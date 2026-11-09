"""One live structured-output call with the configured (or given) provider: proves the key,
model and structured-output method work, and shows token use. Free-tier friendly: 1 request.

    uv run python -m scripts.llm_probe --provider groq
    uv run python -m scripts.llm_probe --provider google_genai --model gemini-3.5-flash-lite

Keys come from backend/.env (GROQ_API_KEY / GOOGLE_API_KEY); they are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.agent.llm import LLMError, build_llm, call_structured
from app.agent.prompts import triage_prompt
from app.agent.schemas import TriageResult
from app.agent.state import MessageForAgent, OwnerProfile, initial_state
from app.config import Settings

DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "google_genai": "gemini-3.5-flash-lite"}


async def main(args: argparse.Namespace) -> int:
    base = Settings()
    provider = args.provider or base.llm_provider
    model = args.model or DEFAULT_MODELS.get(provider, base.llm_model)
    settings = base.model_copy(update={"llm_provider": provider, "llm_model": model})
    try:
        llm = build_llm(settings, "triage")
    except LLMError as exc:
        print(f"FAILED {type(exc).__name__}: {exc}")
        return 2
    from datetime import UTC, datetime

    state = initial_state(
        job_kind="triage",
        message=MessageForAgent(
            id="probe",
            source="probe",
            source_display="Gmail",
            sender_id="priya@acme.example",
            sender_name="Priya Raman",
            subject="URGENT: checkout API 500s",
            thread_title=None,
            body="Checkout fails for 10% of requests since the deploy. Roll back? "
            "I need an answer in 30 minutes.",
            received_at=datetime.now(UTC),
        ),
        owner=OwnerProfile(settings.agent_owner_name, settings.agent_owner_context, "Asia/Kolkata"),
    )
    system, prompt = triage_prompt(state)
    chat_model = getattr(llm, "_model", None)
    print(
        f"provider={provider} model={model} method={getattr(llm, '_structured_kwargs', {})} "
        f"reasoning_effort={getattr(chat_model, 'reasoning_effort', None)} "
        f"max_tokens={getattr(chat_model, 'max_tokens', None)}"
    )
    start = time.monotonic()
    try:
        result = await call_structured(
            llm, TriageResult, system=system, prompt=prompt, max_tokens=400, parse_retries=0
        )
    except LLMError as exc:
        print(f"FAILED {type(exc).__name__}: {exc}")
        cause = exc.__cause__
        if cause is not None:
            print(f"  cause {type(cause).__name__}: {str(cause)[:500]}")
        return 1
    except Exception as exc:
        print(f"FAILED {type(exc).__name__}: {str(exc)[:500]}")
        return 1
    elapsed = time.monotonic() - start
    u = llm.usage
    print(f"OK in {elapsed:.1f}s: priority={result.priority} category={result.category}")
    print(f"   summary: {result.summary}")
    print(
        f"   tokens: in={u.input_tokens} out={u.output_tokens} "
        f"(of which reasoning={u.reasoning_tokens}, answer={u.output_tokens - u.reasoning_tokens})"
        f" total={u.total_tokens}"
    )
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider")
    p.add_argument("--model")
    sys.exit(asyncio.run(main(p.parse_args())))
