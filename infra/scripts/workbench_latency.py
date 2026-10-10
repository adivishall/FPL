"""Measure Transfer Workbench latency on a running stack (M1.2 acceptance: slow work runs as a
job; served results are instant).

Creates a throw-away manager with the operator key, stores a squad, then measures:

* ``context``: ``GET /workbench/context`` (p50 / p95);
* ``cold``: ``POST /workbench/evaluate`` for HOLD + 3 engine replacements + the optimiser's best
  plan with one alternative (the page's default question) — the time the API takes to answer
  (``202`` queued in production) and the time until the run is served ``succeeded``;
* ``warm``: the same question again (served from the stored run, no job) and
  ``GET /workbench/runs/{id}`` (p50 / p95);
* the solver's own ``timings`` as recorded in the result.

The manager is erased at the end. Prints Markdown. Usage:
  FPL_OPS_KEY=… uv run python infra/scripts/workbench_latency.py --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import os
import statistics
import time
from typing import Any

import httpx


def _pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p * (len(s) - 1)))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--horizon", type=int, default=None)
    args = ap.parse_args()
    key = os.environ.get("FPL_OPS_KEY")
    if not key:
        raise SystemExit("FPL_OPS_KEY (an operator key) is required")
    c = httpx.Client(base_url=args.api + "/api/v1", headers={"x-api-key": key}, timeout=600)
    manager = f"wb-probe-{int(time.time())}"
    squad = c.post("/optimize/squad", json={"budget": 1000, "horizon": 2}).json()
    picks = [{"player_code": p["player_code"]} for p in squad["squad"]]
    c.post(
        "/squad",
        json={
            "manager_key": manager,
            "picks": picks,
            "bank": squad["bank_after"],
            "free_transfers": 1,
        },
    ).raise_for_status()

    def timed(method: str, path: str, **kw: Any) -> tuple[float, httpx.Response]:
        t0 = time.perf_counter()
        r = c.request(method, path, **kw)
        return (time.perf_counter() - t0) * 1000, r

    ctx_t = []
    for _ in range(args.n):
        ms, r = timed("GET", "/workbench/context", params={"manager_key": manager})
        r.raise_for_status()
        ctx_t.append(ms)
    ctx = c.get("/workbench/context", params={"manager_key": manager}).json()
    out = next(p["player_code"] for p in ctx["players"] if p["position"] == "MID")
    body = {
        "manager_key": manager,
        "horizon": args.horizon or ctx["horizon_max"],
        "plans": [
            {"kind": "hold"},
            {"kind": "replace", "label": "replace", "out_player": out, "candidates": 3},
            {"kind": "best", "label": "best", "alternatives": 1},
        ],
    }
    t_submit, r = timed("POST", "/workbench/evaluate", json=body)
    run = r.json()
    status_code = r.status_code
    t0 = time.perf_counter()
    while run["status"] in ("queued", "running"):
        time.sleep(0.5)
        run = c.get(f"/workbench/runs/{run['id']}").json()
        if time.perf_counter() - t0 > 900:
            break
    t_ready = (time.perf_counter() - t0) * 1000
    warm_post = [timed("POST", "/workbench/evaluate", json=body)[0] for _ in range(5)]
    warm_get = [timed("GET", f"/workbench/runs/{run['id']}")[0] for _ in range(args.n)]
    timings = (run.get("result") or {}).get("result", {}).get("timings", {})
    n_plans = len((run.get("result") or {}).get("result", {}).get("plans", []))
    c.delete(f"/managers/{manager}")
    rows = [
        (
            f"GET /workbench/context p50 / p95 of {args.n}",
            f"{statistics.median(ctx_t):.0f} / {_pct(ctx_t, 0.95):.0f} ms",
        ),
        (
            f"POST /workbench/evaluate, new question ({body['horizon']} GW; HOLD + 3 "
            "replacements + best + 1 alternative)",
            f"{t_submit:.0f} ms (HTTP {status_code}; {run['status']} after "
            f"{t_ready / 1000:.1f} s; {n_plans} plans served)",
        ),
        (
            "solver stages recorded in the result",
            ", ".join(f"{k} {v:.1f} s" for k, v in timings.items()),
        ),
        (
            "POST /workbench/evaluate, same question again, p50 of 5",
            f"{statistics.median(warm_post):.0f} ms",
        ),
        (
            f"GET /workbench/runs/{{id}} p50 / p95 of {args.n}",
            f"{statistics.median(warm_get):.0f} / {_pct(warm_get, 0.95):.0f} ms",
        ),
    ]
    print("| measurement | result |\n|---|---|")
    for k, v in rows:
        print(f"| {k} | {v} |")


if __name__ == "__main__":
    main()
