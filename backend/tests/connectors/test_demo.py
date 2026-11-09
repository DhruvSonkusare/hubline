from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog

from app.connectors.base import ConnectorContext
from app.connectors.demo import SCENARIOS, DemoConnector, DemoSettings
from tests.conftest import make_settings

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(clock: Clock, **settings: int) -> DemoConnector:
    return DemoConnector(
        ConnectorContext(
            account_id=uuid.uuid4(),
            source="demo",
            settings=DemoSettings(**settings),
            self_ids=frozenset(),
            http=httpx.AsyncClient(),
            app_settings=make_settings(),
            log=structlog.get_logger("test"),
            clock=clock,
        )
    )


async def test_backfill_is_deterministic_and_labelled() -> None:
    clock = Clock(T0)
    a = await make(clock).fetch_new(None)
    b = await make(clock).fetch_new(None)
    assert [m.model_dump() for m in a.messages] == [m.model_dump() for m in b.messages]
    assert len(a.messages) == 6
    assert all(m.is_demo and m.source == "demo" for m in a.messages)
    assert all(m.received_at < T0 for m in a.messages)
    assert [m.external_id for m in a.messages] == [f"demo-{i}" for i in range(6)]


async def test_emits_on_cadence() -> None:
    clock = Clock(T0)
    c = make(clock, emit_every_minutes=20)
    first = await c.fetch_new(None)

    clock.now = T0 + timedelta(minutes=19)
    r = await c.fetch_new(first.cursor)
    assert r.messages == []

    clock.now = T0 + timedelta(minutes=41)
    r2 = await c.fetch_new(r.cursor)
    assert [m.external_id for m in r2.messages] == ["demo-6", "demo-7"]
    assert r2.messages[0].received_at == T0 + timedelta(minutes=20)


async def test_backlog_is_capped_per_poll_and_does_not_accumulate() -> None:
    clock = Clock(T0)
    c = make(clock, emit_every_minutes=1, max_per_poll=3)
    first = await c.fetch_new(None)
    clock.now = T0 + timedelta(hours=5)  # e.g. the app was asleep
    r = await c.fetch_new(first.cursor)
    assert len(r.messages) == 3
    assert json.loads(r.cursor or "")["last_emit"] == clock.now.isoformat()
    r2 = await c.fetch_new(r.cursor)  # same instant: nothing more
    assert r2.messages == []


async def test_respects_max_messages() -> None:
    clock = Clock(T0)
    c = make(clock, emit_every_minutes=1, max_messages=7, backfill_count=6)
    first = await c.fetch_new(None)
    clock.now = T0 + timedelta(minutes=10)
    r = await c.fetch_new(first.cursor)
    assert len(r.messages) == 1
    clock.now = T0 + timedelta(minutes=30)
    assert (await c.fetch_new(r.cursor)).messages == []


async def test_follow_up_scenarios_share_a_thread() -> None:
    clock = Clock(T0)
    c = make(clock, backfill_count=len(SCENARIOS))
    msgs = (await c.fetch_new(None)).messages
    incident = [m for m in msgs if m.raw and str(m.raw["scenario"]).startswith("prod-incident")]
    assert len(incident) == 2
    assert incident[0].thread_id == incident[1].thread_id


@pytest.mark.parametrize("bad", [{"emit_every_minutes": 0}, {"max_per_poll": 0}])
def test_settings_validation(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        DemoSettings(**bad)
