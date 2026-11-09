"""Swappable structured-output LLMs. Every real provider goes through LangChain init_chat_model.

LLM_PROVIDER:
  fake          deterministic offline model (app/agent/fake.py); default for local dev and tests
  groq          production default, free tier (openai/gpt-oss-120b, strict json_schema)
  google_genai  Gemini free tier (demo data only: Google may use free-tier prompts)
  anthropic     optional and paid: `uv sync --extra anthropic`
  <other>       any init_chat_model provider whose package is installed

Rate limits (all free tiers): a client-side RPM/TPM pacer per model keeps us inside the limits;
a provider 429 becomes LLMRateLimitError(retry_after) and the worker DEFERS the job (never fails
it). The rolling 24h token budget is enforced by the worker from the database.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from app.config import Settings

T = TypeVar("T", bound=BaseModel)
Purpose = Literal["triage", "draft"]

# Output caps per call. Reasoning tokens (gpt-oss, effort=low) count as output, so leave room.
MAX_TOKENS: dict[str, int] = {"triage": 400, "draft": 600}
# Groq models with strict (constrained-decoding) structured outputs (console.groq.com docs).
GROQ_STRICT_MODELS = (
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
    "meta-models/muse-spark-open",
)
DEFAULT_RETRY_AFTER_S = 60.0
MAX_PACING_WAIT_S = 20.0  # wait this long for a local RPM/TPM slot, otherwise defer the job


# ------------------------------------------------------------------------------ errors


class LLMError(Exception):
    """Base class for agent LLM failures."""


class LLMConfigError(LLMError):
    """Provider/model/key misconfigured; retrying will not help."""


class LLMRefusalError(LLMError):
    """The model declined. Not retried."""


class LLMOutputError(LLMError):
    """Output missing, truncated or not matching the schema."""


class LLMRateLimitError(LLMError):
    """Provider or local pacer says "not now". The job is deferred, never failed."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = max(1.0, retry_after)


# ------------------------------------------------------------------------------ usage


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, list[int]] = field(default_factory=dict)  # model -> [in, out]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        acc = self.by_model.setdefault(model, [0, 0])
        acc[0] += input_tokens
        acc[1] += output_tokens


# The worker sets a fresh Usage per job, so tokens are attributed even when a job fails midway.
job_usage: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("job_usage", default=None)


def record_usage(model: str, input_tokens: int, output_tokens: int, own: Usage) -> None:
    own.add(model, input_tokens, output_tokens)
    current = job_usage.get()
    if current is not None and current is not own:
        current.add(model, input_tokens, output_tokens)


class StructuredLLM(Protocol):
    model_name: str
    usage: Usage

    async def generate(
        self, schema: type[T], *, system: str, prompt: str, max_tokens: int
    ) -> T: ...


# ------------------------------------------------------------------------------ pacing


class RateLimiter:
    """Sliding 60-second window of requests and tokens for one model (single process)."""

    def __init__(
        self,
        rpm: int,
        tpm: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.rpm, self.tpm = rpm, tpm
        self._clock, self._sleep = clock, sleep
        self._events: deque[list[float]] = deque()  # [timestamp, tokens]
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] <= now - 60.0:
            self._events.popleft()

    def wait_needed(self, est_tokens: int, now: float) -> float:
        self._prune(now)
        events = list(self._events)
        calls = len(events)
        tokens = sum(int(e[1]) for e in events)
        if (not self.rpm or calls + 1 <= self.rpm) and (
            not self.tpm or tokens + est_tokens <= self.tpm or not events
        ):
            return 0.0
        # Drop the oldest events one by one until both limits fit.
        for i, (ts, tok) in enumerate(events):
            calls -= 1
            tokens -= int(tok)
            remaining = events[i + 1 :]
            if (not self.rpm or calls + 1 <= self.rpm) and (
                not self.tpm or tokens + est_tokens <= self.tpm or not remaining
            ):
                return max(0.0, ts + 60.0 - now)
        return 60.0  # pragma: no cover - loop always returns

    async def acquire(self, est_tokens: int, max_wait: float = MAX_PACING_WAIT_S) -> list[float]:
        async with self._lock:
            now = self._clock()
            wait = self.wait_needed(est_tokens, now)
            if wait > max_wait:
                raise LLMRateLimitError(f"local pacing: free-tier limit, wait {wait:.0f}s", wait)
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
                self._prune(now)
            entry = [now, float(est_tokens)]
            self._events.append(entry)
            return entry

    @staticmethod
    def settle(entry: list[float], actual_tokens: int) -> None:
        entry[1] = float(actual_tokens)


_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(provider: str, model: str, rpm: int, tpm: int) -> RateLimiter | None:
    if not rpm and not tpm:
        return None
    key = f"{provider}:{model}"
    if key not in _LIMITERS:
        _LIMITERS[key] = RateLimiter(rpm, tpm)
    return _LIMITERS[key]


def reset_limiters() -> None:
    _LIMITERS.clear()


# ------------------------------------------------------------------------------ error mapping

_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?")
_RETRY_IN_TEXT = re.compile(
    r"(?:try again in|retry in|retryDelay['\"]?\s*[:=]\s*['\"]?)\s*([0-9hms.]+)", re.I
)


def _duration_s(text: str) -> float | None:
    text = text.strip().strip("'\"")
    try:
        return float(text)
    except ValueError:
        pass
    m = _DURATION.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(seen) < 6:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _status(e: BaseException) -> int | None:
    for value in (
        getattr(e, "status_code", None),
        getattr(e, "code", None),
        getattr(getattr(e, "response", None), "status_code", None),
    ):
        if isinstance(value, int):
            return value
    return None


def retry_after_from(e: BaseException) -> float:
    headers = getattr(getattr(e, "response", None), "headers", None)
    if headers is not None:
        for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            raw = headers.get(name)
            if raw:
                parsed = _duration_s(str(raw))
                if parsed is not None:
                    return parsed
    m = _RETRY_IN_TEXT.search(str(e))
    if m:
        parsed = _duration_s(m.group(1).rstrip("."))
        if parsed is not None:
            return parsed
    return DEFAULT_RETRY_AFTER_S


def classify_error(exc: BaseException) -> LLMError | None:
    """Map provider/LangChain exceptions onto our error types (None = treat as transient)."""
    for e in _chain(exc):
        if isinstance(e, LLMError):
            return e
        status, text, name = _status(e), str(e), type(e).__name__
        if status == 429 or name == "RateLimitError" or "RESOURCE_EXHAUSTED" in text:
            return LLMRateLimitError(f"rate limited by provider: {text[:200]}", retry_after_from(e))
        if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
            return LLMConfigError(f"provider rejected the API key: {text[:200]}")
        if status == 404 or name == "NotFoundError":
            return LLMConfigError(f"model or endpoint not found: {text[:200]}")
        if status == 400 and ("json_validate_failed" in text or "tool_use_failed" in text):
            return LLMOutputError(f"output failed schema validation: {text[:200]}")
        if isinstance(e, ValidationError) or name == "OutputParserException":
            return LLMOutputError(f"output did not match schema: {text[:200]}")
    return None


# ------------------------------------------------------------------------------ LangChain


def _structured_kwargs(provider: str, model: str, method: str) -> dict[str, Any]:
    if method != "auto":
        return {"method": method}
    if provider == "groq":
        if model in GROQ_STRICT_MODELS:
            return {"method": "json_schema", "strict": True}
        return {"method": "function_calling"}
    if provider in ("google_genai", "anthropic"):
        return {"method": "json_schema"}
    return {}


class LangChainLLM:
    def __init__(
        self,
        provider: str,
        model: str,
        chat_model: Any,
        *,
        method: str = "auto",
        limiter: RateLimiter | None = None,
    ) -> None:
        self.provider = provider
        self.model_name = model
        self.usage = Usage()
        self._model = chat_model
        self._structured_kwargs = _structured_kwargs(provider, model, method)
        self._limiter = limiter
        self._runnables: dict[type[BaseModel], Any] = {}

    def _runnable(self, schema: type[BaseModel]) -> Any:
        if schema not in self._runnables:
            self._runnables[schema] = self._model.with_structured_output(
                schema, include_raw=True, **self._structured_kwargs
            )
        return self._runnables[schema]

    async def generate(self, schema: type[T], *, system: str, prompt: str, max_tokens: int) -> T:
        est_tokens = (len(system) + len(prompt)) // 3 + max_tokens // 2
        entry = await self._limiter.acquire(est_tokens) if self._limiter else None
        try:
            out = await self._runnable(schema).ainvoke([("system", system), ("human", prompt)])
        except Exception as exc:
            if entry is not None:
                RateLimiter.settle(entry, est_tokens)
            mapped = classify_error(exc)
            if mapped is None:
                raise
            raise mapped from exc

        raw, parsed, parsing_error = out.get("raw"), out.get("parsed"), out.get("parsing_error")
        usage = getattr(raw, "usage_metadata", None) or {}
        tin, tout = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        record_usage(self.model_name, tin, tout, self.usage)
        if entry is not None:
            RateLimiter.settle(entry, tin + tout or est_tokens)

        meta = getattr(raw, "response_metadata", None) or {}
        stop = meta.get("stop_reason") or meta.get("finish_reason")
        if stop == "refusal":
            raise LLMRefusalError("model declined")
        if parsed is None or parsing_error is not None:
            if stop in ("length", "max_tokens", "MAX_TOKENS"):
                raise LLMOutputError(f"output truncated at max_tokens={max_tokens}")
            raise LLMOutputError(f"output did not match schema: {parsing_error}")
        if not isinstance(parsed, schema):
            raise LLMOutputError(f"expected {schema.__name__}, got {type(parsed).__name__}")
        return parsed


def _provider_kwargs(settings: Settings, provider: str, model: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    key = {
        "groq": settings.groq_api_key,
        "google_genai": settings.google_api_key,
        "anthropic": settings.anthropic_api_key,
    }.get(provider)
    if key is not None:
        kwargs["api_key"] = key.get_secret_value()
    if settings.llm_temperature is not None and provider != "anthropic":
        kwargs["temperature"] = settings.llm_temperature
    if provider == "groq" and model.startswith(("openai/gpt-oss", "qwen/")):
        kwargs["reasoning_effort"] = settings.llm_effort
    elif provider == "anthropic":
        kwargs["output_config"] = {"effort": settings.llm_effort}
    return kwargs


def build_llm(settings: Settings, purpose: Purpose) -> StructuredLLM:
    model = settings.llm_model if purpose == "triage" else settings.draft_model
    provider = settings.llm_provider.strip().lower()
    if provider == "fake":
        from app.agent.fake import FakeLLM

        return FakeLLM(model_name=f"fake:{purpose}")
    try:
        chat_models = importlib.import_module("langchain.chat_models")
        chat_model = chat_models.init_chat_model(
            model,
            model_provider=provider,
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            max_tokens=MAX_TOKENS[purpose],
            **_provider_kwargs(settings, provider, model),
        )
    except ImportError as exc:
        hint = (
            "uv sync --extra anthropic"
            if provider == "anthropic"
            else f"uv add langchain-{provider.replace('_', '-')}"
        )
        raise LLMConfigError(f"LLM_PROVIDER={provider!r} needs its package: {hint}") from exc
    except Exception as exc:  # unknown provider, missing API key, bad option ...
        key_var = {"groq": "GROQ_API_KEY", "google_genai": "GOOGLE_API_KEY"}.get(
            provider, f"the {provider} API key"
        )
        raise LLMConfigError(
            f"could not create {provider} model {model!r} (is {key_var} set in backend/.env?): "
            f"{type(exc).__name__}: {str(exc)[:200]}"
        ) from exc
    return LangChainLLM(
        provider,
        model,
        chat_model,
        method=settings.llm_structured_method,
        limiter=limiter_for(provider, model, settings.llm_rpm, settings.llm_tpm),
    )


async def call_structured[M: BaseModel](
    llm: StructuredLLM,
    schema: type[M],
    *,
    system: str,
    prompt: str,
    max_tokens: int,
    parse_retries: int = 1,
) -> M:
    """generate() plus `parse_retries` extra attempts that feed the validation error back."""
    attempt_prompt = prompt
    for attempt in range(parse_retries + 1):
        try:
            return await llm.generate(
                schema, system=system, prompt=attempt_prompt, max_tokens=max_tokens
            )
        except LLMOutputError as exc:
            if attempt >= parse_retries:
                raise
            attempt_prompt = (
                f"{prompt}\n\nYour previous answer was invalid ({exc}). "
                "Answer again, matching the required structure exactly."
            )
    raise AssertionError("unreachable")  # pragma: no cover
