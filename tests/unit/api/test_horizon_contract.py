"""The served planning horizon never exceeds what the forecast models were trained and evaluated
for (BUILD_STATUS defect 40): settings fail fast, request schemas refuse, the container refuses
instead of clamping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fpl_api import schemas as s
from fpl_api.container import AppServices, UnsupportedHorizon
from fpl_api.settings import Settings
from fpl_forecasting.model_config import points_spec
from fpl_forecasting.pipeline import SUPPORTED_HORIZON, TRAIN_HORIZONS


def test_supported_horizon_is_the_trained_target_range() -> None:
    spec, _ = points_spec()
    assert SUPPORTED_HORIZON == max(TRAIN_HORIZONS) + 1 == 5
    assert tuple(spec.training.horizons) == TRAIN_HORIZONS


def test_settings_refuse_horizons_beyond_the_model_contract() -> None:
    assert Settings().horizon_max == Settings().forecast_horizon == SUPPORTED_HORIZON
    with pytest.raises(ValidationError, match="exceeds the validated horizon"):
        Settings(forecast_horizon=SUPPORTED_HORIZON + 1)
    with pytest.raises(ValidationError, match="horizon_max"):
        Settings(horizon_max=SUPPORTED_HORIZON + 1)
    with pytest.raises(ValidationError, match="horizon_max"):
        Settings(horizon_max=3, forecast_horizon=2)
    with pytest.raises(ValidationError, match="horizon_default"):
        Settings(horizon_default=3, horizon_max=2, forecast_horizon=2)
    assert Settings(horizon_default=2, horizon_max=2, forecast_horizon=2).horizon_max == 2


@pytest.mark.parametrize(
    ("cls", "extra"),
    [
        (s.OptimizeIn, {"manager_key": "m"}),
        (s.SquadBuildIn, {}),
        (s.ReplacementIn, {"manager_key": "m", "out_player": 1}),
        (s.ChipSimIn, {"manager_key": "m"}),
        (s.WhatIfIn, {"manager_key": "m", "scenarios": [{"kind": "conservative"}]}),
        (s.RecommendationIn, {"manager_key": "m"}),
        (s.BacktestIn, {"season": "2025-26"}),
    ],
)
def test_request_schemas_reject_unvalidated_horizons(cls: type, extra: dict[str, object]) -> None:
    assert cls(horizon=SUPPORTED_HORIZON, **extra).horizon == SUPPORTED_HORIZON
    with pytest.raises(ValidationError):
        cls(horizon=SUPPORTED_HORIZON + 1, **extra)
    with pytest.raises(ValidationError):
        cls(horizon=0, **extra)


def test_settings_schema_horizon_is_capped_too() -> None:
    assert s.ManagerSettingsIn(horizon=SUPPORTED_HORIZON).horizon == SUPPORTED_HORIZON
    with pytest.raises(ValidationError):
        s.ManagerSettingsIn(horizon=SUPPORTED_HORIZON + 1)


def test_container_refuses_rather_than_clamps() -> None:
    cfg = Settings(horizon_default=2, horizon_max=2, forecast_horizon=2)
    svc = SimpleNamespace(settings=cfg)
    assert AppServices.horizon(svc, None) == 2  # type: ignore[arg-type]
    assert AppServices.horizon(svc, 1) == 1  # type: ignore[arg-type]
    with pytest.raises(UnsupportedHorizon, match="plans 1–2 gameweeks"):
        AppServices.horizon(svc, 3)  # type: ignore[arg-type]
