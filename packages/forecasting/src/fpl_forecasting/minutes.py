"""Expected-minutes models (§10, §58): a two-part system.

1. ``P(start)`` — gradient-boosted classifier, isotonic-calibrated on the most recent slice of the
   training window (chronological, no shuffling). Trained on *multi-horizon* rows: features at
   cutoff t with the label "started in the target fixture of GW t+h"; the horizon is a feature,
   so the same model yields return probabilities for players currently absent (§10 'Return
   probability').
2. ``P(sub appearance | no start)`` — gradient-boosted classifier.
3. Minutes given a start — multiclass classifier over ``START_BUCKETS``; minutes given a
   substitute appearance — empirical bucket shares by position.

Baseline for comparison (§57.2): recency-weighted start rate (``RateBaselineMinutes``).

Live availability (FPL status / chance of playing) is not present in any historical training
row (ADR-0001 #7), so it is applied as an explicit, versioned adjustment layer
(``AvailabilityAdjustment``) whose mapping is configuration, not a learned quantity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from pydantic import BaseModel, ConfigDict
from sklearn.isotonic import IsotonicRegression

from fpl_domain.forecast import START_BUCKETS, SUB_BUCKETS

MINUTES_FEATURES = (
    "n_prior_matches",
    "mins_last1",
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
    "pts_ewm_long",
    "pts_p90",
    "xg_p90",
    "price",
    "price_change_season",
    "ownership_pctile",
    "net_transfers_last_gw",
    "pos_GK",
    "pos_DEF",
    "pos_MID",
    "pos_FWD",
    "is_home",
    "horizon",
    "fixtures_in_gw",
    "days_rest",
    "team_xg_for_ewm",
    "opp_xg_for_ewm",
    "ppg_season",
)


def start_bucket(minutes: pd.Series) -> pd.Series:
    m = minutes.astype(float)
    edges = [b[1] for b in START_BUCKETS]
    return pd.Series(np.searchsorted(edges, np.clip(m, 1, 90), side="left"), index=m.index)


def sub_bucket(minutes: pd.Series) -> pd.Series:
    m = minutes.astype(float)
    edges = [b[1] for b in SUB_BUCKETS]
    return pd.Series(
        np.minimum(np.searchsorted(edges, np.clip(m, 1, 89), side="left"), len(SUB_BUCKETS) - 1),
        index=m.index,
    )


class MinutesConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_estimators: int = 300
    learning_rate: float = 0.05
    num_leaves: int = 31
    min_child_samples: int = 100
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_lambda: float = 1.0
    calibration_fraction: float = 0.15
    seed: int = 7
    n_jobs: int = 2


def _lgbm(cfg: MinutesConfig, **kw: Any) -> LGBMClassifier:
    return LGBMClassifier(
        n_estimators=cfg.n_estimators,
        learning_rate=cfg.learning_rate,
        num_leaves=cfg.num_leaves,
        min_child_samples=cfg.min_child_samples,
        subsample=cfg.subsample,
        subsample_freq=1,
        colsample_bytree=cfg.colsample_bytree,
        reg_lambda=cfg.reg_lambda,
        random_state=cfg.seed,
        n_jobs=cfg.n_jobs,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
        **kw,
    )


def _x(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame[list(MINUTES_FEATURES)].copy()
    for c in x.columns:
        x[c] = pd.to_numeric(x[c], errors="coerce").astype(float)
    return x


@dataclass
class MinutesPrediction:
    p_start: np.ndarray
    p_sub: np.ndarray
    start_buckets: np.ndarray
    sub_buckets: np.ndarray

    def expected_minutes(self) -> np.ndarray:
        mid_s = np.array([(lo + hi) / 2 for lo, hi in START_BUCKETS])
        mid_b = np.array([(lo + hi) / 2 for lo, hi in SUB_BUCKETS])
        return self.p_start * (self.start_buckets @ mid_s) + (1 - self.p_start) * self.p_sub * (
            self.sub_buckets @ mid_b
        )


@dataclass
class MinutesModel:
    config: MinutesConfig = field(default_factory=MinutesConfig)
    name: str = "minutes"
    version: str = "1.0.0"
    start_clf: LGBMClassifier | None = None
    start_calibrator: IsotonicRegression | None = None
    sub_clf: LGBMClassifier | None = None
    bucket_clf: LGBMClassifier | None = None
    sub_bucket_probs: np.ndarray | None = None  # [4 positions, n sub buckets]
    trained_rows: int = 0

    def fit(self, train: pd.DataFrame) -> MinutesModel:
        """``train``: feature rows + labels ``minutes`` and ``starts`` for the target fixture."""
        t = train[train["starts"].notna()].copy()
        t = t.sort_values(["kickoff_at"]) if "kickoff_at" in t else t
        y_start = (t["starts"].astype(float) > 0).astype(int)
        n_cal = max(int(len(t) * self.config.calibration_fraction), 1)
        fit_part, cal_part = t.iloc[:-n_cal], t.iloc[-n_cal:]
        self.start_clf = _lgbm(self.config).fit(_x(fit_part), y_start.iloc[:-n_cal])
        raw = np.asarray(self.start_clf.predict_proba(_x(cal_part)))[:, 1]
        self.start_calibrator = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        self.start_calibrator.fit(raw, y_start.iloc[-n_cal:])

        ns = t[y_start == 0]
        y_sub = (ns["minutes"].astype(float) > 0).astype(int)
        self.sub_clf = _lgbm(self.config).fit(_x(ns), y_sub)

        st = t[(y_start == 1) & (t["minutes"] > 0)]
        yb = start_bucket(st["minutes"])
        self.bucket_clf = _lgbm(
            self.config, objective="multiclass", num_class=len(START_BUCKETS)
        ).fit(_x(st), yb)

        subs = t[(y_start == 0) & (t["minutes"] > 0)]
        probs = np.full((4, len(SUB_BUCKETS)), 1.0 / len(SUB_BUCKETS))
        pos_idx = subs[["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]].to_numpy().argmax(axis=1)
        sb = sub_bucket(subs["minutes"]).to_numpy()
        for p in range(4):
            counts = np.bincount(sb[pos_idx == p], minlength=len(SUB_BUCKETS)) + 1.0
            probs[p] = counts / counts.sum()
        self.sub_bucket_probs = probs
        self.trained_rows = len(t)
        return self

    def predict(self, frame: pd.DataFrame) -> MinutesPrediction:
        if (
            self.start_clf is None
            or self.start_calibrator is None
            or self.sub_clf is None
            or self.bucket_clf is None
            or self.sub_bucket_probs is None
        ):
            raise RuntimeError("MinutesModel is not fitted")
        x = _x(frame)
        p_start = self.start_calibrator.predict(np.asarray(self.start_clf.predict_proba(x))[:, 1])
        p_sub = np.asarray(self.sub_clf.predict_proba(x))[:, 1]
        sb = np.zeros((len(frame), len(START_BUCKETS)))
        proba = np.asarray(self.bucket_clf.predict_proba(x))
        sb[:, self.bucket_clf.classes_.astype(int)] = proba
        pos_idx = frame[["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]].to_numpy().argmax(axis=1)
        return MinutesPrediction(
            p_start=np.clip(p_start, 0, 1),
            p_sub=np.clip(p_sub, 0, 1),
            start_buckets=sb / sb.sum(axis=1, keepdims=True),
            sub_buckets=self.sub_bucket_probs[pos_idx],
        )


@dataclass
class RateBaselineMinutes:
    """Baseline: recency-weighted start/sub rates and the player's minutes-if-start."""

    name: str = "minutes_rate_baseline"
    version: str = "1.0.0"
    pos_start_rate: np.ndarray = field(default_factory=lambda: np.full(4, 0.3))

    def fit(self, train: pd.DataFrame) -> RateBaselineMinutes:
        t = train[train["starts"].notna()]
        pos_idx = t[["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]].to_numpy().argmax(axis=1)
        y = (t["starts"].astype(float) > 0).to_numpy()
        self.pos_start_rate = np.array(
            [y[pos_idx == p].mean() if (pos_idx == p).any() else 0.3 for p in range(4)]
        )
        return self

    def predict(self, frame: pd.DataFrame) -> MinutesPrediction:
        pos_idx = frame[["pos_GK", "pos_DEF", "pos_MID", "pos_FWD"]].to_numpy().argmax(axis=1)
        short = frame["start_rate_short"].astype(float).to_numpy()
        long = frame["start_rate_long"].astype(float).to_numpy()
        p = np.where(
            np.isnan(short),
            self.pos_start_rate[pos_idx],
            0.6 * short + 0.4 * np.where(np.isnan(long), short, long),
        )
        sub = np.nan_to_num(frame["sub_app_rate_long"].astype(float).to_numpy(), nan=0.1)
        p_sub = np.clip(sub / np.maximum(1 - p, 1e-3), 0, 1)
        mis = np.nan_to_num(frame["mins_if_start_long"].astype(float).to_numpy(), nan=75.0)
        edges = np.array([b[1] for b in START_BUCKETS])
        idx = np.searchsorted(edges, np.clip(mis, 1, 90))
        sb = np.zeros((len(frame), len(START_BUCKETS)))
        sb[np.arange(len(frame)), idx] = 1.0
        sub_b = np.tile(np.array([0.4, 0.3, 0.2, 0.1]), (len(frame), 1))
        return MinutesPrediction(np.clip(p, 0, 1), p_sub, sb, sub_b)


class AvailabilityAdjustment(BaseModel):
    """Maps live FPL availability signals to a multiplier on start/sub probabilities.

    Uncalibrated configuration (no historical news exists to learn from; ADR-0001 #7). Recovery
    over the horizon models that most flagged players return within a few weeks — except FPL
    status ``u`` (left the club / unavailable for the season), which does not recover.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doubtful_default: float = 0.6
    out_default: float = 0.05
    recovery_per_gw: float = 0.2
    floor_chance_zero: float = 0.02
    departed: float = 0.0

    def multiplier(
        self,
        status_flag: np.ndarray,
        chance: np.ndarray,
        horizon: np.ndarray,
        status_code: np.ndarray | None = None,
    ) -> np.ndarray:
        status = np.asarray(status_flag, dtype=float)
        ch = np.asarray(chance, dtype=float) / 100.0
        base = np.where(
            np.isnan(status),
            1.0,
            np.where(
                status == 0, 1.0, np.where(status == 1, self.doubtful_default, self.out_default)
            ),
        )
        base = np.where(~np.isnan(ch), np.maximum(ch, self.floor_chance_zero), base)
        h = np.asarray(horizon, dtype=float)
        out = np.clip(1.0 - (1.0 - base) * (1.0 - self.recovery_per_gw) ** h, 0.0, 1.0)
        if status_code is not None:
            out = np.where(np.asarray(status_code, dtype=object) == "u", self.departed, out)
        return out
