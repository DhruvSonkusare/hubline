"""Application settings. Every value comes from environment variables (or backend/.env locally).

Never read os.environ directly elsewhere in the app; add a typed field here instead.
"""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


class DatabaseSettings(BaseSettings):
    """The minimum needed to talk to the database (used by Alembic, which needs nothing else)."""

    model_config = _ENV_CONFIG

    database_url: str = Field(
        description="Postgres URL; postgres://, postgresql:// or postgresql+asyncpg://"
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)


class Settings(DatabaseSettings):
    model_config = _ENV_CONFIG

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- API auth (Next.js server -> backend). Comma-separated list allows zero-downtime rotation.
    backend_api_tokens: str = Field(min_length=1)

    # --- Public URLs
    public_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"

    # --- Background work (R3: idle-friendly, switchable)
    enable_scheduler: bool = True
    enable_agent_worker: bool = True
    scheduler_min_sleep_s: float = Field(default=1.0, gt=0)
    scheduler_idle_max_s: float = Field(default=60.0, gt=0)
    worker_idle_max_s: float = Field(default=60.0, gt=0)
    scheduler_batch_size: int = Field(default=5, ge=1, le=50)
    poll_lease_s: int = Field(default=120, ge=10)
    poll_timeout_s: float = Field(default=60.0, gt=0)
    max_pages_per_poll: int = Field(default=5, ge=1, le=50)
    max_failure_backoff_s: int = Field(default=900, ge=60)

    # --- Connectors
    demo_mode: bool = False  # R4: only seeds the demo connector as enabled on first registration
    connectors_enabled: str | None = None  # optional comma allowlist; unset = all registered
    backfill_hours: int = Field(default=48, ge=1, le=24 * 30)
    backfill_max: int = Field(default=50, ge=1, le=500)

    # --- Owner
    owner_timezone: str = "Asia/Kolkata"

    @cached_property
    def api_tokens(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.backend_api_tokens.split(",") if t.strip())

    @cached_property
    def connector_allowlist(self) -> frozenset[str] | None:
        if not self.connectors_enabled:
            return None
        return frozenset(k.strip() for k in self.connectors_enabled.split(",") if k.strip())

    @model_validator(mode="after")
    def _check_tokens(self) -> Settings:
        tokens = [t.strip() for t in self.backend_api_tokens.split(",") if t.strip()]
        if not tokens:
            raise ValueError("BACKEND_API_TOKENS must contain at least one token")
        if self.app_env == "production" and any(len(t) < 32 for t in tokens):
            raise ValueError(
                "BACKEND_API_TOKENS entries must be at least 32 characters in production"
            )
        return self
