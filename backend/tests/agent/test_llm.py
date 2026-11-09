"""LLM layer: LangChain adapter (stub chat model, no network), provider error mapping with the
real provider exception classes, provider kwargs, and the RPM/TPM pacer."""

from __future__ import annotations

from typing import Any

import groq
import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_core.messages import AIMessage

from app.agent.fake import FakeLLM
from app.agent.llm import (
    LangChainLLM,
    LLMConfigError,
    LLMOutputError,
    LLMRateLimitError,
    RateLimiter,
    Usage,
    build_llm,
    call_structured,
    classify_error,
    job_usage,
    retry_after_from,
)
from app.agent.schemas import TriageResult
from tests.conftest import make_settings

GOOD = TriageResult(priority=4, summary="s", needs_reply=True, category="question", reason="r")


def raw(stop: str = "stop", tin: int = 120, tout: int = 30) -> AIMessage:
    return AIMessage(
        content="{}",
        usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout},
        response_metadata={"finish_reason": stop},
    )


class StubRunnable:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.calls: list[Any] = []

    async def ainvoke(self, messages: Any) -> Any:
        self.calls.append(messages)
        item = self.outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class StubChatModel:
    def __init__(self, *outputs: Any) -> None:
        self.runnable = StubRunnable(list(outputs))
        self.structured_calls: list[dict[str, Any]] = []

    def with_structured_output(self, schema: Any, **kwargs: Any) -> StubRunnable:
        self.structured_calls.append({"schema": schema, **kwargs})
        return self.runnable


def ok(parsed: Any = GOOD, **kw: Any) -> dict[str, Any]:
    return {"raw": raw(**kw), "parsed": parsed, "parsing_error": None}


def make(provider: str = "groq", model: str = "openai/gpt-oss-120b", *outputs: Any):  # type: ignore[no-untyped-def]
    stub = StubChatModel(*outputs)
    return LangChainLLM(provider, model, stub), stub


# ------------------------------------------------------------------------------ adapter


async def test_generate_returns_parsed_and_counts_usage_globally_and_per_job() -> None:
    llm, stub = make("groq", "openai/gpt-oss-120b", ok())
    per_job = Usage()
    token = job_usage.set(per_job)
    try:
        out = await llm.generate(TriageResult, system="sys", prompt="hello", max_tokens=400)
    finally:
        job_usage.reset(token)
    assert out == GOOD
    assert stub.runnable.calls[0] == [("system", "sys"), ("human", "hello")]
    assert (llm.usage.input_tokens, llm.usage.output_tokens) == (120, 30)
    assert (per_job.calls, per_job.total_tokens) == (1, 150)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("groq", "openai/gpt-oss-120b", {"method": "json_schema", "strict": True}),
        ("groq", "llama-3.1-8b-instant", {"method": "function_calling"}),
        ("google_genai", "gemini-3.5-flash-lite", {"method": "json_schema"}),
        ("anthropic", "claude-haiku-5-5", {"method": "json_schema"}),
        ("ollama", "llama3", {}),
    ],
)
async def test_structured_output_method_per_provider(
    provider: str, model: str, expected: dict[str, Any]
) -> None:
    llm, stub = make(provider, model, ok())
    await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)
    call = stub.structured_calls[0]
    assert call["include_raw"] is True
    assert {k: v for k, v in call.items() if k not in ("schema", "include_raw")} == expected


async def test_parsing_error_and_truncation_are_output_errors() -> None:
    llm, _ = make(
        "groq",
        "openai/gpt-oss-120b",
        {"raw": raw(), "parsed": None, "parsing_error": ValueError("bad json")},
        {"raw": raw(stop="length"), "parsed": None, "parsing_error": ValueError("cut")},
    )
    with pytest.raises(LLMOutputError, match="did not match schema"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)
    with pytest.raises(LLMOutputError, match="truncated"):
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)


async def test_call_structured_retries_output_errors_once() -> None:
    llm, stub = make(
        "groq",
        "openai/gpt-oss-120b",
        {"raw": raw(), "parsed": None, "parsing_error": ValueError("x")},
        ok(),
    )
    assert await call_structured(llm, TriageResult, system="s", prompt="p", max_tokens=10) == GOOD
    assert "previous answer was invalid" in stub.runnable.calls[1][1][1]


# ------------------------------------------------------------------------------ error mapping


def _groq_error(cls: type[groq.APIStatusError], status: int, headers: dict[str, str]) -> Any:
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status, headers=headers, request=request)
    return cls("Rate limit reached for model `openai/gpt-oss-120b`", response=response, body=None)


async def test_groq_429_becomes_rate_limit_with_retry_after() -> None:
    err = _groq_error(groq.RateLimitError, 429, {"retry-after": "17"})
    llm, _ = make("groq", "openai/gpt-oss-120b", err)
    with pytest.raises(LLMRateLimitError) as info:
        await llm.generate(TriageResult, system="s", prompt="p", max_tokens=10)
    assert info.value.retry_after == 17


def test_groq_reset_header_duration_is_used_without_retry_after() -> None:
    err = _groq_error(groq.RateLimitError, 429, {"x-ratelimit-reset-tokens": "2m59.5s"})
    assert retry_after_from(err) == pytest.approx(179.5)


def test_gemini_resource_exhausted_with_retry_delay() -> None:
    err = genai_errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}
                ],
            }
        },
    )
    mapped = classify_error(err)
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after == 12


def test_rate_limit_wrapped_by_another_exception_is_found_via_cause() -> None:
    inner = _groq_error(groq.RateLimitError, 429, {"retry-after": "5"})
    try:
        try:
            raise inner
        except Exception as e:
            raise RuntimeError("langchain wrapper") from e
    except RuntimeError as outer:
        mapped = classify_error(outer)
    assert isinstance(mapped, LLMRateLimitError)
    assert mapped.retry_after == 5


def test_auth_and_unknown_errors() -> None:
    assert isinstance(
        classify_error(_groq_error(groq.AuthenticationError, 401, {})), LLMConfigError
    )
    assert isinstance(classify_error(_groq_error(groq.NotFoundError, 404, {})), LLMConfigError)
    assert classify_error(RuntimeError("connection reset")) is None  # transient: job retry


def test_missing_retry_hint_defaults_to_a_minute() -> None:
    assert retry_after_from(RuntimeError("slow down")) == 60.0
    assert retry_after_from(RuntimeError("Please try again in 7m12.5s.")) == pytest.approx(432.5)


# ------------------------------------------------------------------------------ provider wiring


def test_fake_is_the_default_provider() -> None:
    assert isinstance(build_llm(make_settings(), "triage"), FakeLLM)


def test_groq_wiring() -> None:
    s = make_settings(llm_provider="groq", groq_api_key="gsk_test", llm_rpm=30, llm_tpm=8000)
    llm = build_llm(s, "triage")
    assert isinstance(llm, LangChainLLM)
    model: Any = llm._model
    assert type(model).__name__ == "ChatGroq"
    assert model.model_name == "openai/gpt-oss-120b"
    assert model.reasoning_effort == "low"
    assert model.max_tokens == 400
    assert model.max_retries == 0
    assert llm._limiter is not None
    assert build_llm(s, "draft")._limiter is llm._limiter  # type: ignore[attr-defined]


def test_gemini_wiring() -> None:
    s = make_settings(
        llm_provider="google_genai", llm_model="gemini-3.5-flash-lite", google_api_key="AIza-test"
    )
    llm = build_llm(s, "draft")
    assert isinstance(llm, LangChainLLM)
    assert type(llm._model).__name__ == "ChatGoogleGenerativeAI"
    assert llm._structured_kwargs == {"method": "json_schema"}


def test_anthropic_is_optional_and_never_gets_temperature() -> None:
    pytest.importorskip("langchain_anthropic")
    s = make_settings(
        llm_provider="anthropic",
        llm_model="claude-haiku-5-5",
        anthropic_api_key="sk-ant-test",
        llm_temperature=0.2,
    )
    model: Any = build_llm(s, "triage")._model  # type: ignore[attr-defined]
    assert model.temperature is None
    assert model.output_config == {"effort": "low"}


def test_unknown_provider_is_a_config_error() -> None:
    with pytest.raises(LLMConfigError):
        build_llm(make_settings(llm_provider="definitely-not-a-provider"), "triage")


# ------------------------------------------------------------------------------ pacer


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_pacer_waits_for_rpm_slot() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rpm=2, tpm=0, clock=clock, sleep=clock.sleep)
    await limiter.acquire(10)
    clock.now += 10
    await limiter.acquire(10)
    clock.now += 5
    await limiter.acquire(10, max_wait=60)  # 3rd call in a minute: wait for 1st to age out
    assert clock.slept == [pytest.approx(45)]


async def test_pacer_counts_actual_tokens_and_defers_long_waits() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rpm=0, tpm=1000, clock=clock, sleep=clock.sleep)
    entry = await limiter.acquire(100)
    RateLimiter.settle(entry, 900)  # the call actually used 900 tokens
    with pytest.raises(LLMRateLimitError) as info:
        await limiter.acquire(300, max_wait=10)  # would need ~60 s: defer instead
    assert info.value.retry_after == pytest.approx(60)
    clock.now += 61
    await limiter.acquire(300, max_wait=10)  # window cleared
    assert clock.slept == []


async def test_pacer_allows_a_single_oversized_request_when_window_empty() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rpm=0, tpm=100, clock=clock, sleep=clock.sleep)
    await limiter.acquire(500)
    assert clock.slept == []


def test_missing_api_key_is_a_clear_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(LLMConfigError, match="GROQ_API_KEY"):
        build_llm(make_settings(llm_provider="groq"), "triage")


def test_reasoning_tokens_extracted_from_langchain_or_raw_usage() -> None:
    from app.agent.llm import reasoning_tokens_from

    lc = {"input_tokens": 10, "output_tokens": 90, "output_token_details": {"reasoning": 60}}
    assert reasoning_tokens_from(lc, {}) == 60
    groq_raw = {"token_usage": {"completion_tokens_details": {"reasoning_tokens": 33}}}
    assert reasoning_tokens_from({"output_tokens": 50}, groq_raw) == 33
    assert reasoning_tokens_from({}, {}) == 0


async def test_generate_records_reasoning_tokens() -> None:
    msg = AIMessage(
        content="{}",
        usage_metadata={
            "input_tokens": 300,
            "output_tokens": 120,
            "total_tokens": 420,
            "output_token_details": {"reasoning": 80},
        },
        response_metadata={"finish_reason": "stop"},
    )
    llm, _ = make(
        "groq", "openai/gpt-oss-120b", {"raw": msg, "parsed": GOOD, "parsing_error": None}
    )
    await llm.generate(TriageResult, system="s", prompt="p", max_tokens=400)
    assert (llm.usage.output_tokens, llm.usage.reasoning_tokens) == (120, 80)
