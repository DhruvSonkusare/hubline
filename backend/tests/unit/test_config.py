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
