"""Guards for the prepared deploy files (Step 4): $0 plan, no loops on Render, known env names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.config import Settings

REPO = Path(__file__).resolve().parents[3]


def _service() -> dict[str, Any]:
    blueprint = yaml.safe_load((REPO / "render.yaml").read_text(encoding="utf-8"))
    services = blueprint["services"]
    assert len(services) == 1
    service: dict[str, Any] = services[0]
    return service


def _env() -> dict[str, dict[str, Any]]:
    return {e["key"]: e for e in _service()["envVars"]}


def test_render_service_is_free_singapore_docker() -> None:
    s = _service()
    assert s["plan"] == "free"
    assert s["region"] == "singapore"
    assert s["runtime"] == "docker"
    assert s["numInstances"] == 1
    assert s["healthCheckPath"] == "/healthz"
    assert (REPO / s["dockerfilePath"]).is_file()
    assert (REPO / s["dockerContext"] / "uv.lock").is_file()


def test_render_runs_without_background_loops() -> None:
    env = _env()
    assert env["ENABLE_SCHEDULER"]["value"] == "false"
    assert env["ENABLE_AGENT_WORKER"]["value"] == "false"
    assert env["WORK_ON_DEMAND"]["value"] == "true"
    assert env["TICK_ON_STARTUP"]["value"] == "true"
    assert env["TICK_SECRET"].get("generateValue") is True
    assert env["DEMO_MODE"]["value"] == "false"


def test_render_uses_a_free_llm_and_no_paid_keys() -> None:
    env = _env()
    assert env["LLM_PROVIDER"]["value"] in {"groq", "google_genai", "fake"}
    assert "ANTHROPIC_API_KEY" not in env


def test_render_secrets_are_prompted_not_committed() -> None:
    env = _env()
    for key in ("DATABASE_URL", "BACKEND_API_TOKENS", "GROQ_API_KEY"):
        assert env[key].get("sync") is False
        assert "value" not in env[key]


def test_render_env_keys_are_settings_fields() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    unknown = sorted(set(_env()) - fields)
    assert unknown == []


def test_dockerignore_keeps_secrets_out_of_the_image() -> None:
    lines = (REPO / "backend" / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in lines
    assert ".env.*" in lines
