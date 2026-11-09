"""Polling scheduler.

Each tick claims due connectors with a short lease (UPDATE ... WHERE id IN (SELECT ... FOR UPDATE
SKIP LOCKED)), calls fetch_new() outside any transaction, then ingests each page and saves the
cursor in one transaction. When nothing is due it sleeps until the next due poll, capped at
SCHEDULER_IDLE_MAX_S, so an idle system queries the database rarely (plan R3).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import func, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.connectors import registry
from app.connectors.base import AuthExpiredError, RateLimitedError
from app.models import ConnectorAccount, SyncState
from app.services.connector_runtime import build_connector, pollable_sources
from app.services.ingest import ingest_messages
from app.workers.supervisor import sleep_or_wake

log = structlog.get_logger(__name__)

_CLAIM_SQL = text(
    """
    UPDATE sync_state AS s
    SET lease_until = now() + make_interval(secs => :lease_s),
        last_poll_started_at = now(),
        updated_at = now()
    FROM connector_accounts AS a
    WHERE a.id = s.connector_account_id
      AND s.connector_account_id IN (
        SELECT s2.connector_account_id
        FROM sync_state AS s2
        JOIN connector_accounts AS a2 ON a2.id = s2.connector_account_id
        WHERE a2.enabled
          AND a2.status = 'connected'
          AND a2.source = ANY(:sources)
          AND s2.next_poll_at <= now()
          AND (s2.lease_until IS NULL OR s2.lease_until < now())
        ORDER BY s2.next_poll_at
        LIMIT :limit
        FOR UPDATE OF s2 SKIP LOCKED
      )
    RETURNING s.connector_account_id, s.cursor, a.source
    """
)

_NEXT_DUE_SQL = text(
    """
    SELECT EXTRACT(EPOCH FROM (min(s.next_poll_at) - now()))
    FROM sync_state AS s
    JOIN connector_accounts AS a ON a.id = s.connector_account_id
    WHERE a.enabled AND a.status = 'connected' AND a.source = ANY(:sources)
    """
)


@dataclass(frozen=True)
class Claim:
    account_id: uuid.UUID
    cursor: str | None
    source: str


class Scheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        http: httpx.AsyncClient,
        wake: asyncio.Event,
        instance_id: str | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.http = http
        self.wake = wake
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run(self, stop: asyncio.Event) -> None:
        log.info("scheduler_started", instance=self.instance_id)
        while not stop.is_set():
            claimed = await self.tick()
            delay = 0.0 if claimed else await self.idle_delay()
            await sleep_or_wake(self.wake, stop, delay)

    async def tick(self) -> int:
        """Claim and poll every due connector (up to the batch size). Returns the number polled."""
        claims = await self.claim()
        for claim in claims:
            await self.poll(claim)
        return len(claims)

    async def claim(self) -> list[Claim]:
        sources = pollable_sources(self.settings)
        if not sources:
            return []
        async with self.sessionmaker() as session, session.begin():
            rows = (
                await session.execute(
                    _CLAIM_SQL,
                    {
                        "lease_s": float(self.settings.poll_lease_s),
                        "sources": sources,
                        "limit": self.settings.scheduler_batch_size,
                    },
                )
            ).all()
        return [Claim(account_id=r[0], cursor=r[1], source=r[2]) for r in rows]

    async def idle_delay(self) -> float:
        """Seconds until the next active connector is due, clamped to [min_sleep, idle_max]."""
        idle_max = self.settings.scheduler_idle_max_s
        sources = pollable_sources(self.settings)
        if not sources:
            return idle_max
        async with self.sessionmaker() as session:
            secs = (await session.execute(_NEXT_DUE_SQL, {"sources": sources})).scalar_one()
        if secs is None:
            return idle_max
        return max(self.settings.scheduler_min_sleep_s, min(idle_max, float(secs)))

    async def poll(self, claim: Claim) -> None:
        plog = log.bind(source=claim.source, instance=self.instance_id)
        interval = registry.get(claim.source).describe().poll_interval_s or 60
        async with self.sessionmaker() as session:
            account = await session.get(ConnectorAccount, claim.account_id)
        if account is None:
            return

        cursor = claim.cursor
        inserted = 0
        try:
            connector = build_connector(account, self.settings, self.http)
            for _page in range(self.settings.max_pages_per_poll):
                result = await asyncio.wait_for(
                    connector.fetch_new(cursor), timeout=self.settings.poll_timeout_s
                )
                async with self.sessionmaker() as session, session.begin():
                    ingested = await ingest_messages(session, account, result.messages)
                    if result.cursor is not None:
                        cursor = result.cursor
                    await session.execute(
                        update(SyncState)
                        .where(SyncState.connector_account_id == claim.account_id)
                        .values(
                            cursor=cursor,
                            # renew the lease: a multi-page poll may outlast the initial lease
                            lease_until=func.now() + timedelta(seconds=self.settings.poll_lease_s),
                            updated_at=func.now(),
                        )
                    )
                inserted += ingested.inserted
                if not result.has_more:
                    break
        except AuthExpiredError as exc:
            plog.warning("poll_auth_expired", error=str(exc))
            await self._record_failure(claim, interval, str(exc), needs_reauth=True)
        except RateLimitedError as exc:
            plog.warning("poll_rate_limited", retry_after=exc.retry_after)
            await self._record_failure(
                claim, max(interval, exc.retry_after), str(exc), count_failure=False
            )
        except Exception as exc:
            plog.exception("poll_failed")
            await self._record_failure(claim, interval, f"{type(exc).__name__}: {exc}")
        else:
            await self._record_success(claim, interval)
            if inserted:
                plog.info("poll_ingested", inserted=inserted)

    async def _record_success(self, claim: Claim, interval: float) -> None:
        async with self.sessionmaker() as session, session.begin():
            await session.execute(
                update(SyncState)
                .where(SyncState.connector_account_id == claim.account_id)
                .values(
                    next_poll_at=func.now() + timedelta(seconds=interval),
                    lease_until=None,
                    last_success_at=func.now(),
                    consecutive_failures=0,
                    last_error=None,
                    updated_at=func.now(),
                )
            )
            await session.execute(
                update(ConnectorAccount)
                .where(
                    ConnectorAccount.id == claim.account_id,
                    ConnectorAccount.last_error.is_not(None),
                )
                .values(last_error=None, last_error_at=None, updated_at=func.now())
            )

    async def _record_failure(
        self,
        claim: Claim,
        interval: float,
        error: str,
        *,
        needs_reauth: bool = False,
        count_failure: bool = True,
    ) -> None:
        error = error[:500]
        async with self.sessionmaker() as session, session.begin():
            state = await session.get(SyncState, claim.account_id, with_for_update=True)
            if state is None:
                return
            failures = state.consecutive_failures + (1 if count_failure else 0)
            delay = (
                min(interval * 2**failures, self.settings.max_failure_backoff_s)
                if count_failure
                else interval
            )
            state.consecutive_failures = failures
            state.last_error = error
            state.lease_until = None
            state.next_poll_at = datetime.now(UTC) + timedelta(seconds=delay)
            state.updated_at = datetime.now(UTC)
            values: dict[str, object] = {
                "last_error": error,
                "last_error_at": func.now(),
                "updated_at": func.now(),
            }
            if needs_reauth:
                values["status"] = "needs_reauth"
            await session.execute(
                update(ConnectorAccount)
                .where(ConnectorAccount.id == claim.account_id)
                .values(**values)
            )
