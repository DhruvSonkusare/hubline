"""Deterministic offline LLM (LLM_PROVIDER=fake).

- Heuristic mode (default): keyword rules produce plausible triage + drafts, so the whole app
  works without an API key (tests, CI, offline demos). Output is labelled "fake".
- Scripted mode (tests): queue exact results or exceptions per schema with `script`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from app.agent.llm import LLMOutputError, Usage, record_usage
from app.agent.schemas import DraftResult, TriageResult

T = TypeVar("T", bound=BaseModel)

_URGENT = re.compile(
    r"(?<!not )\b(urgent|asap|immediately|right now|outage|rollback|roll back|500s|incident|"
    r"go/no-go)\b",
    re.I,
)
_DEADLINE = re.compile(
    r"\b(today|tonight|tomorrow|by (mon|tues|wednes|thurs|fri|satur|sun)day|by friday|"
    r"deadline|eod|before our|this week)\b",
    re.I,
)
_ASK = re.compile(r"\?|\b(can you|could you|please|would you|mind taking|are you)\b", re.I)
_AUTOMATED = re.compile(
    r"\b(unsubscribe|newsletter|no-?reply|invoice|receipt|build #?\d+ failed|"
    r"charged automatically)\b",
    re.I,
)
_FROM_TAG = re.compile(r"^From: (.+)$", re.M)
_MESSAGE = re.compile(r"<message>\n(.*?)\n</message>", re.S)
_SUBJECT = re.compile(r"^Subject: (.+)$", re.M)


@dataclass
class FakeCall:
    schema: str
    system: str
    prompt: str


@dataclass
class FakeLLM:
    model_name: str = "fake"
    script: dict[type[BaseModel], list[Any]] = field(default_factory=lambda: defaultdict(list))
    calls: list[FakeCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)

    def queue(self, schema: type[BaseModel], *items: Any) -> FakeLLM:
        self.script.setdefault(schema, []).extend(items)
        return self

    async def generate(self, schema: type[T], *, system: str, prompt: str, max_tokens: int) -> T:
        self.calls.append(FakeCall(schema.__name__, system, prompt))
        queued = self.script.get(schema)
        result: Any
        if queued:
            item = queued.pop(0)
            if isinstance(item, BaseException):
                raise item  # like a real provider: a failed call reports no usage
            if not isinstance(item, schema):
                raise LLMOutputError(f"scripted {type(item).__name__} for {schema.__name__}")
            result = item
        elif schema is TriageResult:
            result = _heuristic_triage(system, prompt)
        elif schema is DraftResult:
            result = _heuristic_draft(system, prompt)
        else:
            raise LLMOutputError(f"fake LLM has no heuristic for {schema.__name__}")
        # ~4 chars per token in, a fixed 50 out; also attributed to the current job
        record_usage(self.model_name, len(system + prompt) // 4, 50, self.usage)
        return cast(T, result)


def _parts(prompt: str) -> tuple[str, str, str]:
    sender = (_FROM_TAG.findall(prompt) or ["Someone"])[-1]
    body = (_MESSAGE.findall(prompt) or [""])[-1]
    subject = (_SUBJECT.findall(prompt) or [""])[-1]
    return sender, subject, body


def _name(sender: str) -> str:
    name = sender.split("<")[0].strip() or sender
    return name.split("(")[0].strip()


def _heuristic_triage(system: str, prompt: str) -> TriageResult:
    sender, subject, body = _parts(prompt)
    text = f"{subject}\n{body}"
    vip = any(v and v in sender.lower() for v in _vips(system))
    if _AUTOMATED.search(text) or "noreply" in sender.lower():
        priority, category, needs_reply, why = 1, "automated", False, "automated or bulk message"
    elif _URGENT.search(text):
        priority, category, needs_reply, why = 5, "action_required", True, "urgent language"
    elif _DEADLINE.search(text) and _ASK.search(text):
        priority, category, needs_reply, why = 4, "question", True, "request with a deadline"
    elif _ASK.search(text):
        priority, category, needs_reply, why = 3, "question", True, "direct request or question"
    else:
        priority, category, needs_reply, why = 2, "fyi", False, "informational"
    if vip and priority < 5:
        priority, why = priority + 1, why + " (VIP sender)"
    first_sentence = re.split(r"(?<=[.!?])\s", " ".join(body.split()), maxsplit=1)[0]
    return TriageResult(
        priority=priority,
        summary=f"{_name(sender)}: {first_sentence}",
        needs_reply=needs_reply,
        category=category,
        reason=f"[fake model] {why}",
    )


def _vips(system: str) -> list[str]:
    m = re.search(r"VIP senders: (.+?)\.", system)
    if not m or m.group(1) == "none listed":
        return []
    return [v.strip().lower() for v in m.group(1).split(",")]


def _open_questions(body: str) -> list[str]:
    """Like the real prompt: a holding reply + a checklist item for the OWNER (never a question
    to the sender) when the message asks for something only the owner knows."""
    if not _ASK.search(body):
        return []
    ask = next((s.strip() for s in re.split(r"(?<=[.!?:;])\s+", body) if "?" in s), "")
    topic = ask.rstrip("?").strip() or "the request"
    topic = topic[:1].upper() + topic[1:]
    if len(topic) > 70:
        topic = topic[:69].rstrip() + "…"
    return [f"Your answer to: “{topic}” (needed for the reply)"]


def _heuristic_draft(system: str, prompt: str) -> DraftResult:
    sender, subject, body_text = _parts(prompt)
    first = _name(sender).split()[0] if _name(sender) else "there"
    owner_first = re.search(r"on behalf of (\S+)", system) or re.search(r"(\S+)", "Me")
    owner = owner_first.group(1) if owner_first else "Me"
    revise = re.search(r"Revise the draft according to: (.+)", prompt)
    note = f" ({revise.group(1).strip()})" if revise else ""
    if "This is an email" in system:
        body = (
            f"Hi {first},\n\nThanks for your message{f' about {subject}' if subject else ''}. "
            f"I'm looking into it and will get back to you shortly.{note}\n\nBest,\n{owner}"
        )
        tone = "formal"
    else:
        body = f"Thanks {first}, on it. I'll get back to you shortly.{note}"
        tone = "casual"
    return DraftResult(body=body, tone=tone, open_questions=_open_questions(body_text))
