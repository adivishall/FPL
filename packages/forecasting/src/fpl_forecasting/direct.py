"""Benchmarks for the decomposed simulation model (§57.2, ADR-0006).

* ``DirectPointsModel`` — a single gradient-boosted regressor of fixture points on all
  point-in-time features (multi-horizon rows, horizon as a feature). It is the standard
  "tabular ML" answer and the benchmark the decomposed model must justify itself against: if a
  direct regressor were clearly better on point accuracy, the decomposition would have to earn
  its keep on distributional quality and explainability alone.
* ``ClimatologyDistribution`` — a distributional baseline: the empirical distribution of training
  fixture points in the same position × recent-start-rate band. Gives CRPS / Brier / coverage
  baselines that, unlike point baselines, carry uncertainty.

Live-only features (FPL status, chance of playing, penalty order) are excluded from the direct
model because they are missing from every historical training row (ADR-0001 #7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import pandas as pd
from lightgbm import LGBMRegressor
from pydantic import BaseModel, ConfigDict

from fpl_features.registry import REGISTRY

DIRECT_FEATURES = tuple(f.name for f in REGISTRY if f.family != "live" and f.leakage_risk == "none")


class DirectConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_estimators: int = 400
    learning_rate: float = 0.03
    num_leaves: int = 31
    min_child_samples: int = 200
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    reg_lambda: float = 1.0
    seed: int = 11
    n_jobs: int = 2


def _x(frame: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in DIRECT_FEATURES if c in frame.columns]
    x = frame[cols].copy()
    for c in x.columns:
        x[c] = pd.to_numeric(x[c], errors="coerce").astype(float)
    return x


@dataclass
class DirectPointsModel:
    config: DirectConfig = field(default_factory=DirectConfig)
    name: str = "direct_lgbm"
    version: str = "1.0.0"
    model: LGBMRegressor | None = None
    features: tuple[str, ...] = ()

    def fit(self, train: pd.DataFrame) -> None:
        t = train[train["points"].notna()]
        x = _x(t)
        self.features = tuple(x.columns)
        c = self.config
        self.model = LGBMRegressor(
            n_estimators=c.n_estimators,
            learning_rate=c.learning_rate,
            num_leaves=c.num_leaves,
            min_child_samples=c.min_child_samples,
            subsample=c.subsample,
            subsample_freq=1,
            colsample_bytree=c.colsample_bytree,
            reg_lambda=c.reg_lambda,
            random_state=c.seed,
            n_jobs=c.n_jobs,
            deterministic=True,
            force_row_wise=True,
            verbose=-1,
        ).fit(x, t["points"].astype(float))

    def predict(self, frame: pd.DataFrame) -> pd.Series:
        if self.model is None:
            raise RuntimeError("DirectPointsModel is not fitted")
        x = _x(frame).reindex(columns=list(self.features))
        return pd.Series(np.asarray(self.model.predict(x), dtype=float), index=frame.index)


START_BANDS = (-np.inf, 0.25, 0.5, 0.75, np.inf)


def _cell(frame: pd.DataFrame) -> pd.Series:
    sr = pd.to_numeric(frame["start_rate_long"], errors="coerce").astype(float)
    band = pd.Series(np.digitize(sr.fillna(-1.0), START_BANDS[1:-1]), index=frame.index)
    band = band.where(sr.notna(), -1)
    return frame["position"].astype(str) + ":" + band.astype(str)


@dataclass
class ClimatologyDistribution:
    """Empirical fixture-points distribution per position × start-rate band (training rows)."""

    name: str = "climatology"
    version: str = "1.0.0"
    max_samples: int = 2000
    seed: int = 3
    pools: dict[str, npt.NDArray[np.float64]] = field(default_factory=dict)

    def fit(self, train: pd.DataFrame) -> None:
        t = train[train["points"].notna()]
        cells = _cell(t)
        rng = np.random.default_rng(self.seed)
        overall = t["points"].to_numpy(float)
        self.pools = {"*": overall}
        for key, g in t.groupby(cells):
            v = g["points"].to_numpy(float)
            if len(v) > self.max_samples:
                v = rng.choice(v, self.max_samples, replace=False)
            self.pools[str(key)] = v if len(v) >= 30 else overall

    def samples(self, frame: pd.DataFrame, n: int) -> npt.NDArray[np.float64]:
        """[n, rows] independent samples of fixture points (deterministic per row position), so
        a double gameweek's two fixtures are summed as independent draws."""
        cells = _cell(frame).to_numpy()
        rng = np.random.default_rng(self.seed)
        out = np.empty((n, len(frame)))
        for j, key in enumerate(cells):
            out[:, j] = rng.choice(self.pools.get(key, self.pools["*"]), n, replace=True)
        return out
