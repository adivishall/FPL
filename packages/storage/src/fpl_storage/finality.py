"""Result finality: the one rule for "these results are final" (M1.4 journal, M1.5 season
evaluation). Pure: dataset frames in, a verdict out.

A gameweek's results — for all of its fixtures, or for the fixtures of some clubs — are final
when FPL has finalised the gameweek (``gameweeks.status = finalized``: ``finished`` and
``data_checked``), every relevant fixture is ``final`` and ``finalized``, and every result row
of those fixtures is ``finalized``: loaded after the ruleset's finalisation lag (rows loaded
while a fixture was provisional, before bonus was confirmed, stay provisional until the
results are loaded again). Fixture and gameweek status come from the hourly live capture,
result rows from the results load, so the two can disagree; only all three together are final.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

PLAYED = ("final", "provisional")


@dataclass(frozen=True)
class GameweekFinality:
    season: str
    gw: int
    gameweek_status: str  # upcoming | live | provisional | finalized | unknown
    fixtures: frozenset[int]  # the relevant fixtures now in the gameweek
    played: frozenset[int]  # status final or provisional
    recorded: frozenset[int]  # with at least one result row
    official: frozenset[int]  # status final and finalized (FPL ``finished``)
    rows_final: frozenset[int]  # every result row finalized
    _state: tuple[tuple[int, str, bool, bool], ...] = ()

    @property
    def complete(self) -> bool:
        """Every relevant fixture has been played and has result rows."""
        return self.fixtures <= self.played and self.fixtures <= self.recorded

    @property
    def officially_final(self) -> bool:
        """FPL finalised the gameweek and every relevant fixture."""
        return self.gameweek_status == "finalized" and self.fixtures <= self.official

    @property
    def final(self) -> bool:
        return self.complete and self.officially_final and self.fixtures <= self.rows_final

    def state(self) -> str:
        """``not_started`` (no relevant fixture played), ``in_progress`` (some unplayed or
        without results), ``provisional`` (all recorded; FPL has not finalised them),
        ``results_not_final`` (FPL has; the loaded rows predate it) or ``final``."""
        if self.final:
            return "final"
        if not self.played and (
            self.fixtures or self.gameweek_status in ("upcoming", "live", "unknown")
        ):
            return "not_started"
        if not self.complete:
            return "in_progress"
        return "provisional" if not self.officially_final else "results_not_final"

    def counts(self) -> dict[str, Any]:
        return {
            "relevant": len(self.fixtures),
            "played": len(self.played & self.fixtures),
            "recorded": len(self.recorded & self.fixtures),
            "final": len(self.official & self.fixtures),
            "results_final": len(self.rows_final & self.fixtures),
            "gameweek_status": self.gameweek_status,
        }

    def digest_state(self) -> list[tuple[int, str, bool, bool]]:
        """(fixture, status, finalized, rows final) per relevant fixture — what an evaluation's
        input digest covers so a change of finality is a change of input."""
        return list(self._state)


def _season(df: pd.DataFrame, season: str) -> pd.DataFrame:
    return df[df["season"] == season]


def gameweek_finality(
    ds: Any, season: str, gw: int, teams: Iterable[int] | None = None
) -> GameweekFinality:
    """Finality of ``gw``'s results in dataset ``ds``, for every fixture of the gameweek or only
    those involving ``teams``."""
    g = _season(ds["gameweeks"], season)
    g = g[g["gw"] == gw]
    gw_status = str(g["status"].iloc[0]) if not g.empty else "unknown"
    fx = _season(ds["fixtures"], season)
    fx = fx[fx["gw"] == gw]
    if teams is not None:
        ts = [int(t) for t in teams]
        fx = fx[fx["home_team_code"].isin(ts) | fx["away_team_code"].isin(ts)]
    ids = frozenset(int(i) for i in fx["fixture_id"])
    pm = _season(ds["player_match"], season)
    pm = pm[pm["gw"] == gw]
    rel = pm[pm["fixture_id"].isin(list(ids))]
    by_fixture = rel["finalized"].fillna(False).astype(bool).groupby(rel["fixture_id"]).all()
    rows_final = frozenset(int(i) for i, ok in by_fixture.items() if ok)
    finalized = fx["finalized"].fillna(False).astype(bool)
    state = tuple(
        sorted(
            (
                int(r.fixture_id),
                str(r.status),
                bool(r.finalized) if pd.notna(r.finalized) else False,
                int(r.fixture_id) in rows_final,
            )
            for r in fx.itertuples(index=False)
        )
    )
    return GameweekFinality(
        season=season,
        gw=int(gw),
        gameweek_status=gw_status,
        fixtures=ids,
        played=frozenset(int(i) for i in fx.loc[fx["status"].isin(PLAYED), "fixture_id"]),
        recorded=frozenset(int(i) for i in pm["fixture_id"]) & ids,
        official=frozenset(
            int(i) for i in fx.loc[(fx["status"] == "final") & finalized, "fixture_id"]
        ),
        rows_final=rows_final,
        _state=state,
    )
