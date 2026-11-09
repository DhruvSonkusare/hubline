"""Application settings. Every value comes from environment variables (or backend/.env locally).

Never read os.environ directly elsewhere in the app; add a typed field here instead.
"""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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

    # --- LLM (Step 2). "fake" = deterministic offline model (default: never spends money);
    #     "anthropic" = official Anthropic SDK; anything else = LangChain init_chat_model provider.
    llm_provider: str = "fake"
    llm_model: str = "claude-haiku-5-5"  # triage
    llm_draft_model: str | None = None  # drafts; defaults to llm_model
    llm_effort: Literal["low", "medium", "high"] = "low"  # Anthropic output_config.effort
    llm_timeout_s: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)  # transport retries (429/5xx/network)
    anthropic_api_key: SecretStr | None = None

    # --- Agent worker
    agent_concurrency: int = Field(default=2, ge=1, le=10)
    agent_max_jobs_per_hour: int = Field(default=30, ge=0)  # budget guard; 0 = pause the agent
    agent_retry_delays_s: str = "30,120,600"  # wait before attempt 2, 3, ...
    agent_job_timeout_s: float = Field(default=180.0, gt=0)
    agent_stale_after_s: int = Field(default=600, ge=60)  # running longer than this = crashed
    agent_maintenance_interval_s: int = Field(default=900, ge=60)
    worker_min_sleep_s: float = Field(default=2.0, gt=0)
    agent_thread_context: int = Field(default=8, ge=0, le=50)
    agent_owner_name: str = "Dhruv Sonkusare"
    agent_owner_context: str = "Python backend developer"
    agent_vip_senders: str = ""  # comma list of sender ids / emails

    @cached_property
    def api_tokens(self) -> tuple[str, ...]:
        return tuple(t.strip() for t in self.backend_api_tokens.split(",") if t.strip())

    @cached_property
    def retry_delays(self) -> tuple[float, ...]:
        return tuple(float(x) for x in self.agent_retry_delays_s.split(",") if x.strip())

    @cached_property
    def vip_senders(self) -> tuple[str, ...]:
        return tuple(v.strip().lower() for v in self.agent_vip_senders.split(",") if v.strip())

    @property
    def draft_model(self) -> str:
        return self.llm_draft_model or self.llm_model

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
        try:
            delays = [float(x) for x in self.agent_retry_delays_s.split(",") if x.strip()]
        except ValueError:
            raise ValueError("AGENT_RETRY_DELAYS_S must be a comma list of seconds") from None
        if not delays or any(d < 0 for d in delays):
            raise ValueError("AGENT_RETRY_DELAYS_S must contain non-negative numbers")
        return self
