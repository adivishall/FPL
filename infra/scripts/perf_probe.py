"""Measure serving performance (§79) — writes ml/reports/performance.{json,md}.

Two modes, reported separately (numbers are machine-specific; the report records the machine):

1. **In-process** (development mode): the real FastAPI app via TestClient on this machine, an
   ephemeral PostgreSQL, production model settings (1,000 simulations, 8-GW canonical forecast,
   5-GW default horizon). Measures the cold forecast (train + simulate, peak RSS), disk/memory
   forecast cache hits, the first request on a cold API process, warm endpoint latency
   percentiles, decision workflows and database round trips.
2. **Deployed** (production topology, optional ``--web/--api``): HTTP against a running stack —
   browser path ``web /backend/* → proxy → API`` and the direct API with an operator key —
   paced under the API's rate limit, plus worker-queue round trips (job submitted over HTTP,
   executed by the RQ worker).

Usage:
  uv run python infra/scripts/perf_probe.py [--snapshot-dir DIR] \
      [--web http://127.0.0.1:3000 --api http://127.0.0.1:8000 --key-file PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
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


def peak_rss_gb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (2**30 if sys.platform == "darwin" else 2**20), 2)  # bytes vs KiB


def machine() -> dict[str, Any]:
    def sh(cmd: list[str]) -> str:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        return out.stdout.strip()

    darwin = sys.platform == "darwin"
    mem = sh(["sysctl", "-n", "hw.memsize"]) if darwin else ""
    return {
        "cpu": sh(["sysctl", "-n", "machdep.cpu.brand_string"]) if darwin else platform.processor(),
        "logical_cpus": os.cpu_count(),
        "memory_gb": round(int(mem) / 2**30, 1) if mem.isdigit() else None,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _probes(c: TestClient, code: int) -> dict[str, tuple[Any, int]]:
    def what_if() -> Any:
        return c.post(
            "/api/v1/what-if",
            json={
                "manager_key": "perf",
                "horizon": 5,
                "scenarios": [{"kind": "minutes_downside", "players": [code]}],
            },
        )

    return {
        "GET /health": (lambda: c.get("/api/v1/health"), 200),
        "GET /gameweeks/current": (lambda: c.get("/api/v1/gameweeks/current"), 200),
        "GET /players?limit=50": (lambda: c.get("/api/v1/players", params={"limit": 50}), 200),
        "GET /players/{code}/forecast (cached)": (
            lambda: c.get(f"/api/v1/players/{code}/forecast"),
            200,
        ),
        "GET /players/{code} (profile)": (lambda: c.get(f"/api/v1/players/{code}"), 50),
        "GET /squad": (lambda: c.get("/api/v1/squad", params={"manager_key": "perf"}), 100),
        "POST /lineup": (lambda: c.post("/api/v1/lineup", json={"manager_key": "perf"}), 30),
        "POST /replacements (1 player, 5 GW)": (
            lambda: c.post(
                "/api/v1/replacements",
                json={"manager_key": "perf", "out_player": code, "horizon": 5},
            ),
            15,
        ),
        "POST /optimize (1 GW)": (
            lambda: c.post(
                "/api/v1/optimize", json={"manager_key": "perf", "horizon": 1, "alternatives": 0}
            ),
            15,
        ),
        "POST /optimize (5 GW, 1 alt)": (
            lambda: c.post(
                "/api/v1/optimize", json={"manager_key": "perf", "horizon": 5, "alternatives": 1}
            ),
            10,
        ),
        "POST /what-if (1 scenario, 5 GW)": (what_if, 10),
        "GET /metrics": (lambda: c.get("/metrics"), 100),
    }


def in_process(snapshot_dir: Path | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
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
            snapshot_dir=snapshot_dir,
            artifact_dir=Path(tmp) / "artifacts",
            feature_store_dir=Path(tmp) / "features",
            n_sims=1000,
            horizon_default=5,
            horizon_max=8,
            forecast_horizon=8,
            sync_horizon_limit=5,
            rate_limit_per_minute=10**6,
            expensive_rate_limit_per_minute=10**6,
        )
        svc = AppServices.build(st)
        _, t_load = timed(lambda: svc.data.ds)
        _, t_id = timed(lambda: svc.data.snapshot_id)
        out["snapshot"] = {
            "id": svc.data.snapshot_id,
            "load_and_verify_s": round(t_load, 2),
            "id_lookup_ms": round(t_id * 1000, 3),
        }
        ctx = svc.context()
        rss0 = peak_rss_gb()
        fc, t_cold = timed(lambda: svc.forecast_for(ctx, 8))
        rss1 = peak_rss_gb()
        svc.forecasts._mem.clear()
        _, t_disk = timed(lambda: svc.forecast_for(ctx, 8))
        _, t_mem = timed(lambda: svc.forecast_for(ctx, 8))
        _, t_h5 = timed(lambda: svc.forecast_for(ctx, 5))  # served from the 8-GW forecast
        out["forecast"] = {
            "season": ctx.season,
            "gameweek": ctx.gameweek,
            "players": int(fc.summary["player_code"].nunique()),
            "rows": len(fc.summary),
            "n_sims": int(fc.provenance["n_simulations"]),
            "cold_train_and_simulate_s": round(t_cold, 1),
            "disk_cache_load_s": round(t_disk, 2),
            "memory_cache_ms": round(t_mem * 1000, 2),
            "shorter_horizon_from_cache_ms": round(t_h5 * 1000, 2),
            "peak_rss_gb_before": rss0,
            "peak_rss_gb_after": rss1,
        }
        _, t_px = timed(lambda: svc.price_probs(ctx))
        out["price_model_train_predict_s"] = round(t_px, 2)
        # a second API process: empty memory cache, warm disk cache → its first request
        cold_app = create_app(st, AppServices.build(st))
        with TestClient(cold_app) as cc:
            _, t_first = timed(
                lambda: cc.get("/api/v1/players", params={"limit": 50}).raise_for_status()
            )
        out["cold_process_first_players_request_s"] = round(t_first, 2)
        app = create_app(st, svc)
        with TestClient(app) as c:
            sq = (
                c.post("/api/v1/optimize/squad", json={"budget": 1000, "horizon": 5})
                .raise_for_status()
                .json()
            )
            codes = [p["player_code"] for p in sq["squad"]]
            body = {
                "manager_key": "perf",
                "picks": [{"player_code": x} for x in codes],
                "bank": sq["bank_after"],
                "free_transfers": 1,
            }
            db = [
                timed(lambda: c.post("/api/v1/squad", json=body).raise_for_status())[1]
                for _ in range(20)
            ]
            lat: dict[str, Any] = {}
            for name, (fn, n) in _probes(c, codes[5]).items():
                fn().raise_for_status()  # warm-up
                lat[name] = pct([timed(lambda fn=fn: fn().raise_for_status())[1] for _ in range(n)])
            out["latency_in_process"] = lat
            out["db_save_squad"] = pct(db)
            recs = []
            rec: dict[str, Any] = {}
            for prof in ("default", "conservative", "aggressive"):
                rb = {
                    "manager_key": "perf",
                    "profile": prof,
                    "horizon": 5,
                    "stability": True,
                    "scenarios": True,
                    "chips": True,
                }
                r, t_rec = timed(lambda rb=rb: c.post("/api/v1/recommendations/generate", json=rb))
                job = c.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
                rec = c.get(
                    "/api/v1/recommendations/current", params={"manager_key": "perf"}
                ).json()
                recs.append(
                    {
                        "profile": prof,
                        "status": job["status"],
                        "wall_s": round(t_rec, 1),
                        "stages_s": {k: round(v, 2) for k, v in rec.get("timings", {}).items()},
                    }
                )
            out["recommendation_full"] = recs
            out["alerts_evaluation_s"] = round(
                timed(lambda: evaluate_alerts(svc, "perf", deliver=False))[1], 2
            )
            out["trace"] = pct(
                [
                    timed(
                        lambda: c.get(
                            f"/api/v1/recommendations/{rec['id']}/trace"
                        ).raise_for_status()
                    )[1]
                    for _ in range(20)
                ]
            )
            out["db_export"] = pct(
                [
                    timed(lambda: c.get("/api/v1/managers/perf/export").raise_for_status())[1]
                    for _ in range(10)
                ]
            )
        out["peak_rss_gb_process"] = peak_rss_gb()
    return out


def _wait(c: httpx.Client, api: str, h: dict[str, str], jid: str) -> dict[str, Any]:
    # polls at most 2/s (the rate limit is 120/min per client); honours Retry-After on 429
    while True:
        r = c.get(f"{api}/api/v1/jobs/{jid}", headers=h)
        if r.status_code == 429:
            time.sleep(float(r.headers.get("retry-after", "1")))
            continue
        r.raise_for_status()
        j: dict[str, Any] = r.json()
        if j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.5)


def deployed(web: str, api: str, key: str) -> dict[str, Any]:
    out: dict[str, Any] = {"web": web, "api": api}
    h = {"x-api-key": key}
    with httpx.Client(timeout=300) as c:
        sq = c.post(f"{api}/api/v1/optimize/squad", json={"budget": 1000, "horizon": 3}, headers=h)
        sq.raise_for_status()
        squad = sq.json()
        mk = f"perf-{int(time.time())}"
        c.post(
            f"{api}/api/v1/squad",
            headers=h,
            json={
                "manager_key": mk,
                "picks": [{"player_code": p["player_code"]} for p in squad["squad"]],
                "bank": squad["bank_after"],
                "free_transfers": 1,
            },
        ).raise_for_status()
        try:
            _deployed_measurements(c, out, web, api, h, mk, squad)
        finally:  # erase the probe's manager data even when a measurement fails
            c.delete(f"{api}/api/v1/managers/{mk}", headers=h)
    return out


def _deployed_measurements(
    c: httpx.Client,
    out: dict[str, Any],
    web: str,
    api: str,
    h: dict[str, str],
    mk: str,
    squad: dict[str, Any],
) -> None:
    code = squad["squad"][3]["player_code"]
    routes = {
        "GET /gameweeks/current": "gameweeks/current",
        "GET /players?limit=50": "players?limit=50",
        "GET /players/{code}/forecast": f"players/{code}/forecast",
    }
    lat: dict[str, Any] = {}
    for label, route in routes.items():
        for via, url, hh in (
            ("via web proxy", f"{web}/backend/{route}", {}),
            ("direct API + key", f"{api}/api/v1/{route}", h),
        ):
            c.get(url, headers=hh).raise_for_status()
            xs = []
            for _ in range(40):  # paced to stay under the 120/min/client rate limit
                xs.append(
                    timed(lambda url=url, hh=hh: c.get(url, headers=hh).raise_for_status())[1]
                )
                time.sleep(0.6)
            lat[f"{label} ({via})"] = pct(xs)
    out["latency_http"] = lat
    rt = []
    for i in range(5):  # one job per minute bucket (identical requests are de-duplicated)
        t0 = time.perf_counter()
        r = c.post(f"{api}/api/v1/notifications/evaluate", json={"manager_key": mk}, headers=h)
        r.raise_for_status()
        j = _wait(c, api, h, r.json()["job_id"])
        rt.append({"status": j["status"], "seconds": round(time.perf_counter() - t0, 2)})
        if i < 4:
            time.sleep(61)
    out["worker_alert_job_round_trip"] = rt
    t0 = time.perf_counter()
    r = c.post(
        f"{api}/api/v1/recommendations/generate",
        headers=h,
        json={
            "manager_key": mk,
            "horizon": 3,
            "stability": True,
            "scenarios": True,
            "chips": True,
        },
    )
    j = _wait(c, api, h, r.json()["job_id"])
    out["worker_recommendation_job"] = {
        "status": j["status"],
        "seconds": round(time.perf_counter() - t0, 1),
    }


def _row(name: str, v: dict[str, float]) -> str:
    return f"| {name} | {v['n']} | {v['p50_ms']} | {v['p95_ms']} | {v['p99_ms']} | {v['max_ms']} |"


def markdown(out: dict[str, Any]) -> str:
    m, ip = out["machine"], out["in_process"]
    f = ip["forecast"]
    lines = [
        "# Serving performance (measured)",
        "",
        f"Machine: {m['cpu']}, {m['logical_cpus']} logical CPUs, {m['memory_gb']} GB, "
        f"{m['platform']}, Python {m['python']}. Measured {out['measured_at']}. "
        "Development-machine numbers, not production guarantees. Generated by "
        "`infra/scripts/perf_probe.py`.",
        "",
        "## In-process (development mode: FastAPI TestClient, ephemeral PostgreSQL)",
        "",
        f"Snapshot `{ip['snapshot']['id']}` ({f['season']} GW{f['gameweek']}): load + manifest "
        f"verification {ip['snapshot']['load_and_verify_s']} s; snapshot-id lookup "
        f"{ip['snapshot']['id_lookup_ms']} ms (memoised).",
        "",
        "| forecast path | time |",
        "|---|---|",
        f"| cold: train models + simulate {f['players']} players × 8 GWs × {f['n_sims']} "
        f"samples | {f['cold_train_and_simulate_s']} s |",
        f"| warm, disk cache (new process) | {f['disk_cache_load_s']} s |",
        f"| warm, memory cache | {f['memory_cache_ms']} ms |",
        f"| 5-GW request served from the 8-GW forecast | {f['shorter_horizon_from_cache_ms']} ms |",
        f"| price-change model train + predict | {ip['price_model_train_predict_s']} s |",
        f"| first `/players` request of a new API process (cold memory, warm disk) | "
        f"{ip['cold_process_first_players_request_s']} s |",
        "",
        f"Peak RSS of the probe process: {ip['peak_rss_gb_process']} GB (macOS RSS excludes "
        "compressed pages; the Linux container measurement is in docs/KNOWN_LIMITATIONS.md).",
        "",
        "Warm latency (forecast cached):",
        "",
        "| endpoint | n | p50 ms | p95 ms | p99 ms | max ms |",
        "|---|---|---|---|---|---|",
    ]
    lines += [_row(f"`{k}`", v) for k, v in ip["latency_in_process"].items()]
    lines += [
        _row("save squad (POST /squad: validate + DB write)", ip["db_save_squad"]),
        _row("trace (lineage reads + integrity checks)", ip["trace"]),
        _row("manager export (DB reads)", ip["db_export"]),
        "",
        "Full recommendation (5 GW, alternatives, stability, scenarios, chip planner; inline):",
        "",
    ]
    for r in ip["recommendation_full"]:
        lines.append(
            f"* {r['profile']}: {r['status']}, **{r['wall_s']} s** — "
            + ", ".join(f"{k} {v} s" for k, v in r["stages_s"].items())
        )
    lines += ["", f"Alert evaluation for one manager: {ip['alerts_evaluation_s']} s."]
    if "deployed" in out:
        d = out["deployed"]
        rt = d["worker_alert_job_round_trip"]
        lines += [
            "",
            "## Deployed (production topology: Docker Compose, real HTTP)",
            "",
            f"Web `{d['web']}` → Next.js server proxy → API `{d['api']}` (key held server-side); "
            "requests paced under the 120/min rate limit; forecast precomputed by the worker.",
            "",
            "| route | n | p50 ms | p95 ms | p99 ms | max ms |",
            "|---|---|---|---|---|---|",
            *[_row(f"`{k}`", v) for k, v in d["latency_http"].items()],
            "",
            "Worker queue (RQ) round trips, submit over HTTP → worker → `succeeded`: alert "
            "evaluation "
            + ", ".join(f"{r['seconds']} s ({r['status']})" for r in rt)
            + f"; full recommendation (3 GW): {d['worker_recommendation_job']['seconds']} s "
            f"({d['worker_recommendation_job']['status']}).",
        ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-dir", type=Path)
    ap.add_argument("--web")
    ap.add_argument("--api")
    ap.add_argument("--key-file", type=Path)
    a = ap.parse_args()
    out: dict[str, Any] = {
        "machine": machine(),
        "measured_at": time.strftime("%Y-%m-%d %H:%M %Z"),
        "in_process": in_process(a.snapshot_dir),
    }
    rep = ROOT / "ml" / "reports"
    (rep / "performance.json").write_text(json.dumps(out, indent=2) + "\n")  # kept if below fails
    if a.web and a.api and a.key_file:
        out["deployed"] = deployed(a.web, a.api, a.key_file.read_text().strip())
    (rep / "performance.json").write_text(json.dumps(out, indent=2) + "\n")
    (rep / "performance.md").write_text(markdown(out) + "\n")
    print(markdown(out))


if __name__ == "__main__":
    main()
