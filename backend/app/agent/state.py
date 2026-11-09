"""Inputs and graph state for the agent. Everything here is plain data: the graph never touches
the database; the worker loads context before and persists results after (plan §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict

from app.agent.schemas import DraftResult, TriageResult

DEFAULT_MAX_REPLY_CHARS = 4000


@dataclass(frozen=True)
class MessageForAgent:
    id: str
    source: str
    source_display: str
    sender_id: str
    sender_name: str | None
    subject: str | None
    thread_title: str | None
    body: str
    received_at: datetime
    direction: Literal["inbound", "outbound"] = "inbound"
    can_send: bool = False
    max_reply_chars: int = DEFAULT_MAX_REPLY_CHARS
    headers: dict[str, str] = field(default_factory=dict)  # lower-case keys (e.g. Gmail)


@dataclass(frozen=True)
class ThreadItem:
    direction: Literal["inbound", "outbound"]
    sender_name: str
    body: str
    received_at: datetime


@dataclass(frozen=True)
class OwnerProfile:
    name: str
    context: str
    timezone: str = "UTC"
    vips: tuple[str, ...] = ()
    self_ids: frozenset[str] = frozenset()


class AgentState(TypedDict, total=False):
    # inputs
    job_kind: Literal["triage", "redraft"]
    message: MessageForAgent
    thread: list[ThreadItem]
    owner: OwnerProfile
    previous_draft: str | None
    redraft_instructions: str | None
    allow_draft: bool
    # outputs
    triage: TriageResult | None
    triage_by: Literal["llm", "rules"] | None
    draft: DraftResult | None
    draft_attempts: int
    validation_errors: list[str]
    draft_error: str | None
    outcome: Literal["done", "skipped"]
    skip_reason: str | None
    llm_calls: int


def initial_state(
    *,
    job_kind: Literal["triage", "redraft"],
    message: MessageForAgent,
    owner: OwnerProfile,
    thread: list[ThreadItem] | None = None,
    previous_draft: str | None = None,
    redraft_instructions: str | None = None,
    allow_draft: bool = True,
) -> AgentState:
    return AgentState(
        job_kind=job_kind,
        message=message,
        thread=thread or [],
        owner=owner,
        previous_draft=previous_draft,
        redraft_instructions=redraft_instructions,
        allow_draft=allow_draft,
        triage=None,
        triage_by=None,
        draft=None,
        draft_attempts=0,
        validation_errors=[],
        draft_error=None,
        outcome="done",
        skip_reason=None,
        llm_calls=0,
    )


def result_summary(state: AgentState) -> dict[str, Any]:
    """Compact, log-safe view of a finished run."""
    triage = state.get("triage")
    draft = state.get("draft")
    return {
        "outcome": state.get("outcome"),
        "priority": triage.priority if triage else None,
        "has_draft": draft is not None,
        "llm_calls": state.get("llm_calls", 0),
        "draft_error": state.get("draft_error"),
    }
