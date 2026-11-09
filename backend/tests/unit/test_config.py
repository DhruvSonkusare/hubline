import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_token_list_is_split_and_trimmed() -> None:
    s = make_settings(backend_api_tokens=" a-token , b-token ,")
    assert s.api_tokens == ("a-token", "b-token")


def test_empty_token_list_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(backend_api_tokens=" , ")


def test_production_requires_long_tokens() -> None:
    with pytest.raises(ValidationError, match="32 characters"):
        make_settings(app_env="production", backend_api_tokens="short")
    assert make_settings(app_env="production", backend_api_tokens="x" * 32).api_tokens


def test_defaults_are_idle_friendly_and_demo_off() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    # above Neon's 5-minute suspend delay, so an idle app lets the database scale to zero
    assert fields["scheduler_idle_max_s"].default > 300
    assert fields["worker_idle_max_s"].default > 300
    assert fields["demo_mode"].default is False
    assert fields["work_on_demand"].default is True
    assert fields["tick_secret"].default is None  # /internal/tick disabled unless configured
    assert fields["tick_on_startup"].default is False


def test_connector_allowlist() -> None:
    assert make_settings().connector_allowlist is None
    assert make_settings(connectors_enabled="demo, slack").connector_allowlist == {"demo", "slack"}


def test_llm_defaults_are_free() -> None:
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["llm_provider"].default == "fake"  # local dev + tests: offline, $0
    assert fields["llm_model"].default == "openai/gpt-oss-120b"  # Groq free tier in production
    assert fields["llm_effort"].default == "low"
    assert fields["llm_max_retries"].default == 0  # 429s reach the worker -> job deferred
    assert (fields["llm_rpm"].default, fields["llm_tpm"].default) == (30, 8000)
    assert fields["agent_max_jobs_per_hour"].default == 30  # bursts; token budget is the limit
    assert fields["backfill_auto_triage"].default == 10
    assert fields["agent_max_tokens_per_day"].default == 180_000
    assert fields["agent_concurrency"].default == 1
    s = make_settings()
    assert s.draft_model == s.llm_model
    assert make_settings(llm_draft_model="other").draft_model == "other"


def test_retry_delays_parsed_and_validated() -> None:
    assert make_settings(agent_retry_delays_s="1, 2.5,10").retry_delays == (1.0, 2.5, 10.0)
    for bad in ("", "a,b", "-1"):
        with pytest.raises(ValidationError, match="AGENT_RETRY_DELAYS_S"):
            make_settings(agent_retry_delays_s=bad)


def test_api_key_is_secret() -> None:
    s = make_settings(anthropic_api_key="sk-ant-secret-value")
    assert "sk-ant-secret-value" not in repr(s)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-secret-value"
