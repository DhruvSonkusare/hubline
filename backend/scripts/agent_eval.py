"""Run the triage (and a few drafts) against tests/fixtures/agent/samples.yaml with the REAL
model configured in backend/.env, with a hard spend cap.

    uv run python -m scripts.agent_eval                 # needs LLM_PROVIDER=anthropic + key
    uv run python -m scripts.agent_eval --budget 0.05 --max-drafts 4
    uv run python -m scripts.agent_eval --allow-fake    # dry run with the offline fake model

No database is used. Exit code 0 when at least --min-pass samples land in their expected band.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.agent.graph import build_graph, run_agent
from app.agent.llm import StructuredLLM, build_llm
from app.agent.state import MessageForAgent, OwnerProfile, initial_state
from app.config import Settings

SAMPLES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agent" / "samples.yaml"

# USD per million tokens (input, output) - Anthropic list prices, Oct 2026.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
FALLBACK_PRICE = (10.00, 50.00)  # unknown model: assume the most expensive tier
# Worst-case reserve before each call, so the cap is never crossed.
TRIAGE_RESERVE_USD = 0.003
DRAFT_RESERVE_USD = 0.02


def cost(llms: list[StructuredLLM]) -> float:
    total = 0.0
    for llm in llms:
        for model, (tin, tout) in llm.usage.by_model.items():
            pin, pout = (
                (0.0, 0.0) if model.startswith("fake") else PRICES.get(model, FALLBACK_PRICE)
            )
            total += tin / 1e6 * pin + tout / 1e6 * pout
    return total


def to_message(sample: dict[str, Any]) -> MessageForAgent:
    return MessageForAgent(
        id=sample["id"],
        source="eval",
        source_display=sample["source"],
        sender_id=sample["sender_id"],
        sender_name=sample.get("sender_name"),
        subject=sample.get("subject"),
        thread_title=None,
        body=sample["body"],
        received_at=datetime.now(UTC),
        can_send=True,
        headers={str(k).lower(): str(v) for k, v in (sample.get("headers") or {}).items()},
    )


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    if settings.llm_provider == "fake" and not args.allow_fake:
        print("LLM_PROVIDER is 'fake'. Set LLM_PROVIDER=anthropic and ANTHROPIC_API_KEY in")
        print("backend/.env to evaluate the real model, or pass --allow-fake for a dry run.")
        return 2
    samples = yaml.safe_load(SAMPLES.read_text(encoding="utf-8"))
    triage_llm, draft_llm = build_llm(settings, "triage"), build_llm(settings, "draft")
    graph = build_graph(triage_llm, draft_llm)
    owner = OwnerProfile(
        name=settings.agent_owner_name,
        context=settings.agent_owner_context,
        timezone=settings.owner_timezone,
        vips=settings.vip_senders,
    )
    print(
        f"provider={settings.llm_provider} triage={triage_llm.model_name} "
        f"draft={draft_llm.model_name} effort={settings.llm_effort} budget=${args.budget:.3f}"
    )
    print(f"{'id':<16} {'want':<6} {'got':<4} {'ok':<3} {'by':<6} summary")
    passed, drafts_done, ran = 0, 0, 0
    drafted: list[tuple[str, str]] = []
    for sample in samples:
        spent = cost([triage_llm, draft_llm])
        if spent + TRIAGE_RESERVE_USD > args.budget:
            print(f"-- budget reached (${spent:.4f}); stopping before {sample['id']}")
            break
        allow_draft = drafts_done < args.max_drafts and spent + DRAFT_RESERVE_USD <= args.budget
        state = initial_state(
            job_kind="triage", message=to_message(sample), owner=owner, allow_draft=allow_draft
        )
        try:
            out = await run_agent(graph, state)
        except Exception as exc:
            print(f"{sample['id']:<16} ERROR {type(exc).__name__}: {exc}")
            ran += 1
            continue
        ran += 1
        lo, hi = sample["expected"]
        t = out.get("triage")
        got = t.priority if t else None
        ok = got is not None and lo <= got <= hi
        passed += ok
        band = f"{lo}-{hi}" if lo != hi else str(lo)
        summary = (t.summary if t else out.get("skip_reason") or "")[:70]
        print(
            f"{sample['id']:<16} {band:<6} {got!s:<4} {'ok' if ok else 'NO':<3} "
            f"{out.get('triage_by') or '-':<6} {summary}"
        )
        draft = out.get("draft")
        if draft is not None:
            drafts_done += 1
            drafted.append((sample["id"], draft.body))

    if drafted:
        print("\n--- drafts")
        for sid, body in drafted:
            print(f"[{sid}]\n{body}\n")
    usage = [triage_llm.usage, draft_llm.usage]
    print(
        f"passed {passed}/{len(samples)} (ran {ran}); LLM calls "
        f"{sum(u.calls for u in usage)}; tokens in {sum(u.input_tokens for u in usage)} "
        f"out {sum(u.output_tokens for u in usage)}; "
        f"estimated cost ${cost([triage_llm, draft_llm]):.4f}"
    )
    return 0 if passed >= args.min_pass else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=float, default=0.05, help="hard cap in USD")
    parser.add_argument("--max-drafts", type=int, default=4)
    parser.add_argument("--min-pass", type=int, default=12)
    parser.add_argument("--allow-fake", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
