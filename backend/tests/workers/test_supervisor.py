from __future__ import annotations

import asyncio

from app.workers.supervisor import Supervisor, sleep_or_wake


async def test_restarts_crashed_loop_then_stops() -> None:
    runs = 0

    async def flaky(stop: asyncio.Event) -> None:
        nonlocal runs
        runs += 1
        if runs < 3:
            raise RuntimeError("crash")
        await stop.wait()

    sup = Supervisor(max_restart_backoff_s=0.01)
    sup._max_backoff = 0.01
    sup.start("w", flaky)
    for _ in range(100):
        if runs >= 3 and sup.status["w"] == "running":
            break
        await asyncio.sleep(0.02)
    assert runs == 3
    await sup.stop(grace_s=1)
    assert sup.status["w"] == "stopped"


async def test_mark_records_status() -> None:
    sup = Supervisor()
    sup.mark("agent_worker", "disabled")
    assert sup.status == {"agent_worker": "disabled"}
    await sup.stop()


async def test_sleep_or_wake_returns_early_and_clears_event() -> None:
    wake, stop = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, wake.set)
    start = loop.time()
    await sleep_or_wake(wake, stop, 10)
    assert loop.time() - start < 1
    assert not wake.is_set()
