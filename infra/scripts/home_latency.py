"""Measure Copilot Home latency on a running stack (M1.1a acceptance: the page must not wait for
heavy computation).

Creates a throw-away manager with the operator key, stores a squad, then measures:

* ``cold``: the first ``GET /copilot/home`` after the squad is stored (in queued mode the API
  answers at once with ``pending`` and the worker computes the analysis; the time until a fresh
  analysis is served is reported separately as ``ready_after``);
* ``warm``: repeated ``GET /copilot/home`` served from the stored analysis (p50 / p95);
* for comparison, ``GET /squad`` and ``POST /lineup`` (the previous overview's per-page work).

The manager is erased at the end. Prints Markdown. Usage:
  FPL_OPS_KEY=… uv run python infra/scripts/home_latency.py --api http://127.0.0.1:8000
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
    args = ap.parse_args()
    key = os.environ.get("FPL_OPS_KEY")
    if not key:
        raise SystemExit("FPL_OPS_KEY (an operator key) is required")
    c = httpx.Client(base_url=args.api + "/api/v1", headers={"x-api-key": key}, timeout=120)
    manager = f"latency-probe-{int(time.time())}"
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

    t_cold, r = timed("GET", "/copilot/home", params={"manager_key": manager})
    body = r.json()
    pending = body.get("pending")
    t_ready: float | None = None
    t0 = time.perf_counter()
    while body.get("pending") or body.get("stale"):
        time.sleep(0.5)
        body = c.get("/copilot/home", params={"manager_key": manager}).json()
        if time.perf_counter() - t0 > 300:
            break
    if pending:
        t_ready = (time.perf_counter() - t0) * 1000
    warm = [
        timed("GET", "/copilot/home", params={"manager_key": manager})[0] for _ in range(args.n)
    ]
    squad_t = [timed("GET", "/squad", params={"manager_key": manager})[0] for _ in range(args.n)]
    lineup_t = [timed("POST", "/lineup", json={"manager_key": manager})[0] for _ in range(5)]
    c.delete(f"/managers/{manager}")
    rows = [
        (
            "GET /copilot/home, first call after the squad is stored",
            f"{t_cold:.0f} ms"
            + (" (answered `pending`; analysis queued)" if pending else " (computed inline)"),
        ),
        ("analysis ready after", f"{t_ready:.0f} ms" if t_ready is not None else "immediately"),
        (
            f"GET /copilot/home warm, p50 / p95 of {args.n}",
            f"{statistics.median(warm):.0f} / {_pct(warm, 0.95):.0f} ms",
        ),
        (
            f"GET /squad p50 / p95 of {args.n}",
            f"{statistics.median(squad_t):.0f} / {_pct(squad_t, 0.95):.0f} ms",
        ),
        ("POST /lineup p50 of 5 (previous per-page work)", f"{statistics.median(lineup_t):.0f} ms"),
    ]
    print("| measurement | result |\n|---|---|")
    for k, v in rows:
        print(f"| {k} | {v} |")


if __name__ == "__main__":
    main()
