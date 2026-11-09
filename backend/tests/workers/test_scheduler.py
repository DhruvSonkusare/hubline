from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors.base import AuthExpiredError, RateLimitedError
from app.connectors.models import FetchResult
from app.workers.scheduler import Scheduler
from tests.conftest import FakeConnector, make_settings, msg
from tests.helpers import all_messages, get_account, get_state, make_due, setup_accounts

pytestmark = pytest.mark.db

SM = async_sessionmaker[AsyncSession]


@pytest.fixture
async def http() -> httpx.AsyncClient:
    async with httpx.AsyncClient() as client:
        yield client  # type: ignore[misc]


def scheduler(sm: SM, http: httpx.AsyncClient, settings: Settings, name: str = "t") -> Scheduler:
    return Scheduler(sm, settings, http, asyncio.Event(), instance_id=name)


async def test_polls_due_enabled_connector_and_saves_cursor(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.pages = [FetchResult(messages=[msg(1), msg(2)], cursor="c1")]

    before = datetime.now(UTC)
    assert await scheduler(sessionmaker, http, settings).tick() == 1

    assert fake_connector.calls == [None]
    assert len(await all_messages(sessionmaker)) == 2
    state = await get_state(sessionmaker, "fake")
    assert state.cursor == "c1"
    assert state.lease_until is None
    assert state.consecutive_failures == 0
    assert state.last_success_at is not None
    assert state.next_poll_at >= before + timedelta(seconds=55)  # interval 60s

    # Not due any more -> nothing to claim.
    assert await scheduler(sessionmaker, http, settings).tick() == 0


async def test_disabled_or_not_due_connectors_are_skipped(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings)  # fake disabled
    assert await scheduler(sessionmaker, http, settings).tick() == 0
    assert fake_connector.calls == []


async def test_follows_has_more_pages_and_resumes_from_cursor(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.pages = [
        FetchResult(messages=[msg(1)], cursor="p1", has_more=True),
        FetchResult(messages=[msg(2)], cursor="p2"),
    ]
    await scheduler(sessionmaker, http, settings).tick()
    assert fake_connector.calls == [None, "p1"]

    await make_due(sessionmaker, "fake")
    await scheduler(sessionmaker, http, settings).tick()
    assert fake_connector.calls[-1] == "p2"


async def test_lease_is_renewed_between_pages(
    sessionmaker: SM, http: httpx.AsyncClient, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(poll_lease_s=30)
    await setup_accounts(sessionmaker, s, fake=True)
    leases: list[datetime | None] = []

    original = FakeConnector.fetch_new

    async def spy(self: FakeConnector, since: str | None) -> FetchResult:
        leases.append((await get_state(sessionmaker, "fake")).lease_until)
        await asyncio.sleep(0.05)
        return await original(self, since)

    fake_connector.pages = [
        FetchResult(messages=[msg(1)], cursor="p1", has_more=True),
        FetchResult(messages=[msg(2)], cursor="p2"),
    ]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(FakeConnector, "fetch_new", spy)
        await scheduler(sessionmaker, http, s).tick()
    assert leases[0] is not None
    assert leases[1] is not None
    assert leases[1] > leases[0]  # extended after page 1 was committed


async def test_failure_backs_off_and_keeps_cursor(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.pages = [FetchResult(messages=[msg(1)], cursor="good")]
    await scheduler(sessionmaker, http, settings).tick()

    fake_connector.error = RuntimeError("boom")
    for expected_failures in (1, 2):
        await make_due(sessionmaker, "fake")
        before = datetime.now(UTC)
        await scheduler(sessionmaker, http, settings).tick()
        state = await get_state(sessionmaker, "fake")
        assert state.consecutive_failures == expected_failures
        assert state.cursor == "good"
        assert state.lease_until is None
        assert "boom" in (state.last_error or "")
        expected_delay = 60 * 2**expected_failures
        assert state.next_poll_at >= before + timedelta(seconds=expected_delay - 5)
    assert "boom" in ((await get_account(sessionmaker, "fake")).last_error or "")

    fake_connector.error = None
    await make_due(sessionmaker, "fake")
    await scheduler(sessionmaker, http, settings).tick()
    state = await get_state(sessionmaker, "fake")
    assert state.consecutive_failures == 0
    assert state.last_error is None
    assert (await get_account(sessionmaker, "fake")).last_error is None


async def test_auth_expired_marks_needs_reauth_and_stops_polling(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.error = AuthExpiredError("token revoked")
    await scheduler(sessionmaker, http, settings).tick()
    assert (await get_account(sessionmaker, "fake")).status == "needs_reauth"

    await make_due(sessionmaker, "fake")
    assert await scheduler(sessionmaker, http, settings).tick() == 0


async def test_rate_limit_waits_retry_after_without_counting_failure(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    await setup_accounts(sessionmaker, settings, fake=True)
    fake_connector.error = RateLimitedError("slow down", retry_after=300)
    before = datetime.now(UTC)
    await scheduler(sessionmaker, http, settings).tick()
    state = await get_state(sessionmaker, "fake")
    assert state.consecutive_failures == 0
    assert state.next_poll_at >= before + timedelta(seconds=295)


async def test_concurrent_schedulers_poll_each_connector_once(
    sessionmaker: SM,
    settings: Settings,
    http: httpx.AsyncClient,
    fake_connector: type[FakeConnector],
) -> None:
    """SKIP LOCKED + lease: several instances ticking at once never double-poll."""
    await setup_accounts(sessionmaker, settings, fake=True, demo=True)
    fake_connector.delay_s = 0.3  # keep the lease held while the others try to claim
    results = await asyncio.gather(
        *(scheduler(sessionmaker, http, settings, f"s{i}").tick() for i in range(4))
    )
    assert sum(results) == 2  # fake + demo, each exactly once
    assert len(fake_connector.calls) == 1


async def test_allowlist_excludes_connectors(
    sessionmaker: SM, http: httpx.AsyncClient, fake_connector: type[FakeConnector]
) -> None:
    only_demo = make_settings(connectors_enabled="demo")
    await setup_accounts(sessionmaker, only_demo, fake=True)
    assert await scheduler(sessionmaker, http, only_demo).tick() == 0
    assert fake_connector.calls == []


async def test_idle_delay(
    sessionmaker: SM, http: httpx.AsyncClient, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(scheduler_idle_max_s=60, scheduler_min_sleep_s=1)
    sch = scheduler(sessionmaker, http, s)

    await setup_accounts(sessionmaker, s)  # nothing enabled -> sleep the full idle max
    assert await sch.idle_delay() == 60

    await setup_accounts(sessionmaker, s, fake=True)  # due now -> minimum sleep
    assert await sch.idle_delay() == 1

    await sch.tick()  # next poll in ~60s -> sleep about that long, capped at idle max
    delay = await sch.idle_delay()
    assert 50 <= delay <= 60

    short = make_settings(scheduler_idle_max_s=15, scheduler_min_sleep_s=1)
    assert await scheduler(sessionmaker, http, short).idle_delay() == 15


async def test_run_loop_wakes_on_event_and_stops(
    sessionmaker: SM, http: httpx.AsyncClient, fake_connector: type[FakeConnector]
) -> None:
    s = make_settings(scheduler_idle_max_s=3600)  # would sleep for an hour without a wake-up
    await setup_accounts(sessionmaker, s)
    wake, stop = asyncio.Event(), asyncio.Event()
    sch = Scheduler(sessionmaker, s, http, wake, instance_id="loop")
    task = asyncio.create_task(sch.run(stop))
    await asyncio.sleep(0.3)
    assert fake_connector.calls == []

    await setup_accounts(sessionmaker, s, fake=True)
    wake.set()
    for _ in range(50):
        if fake_connector.calls:
            break
        await asyncio.sleep(0.05)
    assert fake_connector.calls == [None]

    stop.set()
    await asyncio.wait_for(task, timeout=2)
