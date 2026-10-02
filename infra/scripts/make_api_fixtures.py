"""Build FPL-API-shaped contract fixtures from the committed 2026-27 real-data excerpt.

``bootstrap-static.json`` and ``fixtures.json`` are *reshaped* from real upstream data
(players_raw/teams/fixtures of the pinned commit) into the documented FPL API shape, so the live
parsers can be contract-tested offline. Event deadlines are derived as first kickoff − 90 min.
No values are invented except fields the historical files do not carry (``element_types``
constants and event flags), which are filled with the documented FPL defaults.
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "data" / "fixtures" / "vaastav" / "data" / "2026-27"
OUT = ROOT / "data" / "fixtures" / "fpl_api"
CAPTURED_AT = pd.Timestamp("2026-08-28T09:47:53Z")


def _v(x: object) -> object:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return x


def main() -> None:
    teams = pd.read_csv(SRC / "teams.csv")
    fx = pd.read_csv(SRC / "fixtures.csv")
    pr = pd.read_csv(SRC / "players_raw.csv")
    fx["kickoff_time"] = pd.to_datetime(fx["kickoff_time"], utc=True)
    events = []
    for gw, g in fx.dropna(subset=["event"]).groupby("event"):
        deadline = g["kickoff_time"].min() - pd.Timedelta(minutes=90)
        finished = bool(g["finished"].all())
        events.append(
            {
                "id": int(gw),
                "name": f"Gameweek {int(gw)}",
                "deadline_time": deadline.isoformat().replace("+00:00", "Z"),
                "finished": finished,
                "data_checked": finished,
                "is_current": int(gw) == 1,
                "is_next": int(gw) == 2,
                "is_previous": False,
                "average_entry_score": None,
            }
        )
    elements = []
    for r in pr.to_dict("records"):
        proj = r.get("price_change_projections")
        projections = None
        if isinstance(proj, str) and proj.startswith("["):
            projections = [
                {
                    "offset": p["offset"],
                    "projected_percent": float(p["projected_percent"]),
                    "likelihood": p["likelihood"],
                }
                for p in ast.literal_eval(proj)
            ]
        na = r.get("news_added")
        elements.append(
            {
                "id": int(r["id"]),
                "code": int(r["code"]),
                "web_name": r["web_name"],
                "first_name": _v(r.get("first_name")),
                "second_name": _v(r.get("second_name")),
                "team": int(r["team"]),
                "team_code": int(r["team_code"]),
                "element_type": int(r["element_type"]),
                "now_cost": int(r["now_cost"]),
                "status": r["status"],
                "news": _v(r.get("news")) or "",
                "news_added": None if not isinstance(na, str) or na == "None" else na,
                "chance_of_playing_next_round": None
                if pd.isna(r.get("chance_of_playing_next_round"))
                else int(r["chance_of_playing_next_round"]),
                "selected_by_percent": float(r["selected_by_percent"]),
                "form": _v(r.get("form")),
                "penalties_order": None
                if pd.isna(r.get("penalties_order"))
                else int(r["penalties_order"]),
                "price_change_percent": _v(r.get("price_change_percent")),
                "price_change_projections": projections,
            }
        )
    boot = {
        "events": events,
        "teams": [
            {
                "id": int(t["id"]),
                "code": int(t["code"]),
                "name": t["name"],
                "short_name": t["short_name"],
            }
            for t in teams.to_dict("records")
        ],
        "elements": elements,
        "element_types": [
            {
                "id": 1,
                "singular_name_short": "GKP",
                "squad_select": 2,
                "squad_min_play": 1,
                "squad_max_play": 1,
            },
            {
                "id": 2,
                "singular_name_short": "DEF",
                "squad_select": 5,
                "squad_min_play": 3,
                "squad_max_play": 5,
            },
            {
                "id": 3,
                "singular_name_short": "MID",
                "squad_select": 5,
                "squad_min_play": 2,
                "squad_max_play": 5,
            },
            {
                "id": 4,
                "singular_name_short": "FWD",
                "squad_select": 3,
                "squad_min_play": 1,
                "squad_max_play": 3,
            },
        ],
    }
    fixtures = []
    for r in fx.to_dict("records"):
        fixtures.append(
            {
                "id": int(r["id"]),
                "code": int(r["code"]),
                "event": None if pd.isna(r["event"]) else int(r["event"]),
                "finished": bool(r["finished"]),
                "finished_provisional": bool(r["finished_provisional"]),
                "kickoff_time": r["kickoff_time"].isoformat().replace("+00:00", "Z"),
                "started": bool(r["started"]) if not pd.isna(r["started"]) else None,
                "team_h": int(r["team_h"]),
                "team_a": int(r["team_a"]),
                "team_h_score": None if pd.isna(r["team_h_score"]) else int(r["team_h_score"]),
                "team_a_score": None if pd.isna(r["team_a_score"]) else int(r["team_a_score"]),
                "team_h_difficulty": int(r["team_h_difficulty"]),
                "team_a_difficulty": int(r["team_a_difficulty"]),
            }
        )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "bootstrap-static.json").write_text(json.dumps(boot, indent=1, sort_keys=True))
    (OUT / "fixtures.json").write_text(json.dumps(fixtures, indent=1, sort_keys=True))
    print(len(elements), "elements;", len(fixtures), "fixtures; captured_at", CAPTURED_AT)


if __name__ == "__main__":
    main()
