"""API configuration (environment variables prefixed ``FPL_``; never hard-coded secrets)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[4]  # source checkout; images set FPL_REPORTS_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FPL_", env_file=None, extra="ignore")

    database_url: str | None = Field(None, description="PostgreSQL URL for application state")
    redis_url: str | None = Field(None, description="Redis for queue/cache; absent = inline jobs")
    snapshot_dir: Path | None = Field(None, description="pin one canonical snapshot to serve")
    snapshots_root: Path | None = Field(None, description="directory of exported snapshots")
    snapshot_check_seconds: float = Field(300.0, description="how often to look for a newer one")
    serve_ready_snapshots_only: bool = Field(
        False,
        description="API: swap to a newer snapshot only once its serving forecast is "
        "precomputed (no 'forecast not ready' window after each live refresh)",
    )
    snapshot_retention_keep: int = Field(
        24,
        ge=0,
        description="newest snapshots kept with their caches (0 disables pruning); pinned, "
        "serving and referenced snapshots are always kept (fpl_api.retention)",
    )
    artifact_retention_hours: float = Field(
        24.0, gt=0, description="age after which unused forecast/price caches are deleted"
    )
    artifact_dir: Path = Path("data/artifacts")
    reports_dir: Path | None = Field(None, description="published experiment reports (ml/reports)")
    feature_store_dir: Path | None = None
    # forecasting / decision limits (§80 cost control)
    horizon_default: int = 5
    horizon_max: int = 8
    forecast_horizon: int = Field(
        8, description="one canonical forecast per gameweek covers every request horizon ≤ it"
    )
    n_sims: int = 1000
    n_sims_max: int = 5000
    forecast_on_demand: bool = Field(
        True,
        description="compute a missing forecast synchronously (dev/tests); production "
        "sets False and serves only precomputed forecasts (ADR-0009)",
    )
    sync_horizon_limit: int = Field(5, description="larger optimisations run as async jobs")
    solver_workers: int = Field(
        1,
        ge=1,
        le=32,
        description="processes one request uses for independent MILP solves (stability, chip "
        "weeks, replacement candidates); changes wall time only, never the result",
    )
    # security (§35, §75)
    require_api_key: bool = False
    api_keys_sha256: list[str] = Field(default_factory=list)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    rate_limit_per_minute: int = 120
    expensive_rate_limit_per_minute: int = 12
    max_body_bytes: int = 256_000
    webhook_allowed_hosts: list[str] = Field(
        default_factory=list, description="hosts manager webhooks may target (SSRF guard)"
    )
    # live source (blocked in the build environment; ADR-0001 #5)
    live_sync_enabled: bool = True
    environment: str = "development"

    @property
    def reports_path(self) -> Path:
        return self.reports_dir or _REPO_ROOT / "ml" / "reports"
