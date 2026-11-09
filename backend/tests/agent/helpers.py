from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.agent.state import AgentState, MessageForAgent, OwnerProfile, ThreadItem, initial_state

OWNER = OwnerProfile(
    name="Dhruv Sonkusare",
    context="Python backend developer",
    timezone="Asia/Kolkata",
    vips=("neha@acme.example",),
    self_ids=frozenset({"me@acme.example"}),
)
T0 = datetime(2026, 11, 9, 6, 30, tzinfo=UTC)


def message(**overrides: Any) -> MessageForAgent:
    values: dict[str, Any] = {
        "id": "m1",
        "source": "demo",
        "source_display": "Demo data",
        "sender_id": "priya@acme.example",
        "sender_name": "Priya Raman",
        "subject": "Deploy question",
        "thread_title": "Email · Inbox",
        "body": "Can you confirm whether we should roll back the deploy before 4pm?",
        "received_at": T0,
        "can_send": True,
    }
    values.update(overrides)
    return MessageForAgent(**values)


def state(
    *,
    kind: str = "triage",
    thread: list[ThreadItem] | None = None,
    allow_draft: bool = True,
    previous_draft: str | None = None,
    instructions: str | None = None,
    **msg: Any,
) -> AgentState:
    return initial_state(
        job_kind=kind,  # type: ignore[arg-type]
        message=message(**msg),
        owner=OWNER,
        thread=thread,
        allow_draft=allow_draft,
        previous_draft=previous_draft,
        redraft_instructions=instructions,
    )
