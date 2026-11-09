"""Structured outputs the LLM must return. Length limits are enforced by normalisation after
parsing (not in the JSON schema), so a slightly long summary is trimmed instead of failing."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "action_required",
    "question",
    "scheduling",
    "fyi",
    "social",
    "automated",
    "marketing",
    "other",
]

SUMMARY_MAX = 160
REASON_MAX = 300
MAX_OPEN_QUESTIONS = 3


class TriageResult(BaseModel):
    priority: int = Field(ge=1, le=5, description="1 = noise ... 5 = urgent, act within hours")
    summary: str = Field(description="One line, at most 160 characters: who wants what, by when")
    needs_reply: bool
    category: Category
    reason: str = Field(description="One sentence explaining the priority")


class DraftResult(BaseModel):
    body: str = Field(description="The reply text only, ready to send")
    tone: Literal["formal", "neutral", "casual"]
    open_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Checklist for the owner who approves the reply: each item names a fact the owner "
            "must supply, e.g. 'Your current blockers (needed for the reply)'. Never a "
            "question to the sender."
        ),
    )


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def normalize_triage(t: TriageResult) -> TriageResult:
    return t.model_copy(
        update={
            "summary": _one_line(t.summary, SUMMARY_MAX),
            "reason": _one_line(t.reason, REASON_MAX),
        }
    )


def normalize_draft(d: DraftResult) -> DraftResult:
    questions = [q.strip() for q in d.open_questions if q.strip()][:MAX_OPEN_QUESTIONS]
    return d.model_copy(update={"body": d.body.strip(), "open_questions": questions})


# "Do you want me to roll back?", "Could you share the logs?" - addressed to the sender,
# which open_questions must never be (they are a checklist for the approving owner).
_SENDER_DIRECTED = re.compile(
    r"^\s*(do|did|does|can|could|would|will|are|is|should|shall|may|have|has)\s+(you|we)\b"
    r"|\b(do|would) you (want|like|prefer)\b"
    r"|\bplease (share|send|confirm|let me know)\b",
    re.IGNORECASE,
)


def is_sender_directed(question: str) -> bool:
    """True when an open question is phrased TO THE SENDER instead of as an item for the owner
    (used by scripts/agent_eval.py as a quality check)."""
    return bool(_SENDER_DIRECTED.search(question))
