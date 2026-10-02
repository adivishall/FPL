"""Post-deploy smoke test: read-only checks against a running API (and optionally the UI).

Usage: uv run python infra/scripts/smoke.py --api https://api.example.com [--web https://...]
Exits non-zero on the first failed check; prints one JSON line per check. Uses only GETs, so it
is safe against production. Degraded mode (stale data, unreachable live source) is reported but
is not a failure — serving the last validated snapshot is the designed behaviour.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True)
    p.add_argument("--web")
    p.add_argument("--timeout", type=float, default=30.0)
    a = p.parse_args(argv)
    c = httpx.Client(base_url=a.api.rstrip("/"), timeout=a.timeout)
    failures = 0

    def check(name: str, fn: Callable[[], Any]) -> None:
        nonlocal failures
        t0 = time.perf_counter()
        try:
            detail = fn()
            ok = True
        except Exception as exc:
            detail, ok = f"{type(exc).__name__}: {exc}", False
            failures += 1
        ms = round(1000 * (time.perf_counter() - t0), 1)
        print(json.dumps({"check": name, "ok": ok, "ms": ms, "detail": detail}, default=str))

    def health() -> Any:
        r = c.get("/api/v1/health")
        r.raise_for_status()
        b = r.json()
        assert b["checks"]["dataset"]["ok"], "dataset not loaded"
        assert b["checks"]["database"]["ok"], "database unreachable"
        return {"status": b["status"], "degraded": b["freshness"]["degraded"]}

    def gameweek() -> Any:
        b = c.get("/api/v1/gameweeks/current").raise_for_status().json()
        assert b["decision_cutoff"] < b["deadline"]
        return {"season": b["season"], "gameweek": b["gameweek"]}

    def players() -> Any:
        r = c.get("/api/v1/players", params={"limit": 5})
        if r.status_code == 503:  # forecast not precomputed yet: queued, not broken
            return {"forecast": "queued", "job_id": r.json().get("job_id")}
        ps = r.raise_for_status().json()["players"]
        assert ps and all("xp" in x for x in ps)
        return {"players": len(ps)}

    def metrics() -> Any:
        t = c.get("/metrics").raise_for_status().text
        assert "fpl_http_requests_total" in t and "fpl_data_freshness_hours" in t
        return {"bytes": len(t)}

    def models() -> Any:
        b = c.get("/api/v1/models").raise_for_status().json()
        gates = b.get("forecast_eval", {}).get("promotion_gates", {})
        return {k: v.get("passed") for k, v in gates.items()}

    check("health", health)
    check("gameweek", gameweek)
    check("players", players)
    check("metrics", metrics)
    check("models", models)
    if a.web:
        check("web", lambda: httpx.get(a.web, timeout=a.timeout).raise_for_status().status_code)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
