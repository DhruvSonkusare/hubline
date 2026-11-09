"""Run triage (and a few drafts) on tests/fixtures/agent/samples.yaml with a REAL free-tier model,
capped in TOKENS so it never eats the daily free allowance.

    uv run python -m scripts.agent_eval --provider groq          # needs GROQ_API_KEY in .env
    uv run python -m scripts.agent_eval --provider google_genai --model gemini-3.5-flash-lite
    uv run python -m scripts.agent_eval --allow-fake             # offline dry run

Calls are paced by the client-side RPM/TPM limiter (LLM_RPM / LLM_TPM); a provider 429 waits
retry-after and retries the sample once. No database is used. Exit code 0 when at least
--min-pass samples land in their expected priority band.
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
from app.agent.llm import LLMRateLimitError, build_llm
from app.agent.schemas import is_sender_directed
from app.agent.state import MessageForAgent, OwnerProfile, initial_state
from app.config import Settings

SAMPLES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agent" / "samples.yaml"
DEFAULT_MODELS = {"groq": "openai/gpt-oss-120b", "google_genai": "gemini-3.5-flash-lite"}
TRIAGE_RESERVE_TOKENS = 1_500  # worst case for one triage; stop before crossing the cap
DRAFT_RESERVE_TOKENS = 2_000
MAX_RATE_LIMIT_WAIT_S = 90.0


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
    base = Settings()
    provider = args.provider or base.llm_provider
    model = args.model or DEFAULT_MODELS.get(provider, base.llm_model)
    settings = base.model_copy(update={"llm_provider": provider, "llm_model": model})
    if provider == "fake" and not args.allow_fake:
        print("Provider is 'fake'. Pass --provider groq (GROQ_API_KEY in backend/.env) or")
        print("--provider google_genai (GOOGLE_API_KEY), or --allow-fake for a dry run.")
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

    def used() -> int:
        return triage_llm.usage.total_tokens + draft_llm.usage.total_tokens

    print(
        f"provider={provider} model={model} effort={settings.llm_effort} "
        f"token cap={args.max_tokens} pacing={settings.llm_rpm} rpm/{settings.llm_tpm} tpm"
    )
    print(
        f"{'id':<16} {'want':<5} {'got':<3} {'ok':<3} {'by':<5} "
        f"{'tri in/out/rsn':>15} {'draft in/out/rsn':>17} {'total':>6}  summary"
    )
    passed, drafts_done, ran = 0, 0, 0
    drafted: list[tuple[str, str, list[str]]] = []
    per_message: list[dict[str, int]] = []  # measured tokens per message (for the budget maths)

    def snap() -> tuple[int, ...]:
        t, d = triage_llm.usage, draft_llm.usage
        return (
            t.calls, t.input_tokens, t.output_tokens, t.reasoning_tokens,
            d.calls, d.input_tokens, d.output_tokens, d.reasoning_tokens,
        )  # fmt: skip

    for sample in samples:
        if used() + TRIAGE_RESERVE_TOKENS > args.max_tokens:
            print(f"-- token cap reached ({used()}); stopping before {sample['id']}")
            break
        allow_draft = (
            drafts_done < args.max_drafts and used() + DRAFT_RESERVE_TOKENS <= args.max_tokens
        )
        before = snap()
        out: Any = None
        for attempt in (1, 2):
            state = initial_state(
                job_kind="triage", message=to_message(sample), owner=owner, allow_draft=allow_draft
            )
            try:
                out = await run_agent(graph, state)
                break
            except LLMRateLimitError as exc:
                if attempt == 2 or exc.retry_after > MAX_RATE_LIMIT_WAIT_S:
                    print(f"{sample['id']:<16} RATE-LIMITED (retry after {exc.retry_after:.0f}s)")
                    break
                await asyncio.sleep(exc.retry_after)
            except Exception as exc:
                print(f"{sample['id']:<16} ERROR {type(exc).__name__}: {exc}")
                break
        ran += 1
        if out is None:
            continue
        lo, hi = sample["expected"]
        t = out.get("triage")
        got = t.priority if t else None
        ok = got is not None and lo <= got <= hi
        passed += ok
        band = f"{lo}-{hi}" if lo != hi else str(lo)
        summary = (t.summary if t else out.get("skip_reason") or "")[:48]
        delta = [a - b for a, b in zip(snap(), before, strict=True)]
        m = {
            "tri_calls": delta[0], "tri_in": delta[1], "tri_out": delta[2], "tri_rsn": delta[3],
            "dr_calls": delta[4], "dr_in": delta[5], "dr_out": delta[6], "dr_rsn": delta[7],
        }  # fmt: skip
        m["total"] = m["tri_in"] + m["tri_out"] + m["dr_in"] + m["dr_out"]
        if m["tri_calls"] or m["dr_calls"]:
            per_message.append(m)
        tri = f"{m['tri_in']}/{m['tri_out']}/{m['tri_rsn']}" if m["tri_calls"] else "-"
        dr = f"{m['dr_in']}/{m['dr_out']}/{m['dr_rsn']}" if m["dr_calls"] else "-"
        print(
            f"{sample['id']:<16} {band:<5} {got!s:<3} {'ok' if ok else 'NO':<3} "
            f"{out.get('triage_by') or '-':<5} {tri:>15} {dr:>17} {m['total']:>6}  {summary}"
        )
        draft = out.get("draft")
        if draft is not None:
            drafts_done += 1
            drafted.append((sample["id"], draft.body, list(draft.open_questions)))

    all_questions = [q for _, _, qs in drafted for q in qs]
    bad_questions = [q for q in all_questions if is_sender_directed(q)]
    if drafted:
        print("\n--- drafts  (? = open question for you; ✗ = wrongly addressed to the sender)")
        for sid, body, questions in drafted:
            print(f"[{sid}]\n{body}")
            for q in questions:
                print(f"  {'✗' if is_sender_directed(q) else '?'} {q}")
            print()
    print(
        f"open-question check: {len(all_questions) - len(bad_questions)}/{len(all_questions)} "
        f"written as items for the approver ({len(bad_questions)} addressed to the sender)"
    )
    u_t, u_d = triage_llm.usage, draft_llm.usage
    print(
        f"passed {passed}/{len(samples)} (ran {ran}); LLM calls {u_t.calls + u_d.calls} "
        f"(triage {u_t.calls}, draft {u_d.calls}); tokens in {u_t.input_tokens + u_d.input_tokens}"
        f" out {u_t.output_tokens + u_d.output_tokens} (reasoning "
        f"{u_t.reasoning_tokens + u_d.reasoning_tokens}) total {used()}; cost $0 (free tier)"
    )
    print_token_stats(per_message)
    return 0 if passed >= args.min_pass and not bad_questions else 1


def print_token_stats(rows: list[dict[str, int]]) -> None:
    """Measured per-call and per-message token statistics (input for the budget arithmetic)."""
    if not rows:
        return

    def stat(values: list[int]) -> str:
        if not values:
            return "n/a"
        v = sorted(values)
        return (
            f"mean {sum(v) / len(v):7.0f}  median {v[len(v) // 2]:6d}  max {v[-1]:6d}  n={len(v)}"
        )

    tri = [r for r in rows if r["tri_calls"]]
    dr = [r for r in rows if r["dr_calls"]]
    print("\n--- measured tokens")
    print(f"triage call  input     {stat([r['tri_in'] for r in tri])}")
    print(f"triage call  output    {stat([r['tri_out'] for r in tri])}")
    print(f"triage call  reasoning {stat([r['tri_rsn'] for r in tri])}")
    print(f"draft call   input     {stat([r['dr_in'] for r in dr])}")
    print(f"draft call   output    {stat([r['dr_out'] for r in dr])}")
    print(f"draft call   reasoning {stat([r['dr_rsn'] for r in dr])}")
    print(f"per message (all)      {stat([r['total'] for r in rows])}")
    print(f"per message (drafted)  {stat([r['total'] for r in dr])}")
    print(f"draft rate             {len(dr)}/{len(rows)} LLM-triaged messages")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--max-tokens", type=int, default=40_000, help="hard cap for this run")
    parser.add_argument("--max-drafts", type=int, default=4)
    parser.add_argument("--min-pass", type=int, default=12)
    parser.add_argument("--allow-fake", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args())))
