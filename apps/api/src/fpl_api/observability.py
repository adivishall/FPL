"""Metrics for the observability contract (§76.1), exported in Prometheus format at /metrics.

Request metrics are recorded by the HTTP middleware; domain metrics by the services (forecast
cache and latency, optimisation runtime and solver status, recommendation outcomes). Gauges that
describe state (data freshness, queue depth, job failures, model gate metrics) are refreshed at
scrape time from their sources of truth, so they can never drift from the database or reports.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from sqlalchemy import Engine, func, or_, select

from fpl_storage import models as m
from fpl_storage.db import session_scope

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


class Metrics:
    def __init__(self) -> None:
        r = self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "fpl_http_requests_total", "HTTP requests", ["method", "route", "status"], registry=r
        )
        self.http_latency = Histogram(
            "fpl_http_request_seconds",
            "HTTP latency",
            ["route"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.security_events = Counter(
            "fpl_security_events_total",
            "Rejected requests (auth / rate limit / size)",
            ["kind"],
            registry=r,
        )
        self.forecast_cache = Counter(
            "fpl_forecast_cache_total", "Forecast cache lookups", ["result"], registry=r
        )
        self.forecast_seconds = Histogram(
            "fpl_forecast_seconds",
            "Forecast generation latency (train + simulate)",
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.forecast_failures = Counter(
            "fpl_forecast_failures_total", "Forecast generation failures", registry=r
        )
        self.optimization_seconds = Histogram(
            "fpl_optimization_seconds",
            "Optimisation stage runtime",
            ["stage"],
            buckets=LATENCY_BUCKETS,
            registry=r,
        )
        self.optimization_status = Counter(
            "fpl_optimization_status_total",
            "Solver status of recommendation plans",
            ["status"],
            registry=r,
        )
        self.recommendations = Counter(
            "fpl_recommendations_total",
            "Recommendation generation outcomes",
            ["outcome"],
            registry=r,
        )
        self.freshness_hours = Gauge(
            "fpl_data_freshness_hours", "Age of the newest source datum", ["source"], registry=r
        )
        self.job_failures = Counter(
            "fpl_job_failures_total",
            "Job failures recorded by this process, by kind and failure (error | interrupted)",
            ["kind", "failure"],
            registry=r,
        )
        # authoritative across processes (the scheduler's reaper serves no metrics): from the DB
        self.jobs_failed = Gauge(
            "fpl_jobs_failed",
            "Failed jobs in the database by kind and failure (error | interrupted)",
            ["kind", "failure"],
            registry=r,
        )
        self.jobs = Gauge(
            "fpl_jobs", "Jobs by status (queue depth = queued)", ["status"], registry=r
        )
        self.model_metric = Gauge(
            "fpl_model_metric",
            "Latest evaluation metric of a model",
            ["model", "metric"],
            registry=r,
        )

    def refresh(
        self, engine: Engine | None, freshness_hours: float | None, reports_dir: Path
    ) -> None:
        if freshness_hours is not None:
            self.freshness_hours.labels("bootstrap").set(freshness_hours)
        if engine is not None:
            with session_scope(engine) as s:
                counts = dict(
                    s.execute(select(m.JobRow.status, func.count()).group_by(m.JobRow.status)).all()
                )
                interrupted = or_(
                    m.JobRow.error.like("abandoned:%"), m.JobRow.error.like("interrupted:%")
                )
                failed = s.execute(
                    select(m.JobRow.kind, interrupted, func.count())
                    .where(m.JobRow.status == "failed")
                    .group_by(m.JobRow.kind, interrupted)
                ).all()
            for st in ("queued", "running", "succeeded", "failed"):
                self.jobs.labels(st).set(float(counts.get(st, 0)))
            for kind, intr, n in failed:
                self.jobs_failed.labels(kind, "interrupted" if intr else "error").set(float(n))
        for name in ("forecast_eval", "price_change"):
            path = reports_dir / f"{name}.json"
            if not path.exists():
                continue
            data: dict[str, Any] = json.loads(path.read_text())
            for k, v in (data.get("gate_metrics") or {}).items():
                if isinstance(v, int | float):
                    self.model_metric.labels(name, k).set(float(v))
