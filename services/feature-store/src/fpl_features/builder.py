"""Point-in-time feature builder (§9, §56, ADR-0004).

``build_features(view, season, gameweek, horizon, ruleset)`` returns one row per
(player in the pool at cutoff) × (scheduled team fixture in GWs ``gameweek … gameweek+horizon−1``).
All inputs come from the ``PointInTimeView``; the frame records the latest availability timestamp
it used and the builder raises ``LeakageError`` if that is after the cutoff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fpl_domain.enums import Position
from fpl_domain.errors import LeakageError
from fpl_domain.hashing import sha256_hex
from fpl_domain.rules.model import Ruleset
from fpl_features.registry import FEATURE_NAMES, FEATURE_VERSION
from fpl_storage.pit import PointInTimeView

MAX_ROWS_PER_PLAYER = 40
HL_SHORT, HL_LONG, HL_TEAM = 3.0, 10.0, 8.0
MIN_EXPOSURE_90 = 1.0  # need ≥ 1 weighted full match of exposure for per-90 rates
HISTORY_FEATURES = (
    "n_prior_matches",
    "mins_last1",
    "pts_last1",
    "mins_ewm_short",
    "mins_ewm_long",
    "start_rate_short",
    "start_rate_long",
    "app_rate_short",
    "full90_rate_long",
    "sub_app_rate_long",
    "mins_if_start_long",
    "zero_min_streak",
    "days_since_last_app",
    "xg_p90",
    "xa_p90",
    "goals_p90",
    "assists_p90",
    "threat_p90",
    "creativity_p90",
    "saves_p90",
    "bps_p90",
    "bonus_p90",
    "yellow_p90",
    "pts_p90",
    "pts_ewm_short",
    "pts_ewm_long",
    "xg_share",
    "xa_share",
    "dc_actions_p90",
    "dc_hit_rate",
    "ppg_season",
)
KEYS = (
    "season",
    "player_code",
    "team_code",
    "position",
    "fixture_id",
    "target_gw",
    "opponent_team_code",
    "kickoff_at",
)


@dataclass(frozen=True)
class FeatureFrame:
    frame: pd.DataFrame
    season: str
    gameweek: int
    horizon: int
    cutoff: pd.Timestamp
    max_source_available_at: pd.Timestamp | None
    feature_version: str = FEATURE_VERSION

    @property
    def snapshot_id(self) -> str:
        payload = self.frame.to_csv(index=False, float_format="%.10g")
        meta = json.dumps(
            {
                "season": self.season,
                "gw": self.gameweek,
                "h": self.horizon,
                "cutoff": self.cutoff.isoformat(),
                "v": self.feature_version,
            }
        )
        return f"feat_{sha256_hex(meta + payload)[:20]}"


def _wsum(df: pd.DataFrame, value: pd.Series, weight: pd.Series) -> pd.Series:
    mask = value.notna()
    num = (value.where(mask, 0.0) * weight).groupby(df["player_code"]).sum()
    den = weight.where(mask, 0.0).groupby(df["player_code"]).sum()
    return num / den.replace(0.0, np.nan)


def _player_history_features(
    hist: pd.DataFrame, positions: pd.Series, cutoff: pd.Timestamp, season: str, ruleset: Ruleset
) -> pd.DataFrame:
    if hist.empty:  # e.g. the first cutoff of the dataset: every history feature is unknown
        return pd.DataFrame(
            columns=list(HISTORY_FEATURES), index=pd.Index([], name="player_code"), dtype=float
        )
    h = hist.sort_values(["player_code", "kickoff_at"], ascending=[True, False]).copy()
    h["idx"] = h.groupby("player_code").cumcount()
    h = h[h["idx"] < MAX_ROWS_PER_PLAYER]
    w_s = 0.5 ** (h["idx"] / HL_SHORT)
    w_l = 0.5 ** (h["idx"] / HL_LONG)
    mins = h["minutes"].astype(float)
    played = (mins > 0).astype(float)
    starts = h["starts"].astype("Float64").astype(float)
    g = h.groupby("player_code")
    out = pd.DataFrame(index=pd.Index(sorted(h["player_code"].unique()), name="player_code"))
    out["n_prior_matches"] = g.size()
    first = h[h["idx"] == 0].set_index("player_code")
    out["mins_last1"] = first["minutes"].astype(float)
    out["pts_last1"] = first["points"].astype(float)
    out["mins_ewm_short"] = _wsum(h, mins, w_s)
    out["mins_ewm_long"] = _wsum(h, mins, w_l)
    out["start_rate_short"] = _wsum(h, starts, w_s)
    out["start_rate_long"] = _wsum(h, starts, w_l)
    out["app_rate_short"] = _wsum(h, played, w_s)
    out["full90_rate_long"] = _wsum(h, (mins >= 89).astype(float), w_l)
    sub = ((mins > 0) & (starts == 0)).astype(float).where(starts.notna())
    out["sub_app_rate_long"] = _wsum(h, sub, w_l)
    out["mins_if_start_long"] = _wsum(h, mins.where(starts == 1), w_l)
    # consecutive most-recent zero-minute fixtures
    nz = h[mins > 0].groupby("player_code")["idx"].min()
    out["zero_min_streak"] = nz.reindex(out.index).fillna(out["n_prior_matches"]).astype(float)
    last_app = h[mins > 0].groupby("player_code")["kickoff_at"].max()
    out["days_since_last_app"] = ((cutoff - last_app).dt.total_seconds() / 86400.0).reindex(
        out.index
    )

    expo = (mins / 90.0) * w_l
    expo_sum = expo.groupby(h["player_code"]).sum()
    ok = expo_sum >= MIN_EXPOSURE_90

    def per90(col: str) -> pd.Series:
        v = h[col].astype("Float64").astype(float)
        mask = v.notna()
        num = (v.where(mask, 0.0) * w_l).groupby(h["player_code"]).sum()
        den = (expo.where(mask, 0.0)).groupby(h["player_code"]).sum()
        r = num / den.replace(0.0, np.nan)
        return r.where(ok & (den >= MIN_EXPOSURE_90))

    for src, name in (
        ("xg", "xg_p90"),
        ("xa", "xa_p90"),
        ("goals", "goals_p90"),
        ("assists", "assists_p90"),
        ("threat", "threat_p90"),
        ("creativity", "creativity_p90"),
        ("saves", "saves_p90"),
        ("bps", "bps_p90"),
        ("bonus", "bonus_p90"),
        ("yellow_cards", "yellow_p90"),
        ("points", "pts_p90"),
    ):
        out[name] = per90(src)
    out["pts_ewm_short"] = _wsum(h, h["points"].astype(float), w_s)
    out["pts_ewm_long"] = _wsum(h, h["points"].astype(float), w_l)

    team_xg = h["team_xg"].astype(float)
    denom = (team_xg * (mins / 90.0) * w_l).where(team_xg.notna())
    for src, name in (("xg", "xg_share"), ("xa", "xa_share")):
        v = h[src].astype("Float64").astype(float)
        num = (v * w_l).where(v.notna() & team_xg.notna())
        n_ = num.groupby(h["player_code"]).sum()
        d_ = denom.where(v.notna()).groupby(h["player_code"]).sum()
        out[name] = (n_ / d_.where(d_ > 0.25)).clip(upper=1.5)

    # defensive contribution actions (position-specific definition, current position)
    pos = h["player_code"].map(positions)
    cbi = h["cbi"].astype("Float64").astype(float)
    tck = h["tackles"].astype("Float64").astype(float)
    rec = h["recoveries"].astype("Float64").astype(float)
    acts = cbi + tck + np.where(pos.isin([Position.MID.value, Position.FWD.value]), rec, 0.0)
    acts = acts.where(pos != Position.GK.value)
    h["dc_actions"] = acts
    out["dc_actions_p90"] = per90("dc_actions")
    dc = ruleset.scoring.defensive_contribution
    if dc.enabled:
        thr = pos.map({p.value: r.threshold for p, r in dc.by_position.items()})
        hit = (acts >= thr).astype(float).where(acts.notna() & thr.notna() & (mins > 0))
        out["dc_hit_rate"] = _wsum(h, hit, w_l)
    else:
        out["dc_hit_rate"] = np.nan

    cur = h[h["season"] == season]
    apps = cur[cur["minutes"] > 0].groupby("player_code")
    out["ppg_season"] = (apps["points"].sum() / apps.size()).reindex(out.index)
    return out


def _team_features(tm: pd.DataFrame) -> pd.DataFrame:
    if tm.empty:
        return pd.DataFrame(columns=["team_xg_for_ewm", "team_xg_against_ewm"])
    t = tm.sort_values(["team_code", "kickoff_at"], ascending=[True, False]).copy()
    t["idx"] = t.groupby("team_code").cumcount()
    t = t[t["idx"] < 30]
    w = 0.5 ** (t["idx"] / HL_TEAM)
    xf = t["xg_for"].astype(float).fillna(t["goals_for"].astype(float))
    xa = t["xg_against"].astype(float).fillna(t["goals_against"].astype(float))
    g = t.groupby("team_code")
    wsum = w.groupby(t["team_code"]).sum()
    return pd.DataFrame(
        {
            "team_xg_for_ewm": (xf * w).groupby(t["team_code"]).sum() / wsum,
            "team_xg_against_ewm": (xa * w).groupby(t["team_code"]).sum() / wsum,
            "team_matches": g.size(),
        }
    )


def _schedule(view: PointInTimeView, season: str, gameweek: int, horizon: int) -> pd.DataFrame:
    fx = view.fixtures(season)
    sched = fx.dropna(subset=["gw"]).copy()
    long = pd.concat(
        [
            sched.assign(
                team_code=sched["home_team_code"],
                opponent_team_code=sched["away_team_code"],
                is_home=True,
            ),
            sched.assign(
                team_code=sched["away_team_code"],
                opponent_team_code=sched["home_team_code"],
                is_home=False,
            ),
        ]
    )[["fixture_id", "gw", "kickoff_at", "team_code", "opponent_team_code", "is_home"]]
    long = long.sort_values(["team_code", "kickoff_at"])
    long["prev_kickoff"] = long.groupby("team_code")["kickoff_at"].shift(1)
    long["days_rest"] = (long["kickoff_at"] - long["prev_kickoff"]).dt.total_seconds() / 86400.0
    long["fixtures_in_gw"] = long.groupby(["team_code", "gw"])["fixture_id"].transform("count")
    target = long[(long["gw"] >= gameweek) & (long["gw"] < gameweek + horizon)].copy()
    target["target_gw"] = target["gw"].astype(int)
    target["horizon"] = target["target_gw"] - gameweek
    return target.drop(columns=["prev_kickoff", "gw"])


def build_features(
    view: PointInTimeView, season: str, gameweek: int, horizon: int, ruleset: Ruleset
) -> FeatureFrame:
    cutoff = view.as_of
    pool = view.player_pool(season, gameweek)
    if pool.empty:
        raise ValueError(f"empty player pool for {season} GW{gameweek} at {cutoff}")
    positions = pool.set_index("player_code")["position"]

    hist = view.player_match()
    tm = view.team_match()
    team_xg = tm.set_index(["season", "fixture_id", "team_code"])["xg_for"]
    hist = hist.assign(
        team_xg=pd.Series(
            team_xg.reindex(
                pd.MultiIndex.from_frame(hist[["season", "fixture_id", "team_code"]])
            ).to_numpy(),
            index=hist.index,
        )
    )
    pf = _player_history_features(hist, positions, cutoff, season, ruleset)
    tf = _team_features(tm)

    # economics
    prices = view.price_observations(season)
    first_price = prices.sort_values("observed_at").groupby("player_code")["price"].first()
    trans = view.gw_transfers(season)
    last_tr = trans[trans["gw"] == gameweek - 1].set_index("player_code")

    base = pool[["player_code", "team_code", "position", "price", "ownership_count"]].copy()
    base["season"] = season
    base = base.join(pf, on="player_code")
    base["n_prior_matches"] = base["n_prior_matches"].fillna(0)
    base["zero_min_streak"] = base["zero_min_streak"].fillna(0)
    base["price_change_season"] = (base["price"] - base["player_code"].map(first_price)).fillna(0)
    own = pd.to_numeric(base["ownership_count"], errors="coerce").astype(float)
    if own.isna().all() and "ownership" in pool.columns:  # live snapshot: percentage
        own = pd.to_numeric(pool["ownership"], errors="coerce").astype(float).to_numpy()
        own = pd.Series(own, index=base.index)
    base["ownership_pctile"] = own.rank(pct=True)
    t_in = last_tr["transfers_in"].astype(float)
    t_out = last_tr["transfers_out"].astype(float)
    base["net_transfers_last_gw"] = base["player_code"].map(
        (t_in - t_out) / (t_in + t_out + 1000.0)
    )
    for p in Position:
        base[f"pos_{p.value}"] = base["position"] == p.value
    base = base.join(tf[["team_xg_for_ewm", "team_xg_against_ewm"]], on="team_code")

    # live signals (NaN historically)
    snap = view.latest_snapshot(season)
    if not snap.empty:
        s = snap.set_index("player_code")
        status_map = {"a": 0.0, "d": 1.0, "i": 2.0, "s": 2.0, "u": 2.0, "n": 2.0}
        base["status_flag"] = base["player_code"].map(s["status"].map(status_map))
        base["chance_of_playing"] = base["player_code"].map(
            pd.to_numeric(s["chance_of_playing_next_round"], errors="coerce")
        )
        pens = s["set_piece_json"].map(
            lambda j: json.loads(j).get("penalties_order") == 1 if isinstance(j, str) else np.nan
        )
        base["penalty_taker"] = base["player_code"].map(pens)
    else:
        base["status_flag"] = np.nan
        base["chance_of_playing"] = np.nan
        base["penalty_taker"] = np.nan

    sched = _schedule(view, season, gameweek, horizon)
    opp = tf[["team_xg_for_ewm", "team_xg_against_ewm"]].rename(
        columns={"team_xg_for_ewm": "opp_xg_for_ewm", "team_xg_against_ewm": "opp_xg_against_ewm"}
    )
    sched = sched.join(opp, on="opponent_team_code")
    frame = base.merge(sched, on="team_code", how="inner")
    frame = frame.drop(columns=["ownership_count"])
    for c in FEATURE_NAMES:
        if c not in frame.columns:
            frame[c] = np.nan
    bool_cols = ["is_home", "pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]
    for c in bool_cols:
        frame[c] = frame[c].astype(bool)
    frame["penalty_taker"] = frame["penalty_taker"].astype(float)
    ordered = [*KEYS, *[c for c in FEATURE_NAMES if c not in KEYS]]
    frame = (
        frame[ordered]
        .sort_values(["player_code", "kickoff_at", "fixture_id"])
        .reset_index(drop=True)
    )

    view.assert_no_leakage()
    max_av = view.log.max_available_at
    if max_av is not None and max_av > cutoff:  # pragma: no cover - assert_no_leakage covers
        raise LeakageError(f"features used data available at {max_av} > cutoff {cutoff}")
    return FeatureFrame(
        frame=frame,
        season=season,
        gameweek=gameweek,
        horizon=horizon,
        cutoff=cutoff,
        max_source_available_at=max_av,
    )
