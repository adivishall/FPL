"""Point-in-time (PIT) access to canonical data (ADR-0004).

``PointInTimeView`` is the **only** accessor used by feature building, model training,
forecasting and backtesting. Every method returns rows whose availability timestamp is
``<= as_of`` and records the latest availability timestamp it handed out, so callers can prove
(and the backtest audit can assert) that nothing from the future was touched.

Availability rules (see ADR-0004 for rationale):

* match statistics / results ......... ``available_at`` (kickoff + provisional lag), or the
  gameweek ``finalized_at`` when ``label_policy == "finalized_only"``;
* fixture schedule ................... ``schedule_available_at``;
* price & ownership observations ..... the fixture kickoff at which they were observed;
* GW transfer counts ................. the deadline of that gameweek;
* live snapshots / news .............. ``available_at`` (capture / publication time);
* player pool for a gameweek (A1/A2) . registration, team and *first-appearance* price of a
  player whose first row is in GW t are treated as known at GW t's cutoff (players are added
  to the game before the deadline of the first GW in which they have data).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd

from fpl_domain.errors import LeakageError
from fpl_storage.dataset import CanonicalDataset

LabelPolicy = Literal["provisional_ok", "finalized_only"]
REGISTRATION_WINDOW_GWS = 3


@dataclass
class AccessLog:
    max_available_at: pd.Timestamp | None = None
    reads: dict[str, int] = field(default_factory=dict)

    def touch(self, name: str, ts: pd.Series | pd.Timestamp | None) -> None:
        self.reads[name] = self.reads.get(name, 0) + 1
        if ts is None:
            return
        latest = ts.max() if isinstance(ts, pd.Series) else ts
        if latest is pd.NaT or latest is None or (isinstance(latest, float)):
            return
        if self.max_available_at is None or latest > self.max_available_at:
            self.max_available_at = latest


class PointInTimeView:
    def __init__(
        self,
        ds: CanonicalDataset,
        as_of: datetime | pd.Timestamp,
        label_policy: LabelPolicy = "provisional_ok",
    ) -> None:
        ts = pd.Timestamp(as_of)
        if ts.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        self.ds = ds
        self.as_of = ts.tz_convert("UTC")
        self.label_policy = label_policy
        self.log = AccessLog()

    # ---------------------------------------------------------------- helpers
    def _guard(self, name: str, df: pd.DataFrame, col: str) -> pd.DataFrame:
        out = df[df[col] <= self.as_of]
        if len(out) and out[col].max() > self.as_of:  # pragma: no cover - defensive
            raise LeakageError(f"{name}: row available after {self.as_of}")
        self.log.touch(name, out[col] if len(out) else None)
        return out

    def assert_no_leakage(self) -> None:
        m = self.log.max_available_at
        if m is not None and m > self.as_of:
            raise LeakageError(f"accessed data available at {m} > as_of {self.as_of}")

    # ---------------------------------------------------------------- schedule
    def gameweeks(self, season: str | None = None) -> pd.DataFrame:
        """Gameweek deadlines (published with the season schedule)."""
        g = self.ds["gameweeks"]
        g = g if season is None else g[g["season"] == season]
        return g[["season", "gw", "deadline_at"]].copy()

    def fixtures(self, season: str | None = None) -> pd.DataFrame:
        """Schedule as known at ``as_of`` (no results)."""
        f = self.ds["fixtures"]
        if season is not None:
            f = f[f["season"] == season]
        f = self._guard("fixtures", f, "schedule_available_at")
        return f[
            [
                "season",
                "fixture_id",
                "gw",
                "kickoff_at",
                "home_team_code",
                "away_team_code",
                "schedule_available_at",
            ]
        ].copy()

    def results(self, season: str | None = None) -> pd.DataFrame:
        f = self.ds["fixtures"]
        if season is not None:
            f = f[f["season"] == season]
        f = f[f["result_available_at"].notna()]
        f = self._guard("results", f, "result_available_at")
        return f[
            [
                "season",
                "fixture_id",
                "gw",
                "kickoff_at",
                "home_team_code",
                "away_team_code",
                "home_score",
                "away_score",
            ]
        ].copy()

    # ---------------------------------------------------------------- match data
    def _label_time(self, pm: pd.DataFrame) -> pd.Series:
        if self.label_policy == "provisional_ok":
            return pm["available_at"]
        fin = self.ds["gameweeks"].set_index(["season", "gw"])["finalized_at"]
        idx = pd.MultiIndex.from_frame(pm[["season", "gw"]])
        return pd.Series(fin.reindex(idx).to_numpy(), index=pm.index)

    def player_match(self, seasons: list[str] | None = None) -> pd.DataFrame:
        pm = self.ds["player_match"]
        if seasons is not None:
            pm = pm[pm["season"].isin(seasons)]
        pm = pm.assign(_label_at=self._label_time(pm))
        pm = pm[pm["_label_at"].notna()]
        out = self._guard("player_match", pm, "_label_at").drop(columns="_label_at")
        return out.drop(columns=["price", "ownership_count", "transfers_in", "transfers_out"])

    def team_match(self, seasons: list[str] | None = None) -> pd.DataFrame:
        tm = self.ds["team_match"]
        if seasons is not None:
            tm = tm[tm["season"].isin(seasons)]
        return self._guard("team_match", tm, "available_at")

    def price_observations(self, season: str) -> pd.DataFrame:
        pm = self.ds["player_match"]
        pm = pm[(pm["season"] == season) & (pm["kickoff_at"] < self.as_of)]
        self.log.touch("price_observations", pm["kickoff_at"] if len(pm) else None)
        return pm[["season", "gw", "player_code", "kickoff_at", "price", "ownership_count"]].rename(
            columns={"kickoff_at": "observed_at"}
        )

    def gw_transfers(self, season: str) -> pd.DataFrame:
        pm = self.ds["player_match"]
        pm = pm[pm["season"] == season]
        dl = self.ds["gameweeks"]
        dl = dl[dl["season"] == season].set_index("gw")["deadline_at"]
        t = pm.groupby(["season", "gw", "player_code"], as_index=False)[
            ["transfers_in", "transfers_out"]
        ].max()
        t["available_at"] = t["gw"].map(dl)
        return self._guard("gw_transfers", t, "available_at")

    # ---------------------------------------------------------------- live state
    def player_snapshots(self, season: str | None = None) -> pd.DataFrame:
        s = self.ds["player_snapshots"]
        if season is not None:
            s = s[s["season"] == season]
        return self._guard("player_snapshots", s, "available_at")

    def news(self, season: str | None = None) -> pd.DataFrame:
        n = self.ds["news_signals"]
        if season is not None:
            n = n[n["season"] == season]
        return self._guard("news_signals", n, "available_at")

    def latest_snapshot(self, season: str) -> pd.DataFrame:
        s = self.player_snapshots(season)
        if s.empty:
            return s
        latest = s["captured_at"].max()
        return s[s["captured_at"] == latest].copy()

    # ---------------------------------------------------------------- player pool
    def player_pool(self, season: str, gw: int) -> pd.DataFrame:
        """Players selectable for ``gw`` with team, position and price known at ``as_of``.

        Uses the latest live snapshot when one exists for the season (authoritative price,
        team, status, news); otherwise derives the pool from match rows under policies A1/A2.
        Columns: player_code, team_code, position, web_name, price, price_source,
        ownership_count, status, chance_of_playing_next_round.
        """
        players = self.ds["players"]
        players = players[players["season"] == season].set_index("player_code")
        snap = self.latest_snapshot(season)
        if not snap.empty:
            out = snap[
                [
                    "player_code",
                    "team_code",
                    "price",
                    "status",
                    "chance_of_playing_next_round",
                    "ownership",
                ]
            ].copy()
            out["position"] = out["player_code"].map(players["position"])
            out["web_name"] = out["player_code"].map(players["web_name"])
            out["price_source"] = "live_snapshot"
            out["ownership_count"] = pd.NA
            return out.reset_index(drop=True)

        pm_all = self.ds["player_match"]
        pm_all = pm_all[(pm_all["season"] == season) & (pm_all["gw"] <= gw)]
        # Registered players: a row in the last REGISTRATION_WINDOW gameweeks (rows exist for
        # every registered player each GW, injured or not; the window tolerates blank GWs).
        pm = pm_all[pm_all["gw"] >= gw - REGISTRATION_WINDOW_GWS]
        if pm.empty:
            return pd.DataFrame(
                columns=[
                    "player_code",
                    "team_code",
                    "position",
                    "web_name",
                    "price",
                    "price_source",
                    "ownership_count",
                    "status",
                    "chance_of_playing_next_round",
                ]
            )
        # A1: registration + team from the player's row for this GW (or latest earlier GW).
        reg = pm.sort_values(["gw", "kickoff_at"]).groupby("player_code").tail(1)
        reg = reg[["player_code", "team_code", "gw"]].rename(columns={"gw": "last_gw"})
        before = pm_all[pm_all["kickoff_at"] < self.as_of].sort_values("kickoff_at")
        last_obs = before.groupby("player_code").tail(1).set_index("player_code")
        first_obs = (
            pm_all.sort_values("kickoff_at").groupby("player_code").head(1).set_index("player_code")
        )
        out = reg.copy()
        has_prior = out["player_code"].isin(last_obs.index)
        out["price"] = (
            out["player_code"]
            .map(last_obs["price"])
            .where(has_prior, out["player_code"].map(first_obs["price"]))
        )
        out["price_source"] = has_prior.map({True: "prior_observation", False: "first_appearance"})
        out["ownership_count"] = out["player_code"].map(last_obs["ownership_count"])
        out["position"] = out["player_code"].map(players["position"])
        out["web_name"] = out["player_code"].map(players["web_name"])
        out["status"] = pd.NA  # no historical availability status exists (ADR-0001 #7)
        out["chance_of_playing_next_round"] = pd.NA
        if len(before):
            self.log.touch("player_pool", before["kickoff_at"])
        return out.drop(columns="last_gw").reset_index(drop=True)


def decision_cutoff(deadline: datetime | pd.Timestamp, buffer_minutes: int) -> pd.Timestamp:
    return pd.Timestamp(deadline) - timedelta(minutes=buffer_minutes)
