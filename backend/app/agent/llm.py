"""Swappable structured-output LLMs behind one tiny interface.

LLM_PROVIDER:
  fake       deterministic offline model (app/agent/fake.py); default, never spends money
  anthropic  official Anthropic SDK, structured outputs via messages.parse(output_format=...),
             output_config.effort (no temperature: current models reject sampling params)
  <other>    LangChain init_chat_model(model, model_provider=<other>) + with_structured_output;
             needs `langchain` and the provider package installed (not bundled)

Transport retries (429/5xx/529/network) are done by the provider client (LLM_MAX_RETRIES).
call_structured() adds one retry when the output fails schema validation; job-level retries
live in the worker.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from app.config import Settings

T = TypeVar("T", bound=BaseModel)
Purpose = Literal["triage", "draft"]
Effort = Literal["low", "medium", "high"]


class LLMError(Exception):
    """Base class for agent LLM failures."""


class LLMConfigError(LLMError):
    """Provider/model misconfigured; retrying will not help."""


class LLMRefusalError(LLMError):
    """The model declined (stop_reason=refusal). Not retried."""


class LLMOutputError(LLMError):
    """Output missing, truncated or not matching the schema."""


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, list[int]] = field(default_factory=dict)  # model -> [in, out]

    def add(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        acc = self.by_model.setdefault(model, [0, 0])
        acc[0] += input_tokens
        acc[1] += output_tokens


class StructuredLLM(Protocol):
    model_name: str
    usage: Usage

    async def generate(
        self, schema: type[T], *, system: str, prompt: str, max_tokens: int
    ) -> T: ...


# ------------------------------------------------------------------------------ anthropic


class AnthropicLLM:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None,
        effort: Effort = "low",
        timeout_s: float = 60.0,
        max_retries: int = 3,
        client: Any | None = None,
    ) -> None:
        import anthropic

        self.model_name = model
        self.effort = effort
        self.usage = Usage()
        # api_key=None lets the SDK resolve credentials itself (env var or `ant auth` profile).
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )

    async def generate(self, schema: type[T], *, system: str, prompt: str, max_tokens: int) -> T:
        import anthropic
        from anthropic.types import OutputConfigParam

        output_config = cast(OutputConfigParam, {"effort": self.effort})
        try:
            response = await self._client.messages.parse(
                model=self.model_name,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config=output_config,
            )
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"output did not match schema: {exc}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LLMConfigError(f"Anthropic auth failed: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMConfigError(f"unknown model {self.model_name!r}: {exc.message}") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.add(self.model_name, usage.input_tokens or 0, usage.output_tokens or 0)

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMRefusalError(f"model declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise LLMOutputError(f"output truncated at max_tokens={max_tokens}")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMOutputError("no structured output in response")
        if not isinstance(parsed, schema):
            raise LLMOutputError(f"expected {schema.__name__}, got {type(parsed).__name__}")
        return parsed


# ------------------------------------------------------------------------------ langchain


class LangChainLLM:
    """Any LangChain-supported provider. Imported lazily; not installed by default."""

    def __init__(self, provider: str, model: str, *, timeout_s: float, max_retries: int) -> None:
        try:
            chat_models = importlib.import_module("langchain.chat_models")
        except ImportError as exc:
            raise LLMConfigError(
                f"LLM_PROVIDER={provider!r} needs `uv add langchain langchain-{provider}`"
            ) from exc
        self.model_name = model
        self.usage = Usage()
        self._model = chat_models.init_chat_model(
            model, model_provider=provider, timeout=timeout_s, max_retries=max_retries
        )

    async def generate(self, schema: type[T], *, system: str, prompt: str, max_tokens: int) -> T:
        runnable = self._model.with_structured_output(schema)
        try:
            result = await runnable.ainvoke([("system", system), ("human", prompt)])
        except (ValidationError, ValueError) as exc:
            raise LLMOutputError(f"output did not match schema: {exc}") from exc
        self.usage.calls += 1
        if not isinstance(result, schema):
            raise LLMOutputError(f"expected {schema.__name__}, got {type(result).__name__}")
        return result


# ------------------------------------------------------------------------------ factory


def build_llm(settings: Settings, purpose: Purpose) -> StructuredLLM:
    model = settings.llm_model if purpose == "triage" else settings.draft_model
    provider = settings.llm_provider.strip().lower()
    if provider == "fake":
        from app.agent.fake import FakeLLM

        return FakeLLM(model_name=f"fake:{purpose}")
    if provider == "anthropic":
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        return AnthropicLLM(
            model,
            api_key=key,
            effort=settings.llm_effort,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
        )
    return LangChainLLM(
        provider, model, timeout_s=settings.llm_timeout_s, max_retries=settings.llm_max_retries
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
