"""Generate the committed real-data test excerpts under ``data/fixtures/vaastav``.

Excerpts are *subsets* of the pinned upstream files (no values are altered):
* ``merged_gw.csv``: rows for gameweeks <= MAX_GW, restricted to contract columns;
* ``fixtures.csv``: fixtures with event <= MAX_GW (2026-27: full published schedule);
* ``teams.csv``: unchanged;
* ``players_raw.csv``: contract columns only.

Usage: ``uv run python infra/scripts/make_test_fixtures.py`` (needs network access to
raw.githubusercontent.com). The output is deterministic for a given pinned commit.
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import yaml

from fpl_ingestion.contracts import CONTRACTS
from fpl_ingestion.http import HttpConfig, HttpFetcher

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "fixtures" / "vaastav"
EXCERPTS = {"2024-25": 5, "2025-26": 5, "2026-27": 1}


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())["historical_repo"]
    fetcher = HttpFetcher(HttpConfig(allowed_hosts=("raw.githubusercontent.com",)))
    for season, max_gw in EXCERPTS.items():
        frames = {}
        for key, tmpl in cfg["files"].items():
            url = f"{cfg['base_url']}/{cfg['commit']}/{tmpl.format(season=season)}"
            frames[key] = pd.read_csv(io.BytesIO(fetcher.get(url).content), low_memory=False)
        mg = frames["merged_gw"]
        mg = mg[mg["GW"] <= max_gw]
        keep = [c.name for c in CONTRACTS["merged_gw"].columns if c.name in mg.columns]
        frames["merged_gw"] = mg[keep]
        fx = frames["fixtures"]
        if season != "2026-27":
            fx = fx[fx["event"] <= max_gw]
        frames["fixtures"] = fx.drop(columns=["stats"], errors="ignore")
        pr = frames["players_raw"]
        keep_p = [c.name for c in CONTRACTS["players_raw"].columns if c.name in pr.columns]
        frames["players_raw"] = pr[keep_p]
        for key, tmpl in cfg["files"].items():
            path = OUT / tmpl.format(season=season)
            path.parent.mkdir(parents=True, exist_ok=True)
            frames[key].to_csv(path, index=False)
        print(season, {k: len(v) for k, v in frames.items()})


if __name__ == "__main__":
    main()
