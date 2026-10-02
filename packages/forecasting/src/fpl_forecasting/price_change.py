"""Probabilistic price-change model (§23, §57.1 'Price Change', §66).

Target: the direction of a player's price movement between consecutive gameweek deadlines,
``price(t+1) − price(t)`` for decision gameweek ``t`` — the quantity that decides whether buying
now or next week is cheaper and how selling prices evolve. Outputs are calibrated probabilities
``p_rise`` and ``p_fall``; movements are never treated as certain (§66).

Information set (decision cutoff of GW t): only gameweek rows ≤ t−1 are visible — prices and
ownership observed at earlier kickoffs, and per-GW transfer totals published at earlier
deadlines (``PointInTimeView.gw_transfers``). The historical archive is gameweek-granular, so the
transfer momentum of the final week before the deadline (the strongest real-world signal) is not
observable in history; live mode will be able to use daily snapshots once enough accumulate.
This makes the historically validated model deliberately conservative (see the report).

Models:
* ``TrendBaseline`` — naive trend: empirical rise/fall rates conditional on the direction of the
  last change and the quintile of last week's net-transfer ratio (§57.1 'naive trend baseline').
* ``PriceChangeModel`` — two gradient-boosted classifiers (rise, fall), each isotonic-calibrated
  on the chronologically latest slice of its training window.
* ``official_signal`` — the official Price Change Predictor, ingested from live snapshots, is
  surfaced as a separate signal and never mixed into training labels or features (ADR-0001 #20).
  No historical season carries it, so it is absent from every evaluation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from pydantic import BaseModel, ConfigDict
from sklearn.isotonic import IsotonicRegression

from fpl_storage.pit import PointInTimeView

PRICE_FEATURES = (
    "price",
    "d_price_1",
    "d_price_3",
    "d_price_season",
    "rises_season",
    "falls_season",
    "gws_since_change",
    "net_ratio_1",
    "net_ratio_2",
    "in_ratio_1",
    "out_ratio_1",
    "log_ownership",
    "ownership_pctile",
    "pts_last3",
    "mins_last3",
    "gw",
)


def _gw_panel(view: PointInTimeView, season: str) -> pd.DataFrame:
    """One row per player × observed GW (visible at the view's cutoff), forward-filled over
    blank gameweeks so price history is continuous."""
    obs = view.price_observations(season)
    if obs.empty:
        return pd.DataFrame()
    obs = obs.sort_values("observed_at")
    p = obs.groupby(["player_code", "gw"]).agg(
        price=("price", "last"), own=("ownership_count", "last")
    )
    tr = view.gw_transfers(season).set_index(["player_code", "gw"])[
        ["transfers_in", "transfers_out"]
    ]
    p = p.join(tr, how="left")
    pm = view.player_match([season])
    perf = pm.groupby(["player_code", "gw"])[["points", "minutes"]].sum()
    p = p.join(perf, how="left").reset_index()
    for c in ("price", "own", "transfers_in", "transfers_out", "points", "minutes"):
        p[c] = pd.to_numeric(p[c], errors="coerce").astype(float)
    p["season"] = season
    return p.sort_values(["player_code", "gw"]).reset_index(drop=True)


def _features_from_history(panel: pd.DataFrame, decision_gw: int) -> pd.DataFrame:
    """Features for decision GW ``t`` from panel rows with gw ≤ t−1 only."""
    h = panel[panel["gw"] <= decision_gw - 1]
    if h.empty:
        return pd.DataFrame(columns=["player_code", *PRICE_FEATURES])
    g = h.groupby("player_code")
    last = g.tail(1).set_index("player_code")
    out = pd.DataFrame(index=last.index)
    price = last["price"]
    out["price"] = price

    def lag_price(k: int) -> pd.Series:
        lagged = h[h["gw"] <= decision_gw - 1 - k].groupby("player_code")["price"].last()
        return lagged.reindex(out.index)

    out["d_price_1"] = (price - lag_price(1)).fillna(0.0)
    out["d_price_3"] = (price - lag_price(3)).fillna(0.0)
    out["d_price_season"] = price - g["price"].first().reindex(out.index)
    d = h.assign(dp=g["price"].diff())
    out["rises_season"] = d[d["dp"] > 0].groupby("player_code").size().reindex(out.index).fillna(0)
    out["falls_season"] = d[d["dp"] < 0].groupby("player_code").size().reindex(out.index).fillna(0)
    last_change = d[d["dp"] != 0].dropna(subset=["dp"]).groupby("player_code")["gw"].max()
    first_gw = g["gw"].min()
    out["gws_since_change"] = (
        (decision_gw - 1 - last_change.reindex(out.index)).fillna(decision_gw - 1 - first_gw)
    ).astype(float)
    own = last["own"]
    denom = own.fillna(0) + 1000.0
    out["net_ratio_1"] = (last["transfers_in"] - last["transfers_out"]) / denom
    prev = h[h["gw"] == decision_gw - 2].set_index("player_code")
    out["net_ratio_2"] = (
        (prev["transfers_in"] - prev["transfers_out"]) / (prev["own"] + 1000.0)
    ).reindex(out.index)
    out["in_ratio_1"] = last["transfers_in"] / denom
    out["out_ratio_1"] = last["transfers_out"] / denom
    out["log_ownership"] = np.log1p(own)
    out["ownership_pctile"] = own.rank(pct=True)
    recent = h[h["gw"] >= decision_gw - 3].groupby("player_code")[["points", "minutes"]].sum()
    out["pts_last3"] = recent["points"].reindex(out.index).fillna(0.0)
    out["mins_last3"] = recent["minutes"].reindex(out.index).fillna(0.0)
    out["gw"] = float(decision_gw)
    return out.reset_index()


def decision_features(view: PointInTimeView, season: str, decision_gw: int) -> pd.DataFrame:
    """Features for every player with a visible price history, at decision GW ``t``."""
    panel = _gw_panel(view, season)
    if panel.empty:
        return pd.DataFrame(columns=["player_code", *PRICE_FEATURES])
    f = _features_from_history(panel, decision_gw)
    f["season"] = season
    f["decision_gw"] = decision_gw
    return f


def training_rows(view: PointInTimeView, seasons: list[str]) -> pd.DataFrame:
    """Labelled rows whose outcome (price at GW t and t+1) is visible at the view's cutoff."""
    parts = []
    for season in seasons:
        panel = _gw_panel(view, season)
        if panel.empty:
            continue
        price = panel.pivot_table(index="player_code", columns="gw", values="price", aggfunc="last")
        gws = sorted(int(c) for c in price.columns)
        for t in gws:
            if t + 1 not in price.columns or t < 2:
                continue
            f = _features_from_history(panel, t)
            if f.empty:
                continue
            dy = (price[t + 1] - price[t]).reindex(f["player_code"]).to_numpy()
            f["season"] = season
            f["decision_gw"] = t
            f["d_next"] = dy
            parts.append(f[~np.isnan(dy)])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


class PriceModelConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_estimators: int = 300
    learning_rate: float = 0.03
    num_leaves: int = 15
    min_child_samples: int = 100
    calibration_fraction: float = 0.2
    seed: int = 13
    n_jobs: int = 2


def _x(df: pd.DataFrame) -> pd.DataFrame:
    return df[list(PRICE_FEATURES)].apply(pd.to_numeric, errors="coerce").astype(float)


def _order(df: pd.DataFrame) -> pd.DataFrame:
    return df.sort_values(["season", "decision_gw"], kind="stable")


@dataclass
class PriceChangeModel:
    config: PriceModelConfig = field(default_factory=PriceModelConfig)
    name: str = "price_change_lgbm"
    version: str = "1.0.0"
    clfs: dict[str, tuple[LGBMClassifier, IsotonicRegression]] = field(default_factory=dict)

    def fit(self, rows: pd.DataFrame) -> PriceChangeModel:
        t = _order(rows)
        n_cal = max(int(len(t) * self.config.calibration_fraction), 1)
        fit_part, cal_part = t.iloc[:-n_cal], t.iloc[-n_cal:]
        c = self.config
        for name, sign in (("rise", 1.0), ("fall", -1.0)):
            y_fit = (np.sign(fit_part["d_next"]) == sign).astype(int)
            y_cal = (np.sign(cal_part["d_next"]) == sign).astype(int)
            clf = LGBMClassifier(
                n_estimators=c.n_estimators,
                learning_rate=c.learning_rate,
                num_leaves=c.num_leaves,
                min_child_samples=c.min_child_samples,
                subsample=0.8,
                subsample_freq=1,
                colsample_bytree=0.8,
                random_state=c.seed,
                n_jobs=c.n_jobs,
                deterministic=True,
                force_row_wise=True,
                verbose=-1,
            ).fit(_x(fit_part), y_fit)
            raw = np.asarray(clf.predict_proba(_x(cal_part)))[:, 1]
            iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, y_cal)
            self.clfs[name] = (clf, iso)
        return self

    def predict(self, features: pd.DataFrame) -> pd.DataFrame:
        if not self.clfs:
            raise RuntimeError("PriceChangeModel is not fitted")
        out = pd.DataFrame({"player_code": features["player_code"].to_numpy()})
        x = _x(features)
        for name, (clf, iso) in self.clfs.items():
            out[f"p_{name}"] = iso.predict(np.asarray(clf.predict_proba(x))[:, 1])
        # the two calibrated marginals can (rarely) sum above 1; renormalise jointly
        tot = (out["p_rise"] + out["p_fall"]).clip(lower=1.0)
        out["p_rise"] /= tot
        out["p_fall"] /= tot
        return out


@dataclass
class TrendBaseline:
    """Empirical P(rise), P(fall) given last-change direction × net-transfer-ratio quintile."""

    name: str = "trend_baseline"
    version: str = "1.0.0"
    edges: np.ndarray = field(default_factory=lambda: np.zeros(0))
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    prior: tuple[float, float] = (0.02, 0.05)

    def _cell(self, df: pd.DataFrame) -> pd.Series:
        direction = np.sign(df["d_price_1"].astype(float)).astype(int).astype(str)
        q = np.searchsorted(self.edges, df["net_ratio_1"].astype(float).fillna(0.0))
        return direction + ":" + pd.Series(q, index=df.index).astype(str)

    def fit(self, rows: pd.DataFrame) -> TrendBaseline:
        r = rows["net_ratio_1"].astype(float).fillna(0.0)
        self.edges = np.quantile(r, [0.2, 0.4, 0.6, 0.8])
        cell = self._cell(rows)
        y = np.sign(rows["d_next"].astype(float))
        df = pd.DataFrame({"cell": cell, "rise": y > 0, "fall": y < 0})
        self.prior = (float(df["rise"].mean()), float(df["fall"].mean()))
        agg = df.groupby("cell").agg(n=("rise", "size"), rise=("rise", "sum"), fall=("fall", "sum"))
        k = 20.0  # additive smoothing toward the overall rates
        agg["p_rise"] = (agg["rise"] + k * self.prior[0]) / (agg["n"] + k)
        agg["p_fall"] = (agg["fall"] + k * self.prior[1]) / (agg["n"] + k)
        self.table = agg[["p_rise", "p_fall"]]
        return self

    def predict(self, features: pd.DataFrame) -> pd.DataFrame:
        cell = self._cell(features)
        out = pd.DataFrame({"player_code": features["player_code"].to_numpy()})
        out["p_rise"] = cell.map(self.table["p_rise"]).fillna(self.prior[0]).to_numpy()
        out["p_fall"] = cell.map(self.table["p_fall"]).fillna(self.prior[1]).to_numpy()
        return out


def official_signal(view: PointInTimeView, season: str) -> pd.DataFrame:
    """The official Price Change Predictor fields from the latest live snapshot at the cutoff,
    kept separate from the engine's own probabilities (ADR-0001 #20) so explanations can show
    both and record disagreement. Empty when no snapshot carries the signal (all history)."""
    snap = view.latest_snapshot(season)
    cols = ["player_code", "official_price_change_percent", "official_captured_at"]
    if snap.empty or "official_price_signal_json" not in snap:
        return pd.DataFrame(columns=cols)

    def pct(j: object) -> float:
        if isinstance(j, str):
            j = json.loads(j)
        v = j.get("price_change_percent") if isinstance(j, dict) else None
        return float(v) if v is not None else float("nan")

    out = pd.DataFrame(
        {
            "player_code": snap["player_code"].to_numpy(),
            "official_price_change_percent": snap["official_price_signal_json"].map(pct).to_numpy(),
            "official_captured_at": snap["captured_at"].to_numpy(),
        }
    )
    return out[out["official_price_change_percent"].notna()].reset_index(drop=True)
