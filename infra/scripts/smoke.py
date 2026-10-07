"""Post-deploy smoke test: read-only checks against a running deployment.

Usage:
  uv run python infra/scripts/smoke.py --api http://localhost:8000 [--web http://localhost:3000]
  # public deployment (docker-compose.public.yml): only the web origin is reachable; the checks
  # go through its /backend proxy, behind the site login (FPL_SMOKE_WEB_AUTH=user:password)
  FPL_SMOKE_WEB_AUTH=operator:… uv run python infra/scripts/smoke.py --web https://fpl.example.com
Exits non-zero if any check fails; prints one JSON line per check. Uses only GETs, so it is safe
against production. Degraded mode (stale data, unreachable live source) is reported but is not
a failure — serving the last validated snapshot is the designed behaviour. ``--cacert`` trusts a
private CA (a local test of the TLS proxy with Caddy's internal CA).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", help="API origin (operators); omit to check through --web only")
    p.add_argument("--web")
    p.add_argument("--cacert", help="CA bundle to trust instead of the system store")
    p.add_argument("--timeout", type=float, default=30.0)
    a = p.parse_args(argv)
    if not a.api and not a.web:
        p.error("give --api, --web or both")
    verify: str | bool = a.cacert or True
    auth = os.environ.get("FPL_SMOKE_WEB_AUTH")
    web_auth = tuple(auth.split(":", 1)) if auth else None
    if a.api:
        c = httpx.Client(base_url=a.api.rstrip("/") + "/api/v1", timeout=a.timeout, verify=verify)
    else:  # the same read routes through the web server's server-side proxy
        c = httpx.Client(
            base_url=a.web.rstrip("/") + "/backend", timeout=a.timeout, verify=verify, auth=web_auth
        )
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
        r = c.get("/health")
        r.raise_for_status()
        b = r.json()
        assert b["checks"]["dataset"]["ok"], "dataset not loaded"
        assert b["checks"]["database"]["ok"], "database unreachable"
        return {"status": b["status"], "degraded": b["freshness"]["degraded"]}

    def gameweek() -> Any:
        b = c.get("/gameweeks/current").raise_for_status().json()
        assert b["decision_cutoff"] < b["deadline"]
        return {"season": b["season"], "gameweek": b["gameweek"]}

    def players() -> Any:
        r = c.get("/players", params={"limit": 5})
        if r.status_code == 503:  # forecast not precomputed yet: queued, not broken
            return {"forecast": "queued", "job_id": r.json().get("job_id")}
        ps = r.raise_for_status().json()["players"]
        assert ps and all("xp" in x for x in ps)
        return {"players": len(ps)}

    def metrics() -> Any:
        t = httpx.get(a.api.rstrip("/") + "/metrics", timeout=a.timeout, verify=verify)
        t = t.raise_for_status().text
        assert "fpl_http_requests_total" in t and "fpl_data_freshness_hours" in t
        return {"bytes": len(t)}

    def models() -> Any:
        b = c.get("/models").raise_for_status().json()
        gates = b.get("forecast_eval", {}).get("promotion_gates", {})
        return {k: v.get("passed") for k, v in gates.items()}

    def web() -> Any:
        r = httpx.get(a.web, timeout=a.timeout, verify=verify, auth=web_auth).raise_for_status()
        out: dict[str, Any] = {"status": r.status_code}
        if a.web.startswith("https://"):  # the public TLS proxy: HSTS, and plain HTTP redirected
            assert "max-age=" in r.headers.get("strict-transport-security", ""), "no HSTS"
            plain = httpx.get("http://" + a.web[len("https://") :], timeout=a.timeout)
            assert plain.status_code in (301, 308), f"http answered {plain.status_code}"
            assert plain.headers["location"].startswith("https://"), "http not redirected"
            out.update(hsts=True, http_redirect=plain.status_code)
        return out

    check("health", health)
    check("gameweek", gameweek)
    check("players", players)
    if a.api:  # the metrics endpoint is internal: never published through the web origin
        check("metrics", metrics)
    check("models", models)
    if a.web:
        check("web", web)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
