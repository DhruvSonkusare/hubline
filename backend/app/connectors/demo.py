"""Demo connector: deterministic synthetic messages, always labelled is_demo.

Toggled from the Connectors page like any other connector (plan R4). It needs no credentials.
Cadence is deliberately slow (default one message every 20 minutes, capped) so that, once the agent
exists (Step 2), the demo never burns through the LLM budget.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from app.connectors.base import AuthType, Capability, Connector, ConnectorDescriptor
from app.connectors.models import FetchResult, NormalizedMessage, Sender
from app.connectors.registry import register


class DemoSettings(BaseModel):
    emit_every_minutes: int = Field(default=20, ge=1, le=1440)
    max_messages: int = Field(default=100, ge=0, le=1000)
    backfill_count: int = Field(default=6, ge=0, le=20)
    max_per_poll: int = Field(default=3, ge=1, le=20)


@dataclass(frozen=True)
class Scenario:
    key: str
    thread: str  # scenarios sharing a thread key continue the same conversation
    sender_id: str
    sender_name: str
    title: str
    subject: str | None
    body: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "prod-incident",
        "incident",
        "priya@acme.example",
        "Priya Raman",
        "Email · Inbox",
        "URGENT: payments API 500s in prod",
        "Hi Dhruv, the /payments/charge endpoint started returning 500s about 15 minutes ago "
        "after the 2.14 deploy. Error rate is ~8%. Can you take a look right now and tell me "
        "whether we should roll back? I need an answer before the 4pm leadership call.",
    ),
    Scenario(
        "newsletter",
        "newsletter",
        "news@pyweekly.example",
        "Python Weekly",
        "Email · Inbox",
        "Python Weekly #712: async generators, uv tips, and more",
        "This week: 10 tips for faster uv installs, a deep dive into async generators, "
        "and the PSF survey results. Unsubscribe at any time.",
    ),
    Scenario(
        "pr-review",
        "pr-review",
        "U_ARJUN",
        "Arjun Mehta",
        "#backend",
        None,
        "@Dhruv can you review my PR for the connection-pool refactor (#482) when you get a "
        "chance? Not urgent, but I'd like to merge it this week.",
    ),
    Scenario(
        "recruiter",
        "recruiter",
        "talent@bigcorp.example",
        "Meera (BigCorp Talent)",
        "LinkedIn · Messages",
        None,
        "Hi Dhruv, I came across your profile and think you'd be a great fit for a Senior "
        "Python Engineer role on our platform team. Open to a quick chat next week?",
    ),
    Scenario(
        "ci-failure",
        "ci",
        "noreply@ci.example",
        "CI Bot",
        "Email · Inbox",
        "[hubline] Build failed on main (#331)",
        "Build #331 failed on main. 2 tests failed in tests/api/test_messages.py. "
        "View the logs in your CI dashboard.",
    ),
    Scenario(
        "standup-move",
        "standup",
        "U_KAVYA",
        "Kavya Iyer",
        "Teams · Backend chat",
        None,
        "Hey, can we move tomorrow's standup to 10:30? I have a dentist appointment at 9:30.",
    ),
    Scenario(
        "prod-incident-2",
        "incident",
        "priya@acme.example",
        "Priya Raman",
        "Email · Inbox",
        "Re: URGENT: payments API 500s in prod",
        "Following up: error rate is now 12%. Leadership call is in 40 minutes. "
        "Please reply with a go/no-go on rollback.",
    ),
    Scenario(
        "client-question",
        "client",
        "rahul@clientco.example",
        "Rahul Verma",
        "Email · Inbox",
        "Question about the webhook retry policy",
        "Hello Dhruv, quick question before we finalise our integration: if our endpoint is "
        "down, how many times do you retry a webhook and over what period? We need this by "
        "Friday for our security review.",
    ),
    Scenario(
        "friend",
        "friend",
        "@sameer_k",
        "Sameer K",
        "X · Direct messages",
        None,
        "Bro, are you coming to the PyCon meetup on Saturday? A few of us are grabbing dinner "
        "after.",
    ),
    Scenario(
        "invoice",
        "invoice",
        "billing@cloudhost.example",
        "CloudHost Billing",
        "Email · Inbox",
        "Your invoice for October is available",
        "Your invoice INV-20431 for October (USD 18.40) is available in the billing dashboard. "
        "Payment will be charged automatically on the 15th.",
    ),
    Scenario(
        "pr-review-2",
        "pr-review",
        "U_ARJUN",
        "Arjun Mehta",
        "#backend",
        None,
        "Pushed the changes you asked for on #482 — pool size is now configurable. "
        "Mind taking another look?",
    ),
    Scenario(
        "manager-1on1",
        "one-on-one",
        "U_NEHA",
        "Neha Kapoor",
        "Slack · DM",
        None,
        "Can you send me a short summary of what's blocking the Hubline launch before our 1:1 "
        "tomorrow? Bullet points are fine.",
    ),
)


@register
class DemoConnector(Connector):
    descriptor = ConnectorDescriptor(
        key="demo",
        display_name="Demo data",
        auth_type=AuthType.NONE,
        capabilities=frozenset({Capability.POLL}),
        poll_interval_s=60,
        is_demo=True,
        settings_model=DemoSettings,
    )

    @property
    def _settings(self) -> DemoSettings:
        s = self.ctx.settings
        return s if isinstance(s, DemoSettings) else DemoSettings()

    async def fetch_new(self, since: str | None) -> FetchResult:
        cfg = self._settings
        now = self.ctx.clock()
        interval = timedelta(minutes=cfg.emit_every_minutes)

        if since is None:  # first run: backfill a handful of recent messages
            count = min(cfg.backfill_count, cfg.max_messages)
            messages = [self._make(n, now - interval * (count - n) / 2) for n in range(count)]
            return FetchResult(messages=messages, cursor=_encode(count, now))

        n, last_emit = _decode(since)
        due = int((now - last_emit) / interval) if now > last_emit else 0
        k = max(0, min(due, cfg.max_per_poll, cfg.max_messages - n))
        if k == 0:
            # Nothing new. If capped or more were due than allowed, don't let a backlog build up.
            new_last = now if (n >= cfg.max_messages or due > cfg.max_per_poll) else last_emit
            return FetchResult(messages=[], cursor=_encode(n, new_last))

        messages = [self._make(n + i, last_emit + interval * (i + 1)) for i in range(k)]
        new_last = now if due > k else last_emit + interval * k
        return FetchResult(messages=messages, cursor=_encode(n + k, new_last))

    def _make(self, n: int, received_at: datetime) -> NormalizedMessage:
        sc = SCENARIOS[n % len(SCENARIOS)]
        cycle = n // len(SCENARIOS)
        return NormalizedMessage(
            source="demo",
            external_id=f"demo-{n}",
            thread_id=f"demo:{sc.thread}:{cycle}",
            sender=Sender(id=sc.sender_id, name=sc.sender_name),
            body=sc.body,
            received_at=received_at,
            subject=sc.subject,
            thread_title=sc.title,
            raw={"scenario": sc.key, "n": n},
            is_demo=True,
        )


def _encode(n: int, last_emit: datetime) -> str:
    return json.dumps({"n": n, "last_emit": last_emit.isoformat()})


def _decode(cursor: str) -> tuple[int, datetime]:
    data = json.loads(cursor)
    return int(data["n"]), datetime.fromisoformat(data["last_emit"])
