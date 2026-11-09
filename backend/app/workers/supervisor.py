"""Runs long-lived background loops inside the API process, restarting them if they crash."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)

LoopFactory = Callable[[asyncio.Event], Awaitable[None]]


@dataclass
class Wakeups:
    """In-process nudges so API actions take effect without waiting for an idle sleep."""

    scheduler: asyncio.Event = field(default_factory=asyncio.Event)
    agent: asyncio.Event = field(default_factory=asyncio.Event)


async def sleep_or_wake(wake: asyncio.Event, stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, returning early if `wake` or `stop` is set."""
    if seconds > 0 and not (wake.is_set() or stop.is_set()):
        waiters = {asyncio.ensure_future(wake.wait()), asyncio.ensure_future(stop.wait())}
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
    else:
        await asyncio.sleep(0)
    wake.clear()


class Supervisor:
    def __init__(self, *, max_restart_backoff_s: float = 30.0) -> None:
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.status: dict[str, str] = {}
        self._max_backoff = max_restart_backoff_s

    def start(self, name: str, loop: LoopFactory) -> None:
        self.status[name] = "starting"
        self._tasks[name] = asyncio.create_task(self._run_forever(name, loop), name=name)

    def mark(self, name: str, status: str) -> None:
        """Record a non-running worker's status (e.g. "disabled")."""
        self.status[name] = status

    async def _run_forever(self, name: str, loop: LoopFactory) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.status[name] = "running"
            try:
                await loop(self._stop)
                if self._stop.is_set():
                    break
                log.warning("worker_returned_unexpectedly", worker=name)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker_crashed", worker=name, restart_in_s=backoff)
            self.status[name] = "restarting"
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self._max_backoff)
        self.status[name] = "stopped"

    async def stop(self, grace_s: float = 10.0) -> None:
        self._stop.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks.values(), timeout=grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for name in self._tasks:
            self.status[name] = "stopped"
