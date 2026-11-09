"""Graph nodes. Pure functions of state (+ injected LLMs); no database access."""

from __future__ import annotations

import re
from typing import Any

from app.agent.llm import MAX_TOKENS, StructuredLLM, call_structured
from app.agent.prompts import draft_prompt, triage_prompt
from app.agent.schemas import DraftResult, TriageResult, normalize_draft, normalize_triage
from app.agent.state import AgentState

TRIAGE_MAX_TOKENS = MAX_TOKENS["triage"]  # reasoning tokens count against max_tokens
DRAFT_MAX_TOKENS = MAX_TOKENS["draft"]
MAX_DRAFT_ATTEMPTS = 2  # first draft + one retry after validation feedback

_NOREPLY_SENDER = re.compile(
    r"(^|[^a-z])(no-?reply|do-?not-?reply|mailer-daemon|bounces?|notifications?)([^a-z]|$)",
    re.I,
)
_PLACEHOLDER = re.compile(
    r"\[(?:your|insert|name|date|time|day|company|link|todo|tbd|x+)\b[^\]]*\]"
    r"|\{\{[^}]*\}\}"
    r"|<(?:your|insert)[^>]*>"
    r"|\bTODO\b",
    re.I,
)

# ------------------------------------------------------------------------------ prefilter


def prefilter(state: AgentState) -> dict[str, Any]:
    """Cheap rules before any LLM call."""
    m = state["message"]
    owner = state["owner"]
    if m.direction == "outbound" or m.sender_id in owner.self_ids:
        return {"outcome": "skipped", "skip_reason": "sent by the owner"}
    if state.get("job_kind") == "redraft":
        return {}
    if not m.body.strip() and not (m.subject or "").strip():
        return {"outcome": "skipped", "skip_reason": "empty message"}
    if is_bulk_automated(m.sender_id, m.headers):
        return {
            "triage": TriageResult(
                priority=1,
                summary=f"{m.sender_name or m.sender_id}: {(m.subject or m.body)[:120]}".strip(),
                needs_reply=False,
                category="automated",
                reason="Bulk mail from a no-reply sender (rule-based, no LLM call).",
            ),
            "triage_by": "rules",
        }
    return {}


def is_bulk_automated(sender_id: str, headers: dict[str, str]) -> bool:
    bulk = "list-unsubscribe" in headers or headers.get("precedence", "").lower() in {
        "bulk",
        "list",
        "junk",
    }
    return bulk and bool(_NOREPLY_SENDER.search(sender_id))


def route_after_prefilter(state: AgentState) -> str:
    if state.get("outcome") == "skipped":
        return "end"
    if state.get("job_kind") == "redraft":
        return "draft"
    if state.get("triage") is not None:  # rule-based triage
        return "end"
    return "triage"


# ------------------------------------------------------------------------------ triage


def make_triage_node(llm: StructuredLLM) -> Any:
    async def triage(state: AgentState) -> dict[str, Any]:
        system, prompt = triage_prompt(state)
        result = await call_structured(
            llm, TriageResult, system=system, prompt=prompt, max_tokens=TRIAGE_MAX_TOKENS
        )
        return {
            "triage": normalize_triage(result),
            "triage_by": "llm",
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    return triage


def route_after_triage(state: AgentState) -> str:
    triage = state.get("triage")
    if triage is None or not state.get("allow_draft", True):
        return "end"
    return "draft" if (triage.needs_reply or triage.priority >= 3) else "end"


# ------------------------------------------------------------------------------ draft


def make_draft_node(llm: StructuredLLM) -> Any:
    async def draft(state: AgentState) -> dict[str, Any]:
        system, prompt = draft_prompt(state)
        result = await call_structured(
            llm, DraftResult, system=system, prompt=prompt, max_tokens=DRAFT_MAX_TOKENS
        )
        return {
            "draft": normalize_draft(result),
            "draft_attempts": state.get("draft_attempts", 0) + 1,
            "llm_calls": state.get("llm_calls", 0) + 1,
        }

    return draft


def validate_draft(state: AgentState) -> dict[str, Any]:
    draft = state.get("draft")
    m = state["message"]
    errors: list[str] = []
    if draft is None or not draft.body:
        errors.append("the reply body is empty")
    else:
        if len(draft.body) > m.max_reply_chars:
            errors.append(
                f"the reply is {len(draft.body)} characters; the limit is {m.max_reply_chars}"
            )
        if _PLACEHOLDER.search(draft.body):
            errors.append(
                "the reply contains a placeholder; write around missing facts and list them in "
                "open_questions instead"
            )
        if " ".join(draft.body.split()) == " ".join(m.body.split()):
            errors.append("the reply repeats the incoming message instead of answering it")
    if not errors:
        return {"validation_errors": []}
    if state.get("draft_attempts", 0) >= MAX_DRAFT_ATTEMPTS:
        return {
            "validation_errors": errors,
            "draft": None,
            "draft_error": "draft_invalid: " + "; ".join(errors),
        }
    return {"validation_errors": errors}


def route_after_validation(state: AgentState) -> str:
    if not state.get("validation_errors"):
        return "end"
    return "end" if state.get("draft_error") else "draft"
