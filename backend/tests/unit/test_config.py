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
    s = make_settings()
    assert s.scheduler_idle_max_s == 60
    assert s.worker_idle_max_s == 60
    from app.config import Settings

    assert Settings.model_fields["demo_mode"].default is False


def test_connector_allowlist() -> None:
    assert make_settings().connector_allowlist is None
    assert make_settings(connectors_enabled="demo, slack").connector_allowlist == {"demo", "slack"}


def test_llm_defaults_are_offline_and_cheap() -> None:
    s = make_settings()
    assert s.llm_provider == "fake"
    assert s.llm_effort == "low"
    assert s.draft_model == s.llm_model == "claude-haiku-5-5"
    assert make_settings(llm_draft_model="claude-sonnet-5-5").draft_model == "claude-sonnet-5-5"
    assert s.agent_max_jobs_per_hour == 30


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
