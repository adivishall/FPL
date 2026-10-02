"""Typed loaders for the versioned model configurations in ``config/models`` (§83).

Each loader returns the parsed spec together with its ``VersionedConfig`` reference
(``models/<name>@<version>#<hash>``), which is persisted with every model run and prediction.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field

from fpl_domain.config import VersionedConfig, load_versioned_config
from fpl_forecasting.governance import Gate, RetrainPolicy
from fpl_forecasting.minutes import AvailabilityAdjustment, MinutesConfig
from fpl_forecasting.price_change import PriceModelConfig
from fpl_forecasting.rates import RateConfig


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TrainingSpec(_Spec):
    horizons: tuple[int, ...] = (0, 1, 2, 3, 4)
    bps_seasons: int = 2


class SimulationSpec(_Spec):
    n_sims: int = 2000
    seed: int = 20260828
    max_goals: int = 10


class PointsModelSpec(_Spec):
    model: str
    training: TrainingSpec = Field(default_factory=TrainingSpec)
    rates: RateConfig
    simulation: SimulationSpec = Field(default_factory=SimulationSpec)
    availability: AvailabilityAdjustment = Field(default_factory=AvailabilityAdjustment)
    promotion_gates: tuple[Gate, ...] = ()
    retraining: RetrainPolicy = Field(default_factory=RetrainPolicy)


class MinutesModelSpec(_Spec):
    model: str
    params: MinutesConfig
    promotion_gates: tuple[Gate, ...] = ()


class PriceModelSpec(_Spec):
    model: str
    params: PriceModelConfig
    retrain_every_gameweeks: int = 5
    promotion_gates: tuple[Gate, ...] = ()


@lru_cache(maxsize=4)
def points_spec() -> tuple[PointsModelSpec, VersionedConfig]:
    vc = load_versioned_config("models", "points")
    return vc.parse(PointsModelSpec), vc


@lru_cache(maxsize=4)
def minutes_spec() -> tuple[MinutesModelSpec, VersionedConfig]:
    vc = load_versioned_config("models", "minutes")
    return vc.parse(MinutesModelSpec), vc


@lru_cache(maxsize=4)
def price_spec() -> tuple[PriceModelSpec, VersionedConfig]:
    vc = load_versioned_config("models", "price_change")
    return vc.parse(PriceModelSpec), vc
