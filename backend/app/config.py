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

    # --- Background work (R3: idle-friendly, switchable). Idle sleeps default above Neon's
    #     5-minute suspend delay so an idle app lets the database scale to zero.
    #     On Render free (production) both loops are OFF: work runs via /internal/tick,
    #     TICK_ON_STARTUP and on-demand kicks from API actions (see app/workers/runner.py).
    enable_scheduler: bool = True
    enable_agent_worker: bool = True
    scheduler_min_sleep_s: float = Field(default=1.0, gt=0)
    scheduler_idle_max_s: float = Field(default=600.0, gt=0)
    worker_idle_max_s: float = Field(default=600.0, gt=0)
    work_on_demand: bool = True  # loops off: API actions run one scheduler/agent pass
    tick_secret: SecretStr | None = None  # enables POST /internal/tick (X-Tick-Secret header)
    tick_budget_s: float = Field(default=60.0, gt=0, le=600)  # max agent drain per tick
    tick_on_startup: bool = False  # run one tick when the process starts (Render cold start)
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
    # Backfill is stored in full, but only the newest N per connector are auto-triaged; the rest
    # are agent_status="skipped" (triage on request). Protects the free daily token budget.
    backfill_auto_triage: int = Field(default=10, ge=0, le=500)

    # --- Owner
    owner_timezone: str = "Asia/Kolkata"

    # --- LLM. Every provider goes through LangChain init_chat_model.
    #     "fake" (default) = deterministic offline model for local dev and tests ($0, no key).
    #     "groq" = production default (free tier). "google_genai" = Gemini free tier (demo data
    #     only: free-tier prompts may be used by Google). "anthropic" = optional, paid
    #     (`uv sync --extra anthropic`). Any other init_chat_model provider also works.
    llm_provider: str = "fake"
    llm_model: str = "openai/gpt-oss-120b"  # triage
    llm_draft_model: str | None = None  # drafts; defaults to llm_model
    llm_effort: Literal["low", "medium", "high"] = "low"  # reasoning effort where supported
    llm_temperature: float | None = None  # None = provider default (never sent to Anthropic)
    llm_structured_method: Literal["auto", "json_schema", "function_calling", "json_mode"] = "auto"
    llm_timeout_s: float = Field(default=60.0, gt=0)
    # 0: surface 429s immediately so the job is deferred with retry-after (never failed).
    llm_max_retries: int = Field(default=0, ge=0, le=10)
    # Client-side pacing per model (free-tier limits of the default Groq model). 0 = off.
    llm_rpm: int = Field(default=30, ge=0)
    llm_tpm: int = Field(default=8000, ge=0)
    groq_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

    # --- Agent worker
    agent_concurrency: int = Field(default=1, ge=1, le=10)
    # Budget guards (free tier): see reports/step-2-report.md for the arithmetic.
    # burst shaping only; the rolling 24h token budget + per-minute pacer are the real limits
    agent_max_jobs_per_hour: int = Field(default=30, ge=0)  # 0 = pause the agent
    agent_max_tokens_per_day: int = Field(default=180_000, ge=0)  # rolling 24h; 0 = off
    agent_body_chars: int = Field(default=2000, ge=200)  # message text sent to the LLM
    agent_thread_item_chars: int = Field(default=300, ge=50)
    agent_retry_delays_s: str = "30,120,600"  # wait before attempt 2, 3, ...
    agent_job_timeout_s: float = Field(default=180.0, gt=0)
    agent_stale_after_s: int = Field(default=600, ge=60)  # running longer than this = crashed
    agent_maintenance_interval_s: int = Field(default=900, ge=60)
    worker_min_sleep_s: float = Field(default=2.0, gt=0)
    agent_thread_context: int = Field(default=3, ge=0, le=50)
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
