"""Every branch of the LangGraph agent, driven by the scripted fake LLM."""

from __future__ import annotations

import pytest

from app.agent.fake import FakeLLM
from app.agent.graph import build_graph, run_agent
from app.agent.llm import LLMOutputError, LLMRefusalError
from app.agent.schemas import DraftResult, TriageResult
from tests.agent.helpers import state


def triage(priority: int, needs_reply: bool = True) -> TriageResult:
    return TriageResult(
        priority=priority,
        summary="Priya: rollback decision needed",
        needs_reply=needs_reply,
        category="action_required",
        reason="deadline today",
    )


def draft(body: str = "Rolling back now; will confirm on the call.") -> DraftResult:
    return DraftResult(body=body, tone="neutral")


def llms() -> tuple[FakeLLM, FakeLLM]:
    return FakeLLM(model_name="t"), FakeLLM(model_name="d")


async def test_triage_and_draft_happy_path() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(5))
    d.queue(DraftResult, draft())
    out = await run_agent(build_graph(t, d), state())
    assert out["outcome"] == "done"
    assert out["triage"] is not None
    assert out["triage"].priority == 5
    assert out["draft"] is not None
    assert out["draft"].body.startswith("Rolling back")
    assert out["llm_calls"] == 2
    assert out["triage_by"] == "llm"


async def test_low_priority_without_reply_gets_no_draft() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(2, needs_reply=False))
    out = await run_agent(build_graph(t, d), state())
    assert out["draft"] is None
    assert d.calls == []


@pytest.mark.parametrize("priority", [3, 5])
async def test_no_draft_without_needs_reply_even_at_high_priority(priority: int) -> None:
    """E.g. an automated security alert: act on it, don't reply to it."""
    t, d = llms()
    t.queue(TriageResult, triage(priority, needs_reply=False))
    out = await run_agent(build_graph(t, d), state())
    assert out["draft"] is None
    assert d.calls == []


async def test_needs_reply_is_drafted_at_any_priority() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(2, needs_reply=True))
    out = await run_agent(build_graph(t, d), state())
    assert out["draft"] is not None


async def test_draft_prompt_asks_for_a_holding_reply_instead_of_invented_facts() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(4))
    await run_agent(build_graph(t, d), state())
    system = d.calls[0].system
    assert "HOLDING REPLY" in system
    assert "open_questions" in system
    assert "Never invent facts" in system


async def test_allow_draft_false_skips_drafting() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(5))
    out = await run_agent(build_graph(t, d), state(allow_draft=False))
    assert out["draft"] is None
    assert d.calls == []


async def test_skipped_message_makes_no_llm_calls() -> None:
    t, d = llms()
    out = await run_agent(build_graph(t, d), state(direction="outbound"))
    assert out["outcome"] == "skipped"
    assert t.calls == []
    assert d.calls == []


async def test_rule_based_triage_skips_llm() -> None:
    t, d = llms()
    out = await run_agent(
        build_graph(t, d),
        state(sender_id="noreply@news.example", headers={"precedence": "bulk"}),
    )
    assert out["triage_by"] == "rules"
    assert out["triage"] is not None
    assert out["triage"].priority == 1
    assert t.calls == []
    assert d.calls == []


async def test_invalid_draft_is_retried_once_with_feedback() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(4))
    d.queue(DraftResult, draft("See you on [date]."), draft("See you Thursday."))
    out = await run_agent(build_graph(t, d), state())
    assert out["draft"] is not None
    assert out["draft"].body == "See you Thursday."
    assert out["draft_error"] is None
    assert len(d.calls) == 2
    assert "rejected for these reasons" in d.calls[1].prompt


async def test_invalid_draft_twice_keeps_triage_and_records_error() -> None:
    t, d = llms()
    t.queue(TriageResult, triage(4))
    d.queue(DraftResult, draft("[your name]"), draft("{{greeting}}"))
    out = await run_agent(build_graph(t, d), state())
    assert out["triage"] is not None
    assert out["draft"] is None
    assert (out["draft_error"] or "").startswith("draft_invalid")


async def test_structured_output_error_is_retried_once() -> None:
    t, d = llms()
    t.queue(TriageResult, LLMOutputError("bad json"), triage(4))
    d.queue(DraftResult, draft())
    out = await run_agent(build_graph(t, d), state())
    assert out["triage"] is not None
    assert len(t.calls) == 2
    assert "previous answer was invalid" in t.calls[1].prompt


async def test_structured_output_error_twice_raises() -> None:
    t, d = llms()
    t.queue(TriageResult, LLMOutputError("bad"), LLMOutputError("still bad"))
    with pytest.raises(LLMOutputError):
        await run_agent(build_graph(t, d), state())


async def test_refusal_propagates() -> None:
    t, d = llms()
    t.queue(TriageResult, LLMRefusalError("declined"))
    with pytest.raises(LLMRefusalError):
        await run_agent(build_graph(t, d), state())


async def test_redraft_skips_triage_and_uses_instructions() -> None:
    t, d = llms()
    d.queue(DraftResult, draft("Short version."))
    out = await run_agent(
        build_graph(t, d),
        state(kind="redraft", previous_draft="Long version...", instructions="shorter please"),
    )
    assert t.calls == []
    assert out["draft"] is not None
    assert out["draft"].body == "Short version."
    assert "shorter please" in d.calls[0].prompt
    assert "Long version..." in d.calls[0].prompt


async def test_heuristic_fake_runs_without_script() -> None:
    t, d = llms()
    out = await run_agent(build_graph(t, d), state())
    assert out["triage"] is not None
    assert out["triage"].priority >= 3
    assert (out["triage"].reason or "").startswith("[fake model]")
    assert out["draft"] is not None
    assert out["draft"].body.startswith("Hi Priya")
