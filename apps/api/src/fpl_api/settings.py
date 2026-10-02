"""API configuration (environment variables prefixed ``FPL_``; never hard-coded secrets)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FPL_", env_file=None, extra="ignore")

    database_url: str | None = Field(None, description="PostgreSQL URL for application state")
    redis_url: str | None = Field(None, description="Redis for queue/cache; absent = inline jobs")
    snapshot_dir: Path | None = Field(None, description="canonical Parquet snapshot to serve")
    artifact_dir: Path = Path("data/artifacts")
    feature_store_dir: Path | None = None
    # forecasting / decision limits (§80 cost control)
    horizon_default: int = 5
    horizon_max: int = 10
    n_sims: int = 1000
    n_sims_max: int = 5000
    forecast_on_demand: bool = Field(
        True,
        description="compute a missing forecast synchronously (dev/tests); production "
        "sets False and serves only precomputed forecasts (ADR-0009)",
    )
    sync_horizon_limit: int = Field(5, description="larger optimisations run as async jobs")
    # security (§35, §75)
    require_api_key: bool = False
    api_keys_sha256: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_per_minute: int = 120
    expensive_rate_limit_per_minute: int = 12
    max_body_bytes: int = 256_000
    # live source (blocked in the build environment; ADR-0001 #5)
    live_sync_enabled: bool = True
    environment: str = "development"
