"""LLM adapters. The Anthropic adapter is exercised against a stub client (no network)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from app.agent.fake import FakeLLM
from app.agent.llm import (
    AnthropicLLM,
    LLMConfigError,
    LLMOutputError,
    LLMRefusalError,
    build_llm,
    call_structured,
)
from app.agent.schemas import TriageResult
from tests.conftest import make_settings

GOOD = TriageResult(priority=4, summary="s", needs_reply=True, category="question", reason="r")


@dataclass
class StubMessages:
    responses: list[Any]
    calls: list[dict[str, Any]]

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def response(
    parsed: Any = GOOD, stop_reason: str = "end_turn", in_tok: int = 100, out_tok: int = 20
) -> Any:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


def make(*responses: Any) -> tuple[AnthropicLLM, StubMessages]:
    stub = StubMessages(list(responses), [])
    llm = AnthropicLLM(
        "claude-haiku-5-5",
        api_key="sk-test",
        effort="low",
        client=SimpleNamespace(messages=stub),
    )
    return llm, stub


async def test_request_shape_uses_effort_and_no_sampling_params() -> None:
    llm, stub = make(response())
    out = await llm.generate(TriageResult, system="sys", prompt="hello", max_tokens=2000)
    assert out == GOOD
    call = stub.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_format"] is TriageResult
    assert call["output_config"] == {"effort": "low"}
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hello"}]
    assert "temperature" not in call  # current models reject sampling params
    assert "thinking" not in call
    assert llm.usage.input_tokens == 100
    assert llm.usage.output_tokens == 20
    assert llm.usage.by_model == {"claude-haiku-5-5": [100, 20]}


async def test_refusal_raises_refusal_error_and_still_counts_usage() -> None:
    llm, _ = make(response(parsed=None, stop_reason="refusal"))
    with pytest.raises(LLMRefusalError, match="cyber"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)
    assert llm.usage.calls == 1


async def test_truncation_and_missing_output_are_output_errors() -> None:
    llm, _ = make(response(stop_reason="max_tokens"), response(parsed=None))
    with pytest.raises(LLMOutputError, match="truncated"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)
    with pytest.raises(LLMOutputError, match="no structured output"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)


async def test_schema_mismatch_in_parse_is_output_error() -> None:
    llm, _ = make(ValueError("invalid json"))
    with pytest.raises(LLMOutputError):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)


async def test_auth_error_is_config_error() -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx2.Response(401, request=req), body=None
    )
    llm, _ = make(err)
    with pytest.raises(LLMConfigError, match="auth"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)


async def test_call_structured_retries_output_errors_once() -> None:
    llm, stub = make(response(stop_reason="max_tokens"), response())
    assert await call_structured(llm, TriageResult, system="s", prompt="p", max_tokens=10) == GOOD
    assert "previous answer was invalid" in stub.calls[1]["messages"][0]["content"]


def test_build_llm_selects_provider() -> None:
    fake = build_llm(make_settings(), "triage")
    assert isinstance(fake, FakeLLM)

    s = make_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        llm_draft_model="claude-sonnet-5-5",
    )
    triage, draft = build_llm(s, "triage"), build_llm(s, "draft")
    assert isinstance(triage, AnthropicLLM)
    assert triage.model_name == "claude-haiku-5-5"
    assert draft.model_name == "claude-sonnet-5-5"


def test_unknown_provider_without_langchain_is_a_clear_config_error() -> None:
    with pytest.raises(LLMConfigError, match="uv add langchain"):
        build_llm(make_settings(llm_provider="openai", llm_model="gpt-x"), "triage")
