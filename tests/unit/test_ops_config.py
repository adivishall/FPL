"""Operational configuration stays consistent with the code it monitors and deploys."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from fpl_api.observability import Metrics
from fpl_worker.cli import TASKS, load_schedule

ROOT = Path(__file__).resolve().parents[2]


def _exposed_names() -> set[str]:
    names: set[str] = set()
    for fam in Metrics().registry.collect():
        base = fam.name
        names |= {base, f"{base}_total", f"{base}_bucket", f"{base}_count", f"{base}_sum"}
    return names


def test_alert_rules_reference_real_metrics_and_report_keys() -> None:
    text = (ROOT / "infra/deployment/prometheus/alerts.yml").read_text()
    rules = yaml.safe_load(text)["groups"][0]["rules"]
    exposed = _exposed_names()
    used = set(re.findall(r"\bfpl_[a-z0-9_]+", text))
    assert used and used <= exposed, used - exposed
    gate = json.loads((ROOT / "ml/reports/forecast_eval.json").read_text())["gate_metrics"]
    for m in re.findall(r'metric="([a-z0-9_]+)"', text):
        assert m in gate, m
    for r in rules:
        assert r["annotations"]["runbook"].startswith("docs/DEPLOYMENT.md#")
        anchor = r["annotations"]["runbook"].split("#")[1]
        assert f"{{#{anchor}}}" in (ROOT / "docs/DEPLOYMENT.md").read_text() or anchor in (
            (ROOT / "docs/DEPLOYMENT.md").read_text().lower().replace(" ", "-")
        ), anchor


def test_schedule_tasks_exist_and_compose_runs_every_process() -> None:
    assert set(load_schedule().tasks) <= set(TASKS)
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    assert {"api", "worker", "scheduler", "web", "migrate", "postgres", "redis"} <= set(compose)
    assert compose["worker"]["command"] == ["fpl-worker", "work"]
    assert compose["scheduler"]["command"] == ["fpl-worker", "schedule"]
    assert "FPL_API_KEY" in compose["web"]["environment"]  # key stays server-side


def test_env_example_names_map_to_settings() -> None:
    from fpl_api.settings import Settings

    fields = {f"FPL_{k.upper()}" for k in Settings.model_fields}
    extra = {"FPL_WEB_API_KEY"}  # consumed by docker-compose for the web proxy
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.startswith("FPL_"):
            name = line.split("=", 1)[0]
            assert name in fields | extra, name
