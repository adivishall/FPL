"""Measure serving performance on the full canonical snapshot (docs/PERFORMANCE.md).

Runs the real API in-process (FastAPI TestClient) against an ephemeral PostgreSQL and the newest
snapshot under data/snapshots, with production settings (1000 simulations, 8-GW canonical
forecast, 5-GW default horizon). Reports cold/warm forecast times and per-endpoint latency
percentiles; writes ml/reports/performance.{json,md}. Numbers are machine-specific: the report
records the CPU count and Python version next to them.
"""

from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
os.environ.setdefault("FPL_CONFIG_DIR", str(ROOT / "config"))

from fpl_api.alerts import evaluate_alerts  # noqa: E402
from fpl_api.app import create_app  # noqa: E402
from fpl_api.container import AppServices  # noqa: E402
from fpl_api.settings import Settings  # noqa: E402
from fpl_storage.testing import ephemeral_postgres  # noqa: E402


def pct(xs: list[float]) -> dict[str, float]:
    a = np.asarray(xs) * 1000
    return {
        "n": len(xs),
        "p50_ms": round(float(np.percentile(a, 50)), 1),
        "p95_ms": round(float(np.percentile(a, 95)), 1),
        "p99_ms": round(float(np.percentile(a, 99)), 1),
        "max_ms": round(float(a.max()), 1),
    }


def timed(fn: Any) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def main() -> None:
    out: dict[str, Any] = {
        "machine": {
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
    }
    with ephemeral_postgres() as url, tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, FPL_DATABASE_URL=url)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        st = Settings(
            database_url=url,
            artifact_dir=Path(tmp) / "artifacts",
            feature_store_dir=Path(tmp) / "features",
            n_sims=1000,
            horizon_default=5,
            horizon_max=8,
            forecast_horizon=8,
            sync_horizon_limit=5,
            rate_limit_per_minute=100000,
            expensive_rate_limit_per_minute=100000,
        )
        svc = AppServices.build(st)
        _, t_load = timed(lambda: svc.data.ds)
        out["snapshot"] = {"id": svc.data.snapshot_id, "load_s": round(t_load, 2)}
        ctx = svc.context()
        fc, t_cold = timed(lambda: svc.forecast_for(ctx, 8))
        svc.forecasts._mem.clear()
        _, t_disk = timed(lambda: svc.forecast_for(ctx, 8))
        _, t_mem = timed(lambda: svc.forecast_for(ctx, 8))
        out["forecast"] = {
            "players": int(fc.summary["player_code"].nunique()),
            "rows": len(fc.summary),
            "n_sims": int(fc.provenance["n_simulations"]),
            "cold_train_and_simulate_s": round(t_cold, 1),
            "disk_cache_load_s": round(t_disk, 2),
            "memory_cache_s": round(t_mem, 4),
        }
        _, t_px = timed(lambda: svc.price_probs(ctx))
        out["price_model_train_predict_s"] = round(t_px, 2)
        app = create_app(st, svc)
        with TestClient(app) as c:
            r = c.post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 5})
            r.raise_for_status()
            sq = r.json()
            c.post(
                "/api/v1/squad",
                json={
                    "manager_key": "perf",
                    "picks": [{"player_code": p["player_code"]} for p in sq["squad"]],
                    "bank": sq["bank_after"],
                    "free_transfers": 1,
                },
            ).raise_for_status()
            code = sq["squad"][0]["player_code"]
            probes = {
                "GET /health": lambda: c.get("/api/v1/health"),
                "GET /gameweeks/current": lambda: c.get("/api/v1/gameweeks/current"),
                "GET /players?limit=50": lambda: c.get("/api/v1/players", params={"limit": 50}),
                "GET /players/{code}/forecast": lambda: c.get(f"/api/v1/players/{code}/forecast"),
                "POST /lineup": lambda: c.post("/api/v1/lineup", json={"manager_key": "perf"}),
                "POST /optimize (5 GW, 1 alt)": lambda: c.post(
                    "/api/v1/optimize",
                    json={"manager_key": "perf", "horizon": 5, "alternatives": 1},
                ),
                "GET /metrics": lambda: c.get("/metrics"),
            }
            lat: dict[str, Any] = {}
            for name, fn in probes.items():
                n = 5 if "optimize" in name else 40
                fn().raise_for_status()  # warm-up
                xs = []
                for _ in range(n):
                    _, dt = timed(lambda fn=fn: fn().raise_for_status())
                    xs.append(dt)
                lat[name] = pct(xs)
            out["latency"] = lat
            body = {
                "manager_key": "perf",
                "horizon": 5,
                "stability": True,
                "scenarios": True,
                "chips": True,
            }
            r, t_rec = timed(lambda: c.post("/api/v1/recommendations/generate", json=body))
            job = c.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
            rec = c.get("/api/v1/recommendations/current", params={"manager_key": "perf"}).json()
            out["recommendation_full"] = {
                "status": job["status"],
                "wall_s": round(t_rec, 1),
                "stages_s": {k: round(v, 2) for k, v in rec.get("timings", {}).items()},
            }
            _, t_alert = timed(lambda: evaluate_alerts(svc, "perf", deliver=False))
            out["alerts_evaluation_s"] = round(t_alert, 2)
            _, t_trace = timed(lambda: c.get(f"/api/v1/recommendations/{rec['id']}/trace"))
            out["trace_s"] = round(t_trace, 3)
    (ROOT / "ml/reports/performance.json").write_text(json.dumps(out, indent=2))
    lines = [
        "# Serving performance (measured)",
        "",
        f"Machine: {out['machine']['cpus']} CPUs, Python {out['machine']['python']}. "
        f"Snapshot `{out['snapshot']['id']}` (load {out['snapshot']['load_s']} s).",
        "",
        "## Forecast",
        "",
        f"* {out['forecast']['players']} players × 8 GWs, {out['forecast']['n_sims']} joint "
        f"simulations: cold train + simulate **{out['forecast']['cold_train_and_simulate_s']} s**; "
        f"disk-cache load {out['forecast']['disk_cache_load_s']} s; memory hit "
        f"{out['forecast']['memory_cache_s'] * 1000:.1f} ms.",
        f"* Price-change model train + predict: {out['price_model_train_predict_s']} s.",
        "",
        "## API latency (warm forecast cache, in-process client)",
        "",
        "| Endpoint | n | p50 ms | p95 ms | p99 ms | max ms |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in out["latency"].items():
        lines.append(
            f"| `{k}` | {v['n']} | {v['p50_ms']} | {v['p95_ms']} | {v['p99_ms']} | {v['max_ms']} |"
        )
    rf = out["recommendation_full"]
    lines += [
        "",
        "## Decision workflows",
        "",
        f"* Full recommendation (5 GW, alternatives, stability, scenarios, chip planner): "
        f"**{rf['wall_s']} s** wall; stages: "
        + ", ".join(f"{k} {v} s" for k, v in rf["stages_s"].items()),
        f"* Alert evaluation for one manager: {out['alerts_evaluation_s']} s; trace: "
        f"{out['trace_s'] * 1000:.0f} ms.",
        "",
        f"Median of all interactive p50s: "
        f"{statistics.median(v['p50_ms'] for v in out['latency'].values()):.1f} ms.",
    ]
    (ROOT / "ml/reports/performance.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
