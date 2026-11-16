"""Run scheduler/agent work on demand: POST /internal/tick, TICK_ON_STARTUP, and "kicks" from
API actions.

Why: on Render free the instance sleeps after 15 minutes without traffic, and Neon free should
suspend after 5 idle minutes. So production runs WITHOUT background loops
(ENABLE_SCHEDULER=false, ENABLE_AGENT_WORKER=false): an external cron calls /internal/tick
every 30 minutes, the app ticks once when it boots, and API actions (enable/sync a connector,
generate a sample, later webhooks) kick one pass in the background. With loops enabled (local
dev), kicks just wake the loops.

All paths use the same SKIP LOCKED claims, so they are safe to overlap with each other and
with the loops.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from dataclasses import asdict, dataclass
from typing import Any

import structlog

from app.config import Settings
from app.workers.agent_worker import AgentWorker
from app.workers.scheduler import Scheduler
from app.workers.supervisor import Wakeups

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class TickResult:
    polled: int  # connectors polled in the scheduler pass
    agent_jobs: int  # agent jobs processed
    elapsed_s: float
    stopped: str  # drained | time_limit | rate_limited | budget | busy

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkRunner:
    def __init__(
        self,
        scheduler: Scheduler,
        worker: AgentWorker,
        settings: Settings,
        wakeups: Wakeups,
        *,
        scheduler_loop: bool,
        worker_loop: bool,
    ) -> None:
        self.scheduler = scheduler
        self.worker = worker
        self.settings = settings
        self.wakeups = wakeups
        self.scheduler_loop = scheduler_loop
        self.worker_loop = worker_loop
        self._lock = asyncio.Lock()
        self._rerun = False  # agent work arrived while a pass was running
        self._rescan = False  # a connector became due while a pass was running (Sync now)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    # ------------------------------------------------------------------ explicit tick

    async def tick(self, budget_s: float | None = None) -> TickResult:
        """One scheduler pass, then drain agent jobs until done or `budget_s` has elapsed."""
        if self._lock.locked():
            self._rerun = True
            self._rescan = True  # the running pass polls again before it finishes
            return TickResult(0, 0, 0.0, "busy")
        start = time.monotonic()
        deadline = start + (budget_s or self.settings.tick_budget_s)
        async with self._lock:
            self._rescan = False
            polled = await self.scheduler.tick()
            jobs, stopped = await self._drain(deadline)
            # "Sync now" / dashboard open during this pass: its scheduler phase may be over,
            # so poll once more instead of leaving the connector due until the next cron tick.
            while self._rescan and time.monotonic() < deadline:
                self._rescan = False
                polled += await self.scheduler.tick()
                more, stopped = await self._drain(deadline)
                jobs += more
        result = TickResult(polled, jobs, round(time.monotonic() - start, 2), stopped)
        log.info("tick_done", **result.as_dict())
        return result

    async def _drain(self, deadline: float) -> tuple[int, str]:
        total = 0
        while True:
            self._rerun = False
            if time.monotonic() >= deadline:
                return total, "time_limit"
            processed = await self.worker.tick()
            total += processed
            if processed == 0 and not self._rerun:
                return total, self.worker.idle_reason

    # ------------------------------------------------------------------ kicks

    def kick_agent(self) -> None:
        """New agent jobs exist: wake the loop, or drain once in the background."""
        if self.worker_loop:
            self.wakeups.agent.set()
        elif self.settings.work_on_demand:
            if self._lock.locked():
                self._rerun = True  # the running pass will pick it up
            else:
                self._spawn(self._drain_once())

    def kick_scheduler(self) -> None:
        """A connector became due (enabled / sync now): wake the loop, or tick once."""
        if self.scheduler_loop:
            self.wakeups.scheduler.set()
        elif self.settings.work_on_demand:
            if self._lock.locked():
                self._rescan = True  # the running pass picks it up before finishing
            else:
                self._spawn(self.tick())

    async def _drain_once(self) -> None:
        async with self._lock:
            jobs, stopped = await self._drain(time.monotonic() + self.settings.tick_budget_s)
        log.info("on_demand_drain_done", agent_jobs=jobs, stopped=stopped)

    def spawn_tick(self) -> None:
        self._spawn(self.tick())

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        if self._stopping:
            coro.close()
            return
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background_work_failed", error=repr(task.exception()))

    async def idle(self) -> None:
        """Wait for background kicks to finish (tests, graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self, grace_s: float = 10.0) -> None:
        self._stopping = True
        if not self._tasks:
            return
        _, pending = await asyncio.wait(list(self._tasks), timeout=grace_s)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
