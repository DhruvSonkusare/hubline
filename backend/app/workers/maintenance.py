"""Housekeeping run by the agent worker (at start-up and every AGENT_MAINTENANCE_INTERVAL_S)."""

from __future__ import annotations

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# Jobs stuck in "running" (worker crashed or the app was redeployed mid-job) go back to pending,
# or to dead if they have used all their attempts. The message's agent_status follows.
_REAP_SQL = text(
    """
    WITH reaped AS (
        UPDATE agent_jobs
        SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'pending' END,
            run_after = now(),
            last_error = 'lease expired: worker stopped while running this job',
            finished_at = CASE WHEN attempts >= max_attempts THEN now() ELSE NULL END,
            updated_at = now()
        WHERE status = 'running' AND locked_at < now() - make_interval(secs => :stale_s)
        RETURNING message_id, status
    )
    UPDATE messages AS m
    SET agent_status = CASE
            WHEN r.status = 'pending' THEN 'pending'
            WHEN m.triaged_at IS NULL THEN 'failed'
            ELSE 'done'
        END,
        agent_error = CASE WHEN r.status = 'dead'
            THEN 'agent stopped repeatedly while processing this message' ELSE NULL END
    FROM reaped AS r
    WHERE m.id = r.message_id
    RETURNING m.id
    """
)


async def reap_stale_jobs(sessionmaker: async_sessionmaker[AsyncSession], stale_s: int) -> int:
    async with sessionmaker() as session, session.begin():
        rows = (await session.execute(_REAP_SQL, {"stale_s": float(stale_s)})).all()
    if rows:
        log.warning("reaped_stale_agent_jobs", count=len(rows))
    return len(rows)
