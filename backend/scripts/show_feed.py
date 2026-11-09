"""Print the latest messages with the agent's priority, summary and current draft.

    uv run python -m scripts.show_feed            # 10 newest inbound messages
    uv run python -m scripts.show_feed --limit 30

Reads DATABASE_URL from backend/.env. A stop-gap until the dashboard exists (Step 3).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import textwrap

from sqlalchemy import select

from app.config import DatabaseSettings
from app.db.engine import create_engine, create_sessionmaker
from app.models import Draft, Message


async def main(limit: int) -> None:
    engine = create_engine(DatabaseSettings().database_url, pool_size=1)  # type: ignore[call-arg]
    sm = create_sessionmaker(engine)
    try:
        async with sm() as s:
            msgs = (
                (
                    await s.execute(
                        select(Message)
                        .where(Message.direction == "inbound")
                        .order_by(Message.received_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            for m in msgs:
                draft = (
                    await s.execute(
                        select(Draft)
                        .where(Draft.message_id == m.id)
                        .order_by(Draft.version.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                demo = " [DEMO]" if m.is_demo else ""
                print(
                    f"P{m.priority or '?'} {m.agent_status:<8} {m.source}{demo} "
                    f"{m.received_at:%Y-%m-%d %H:%M} {m.sender_name or m.sender_id}"
                )
                print(f"   summary: {m.summary or '-'}")
                if m.agent_error:
                    print(f"   error:   {m.agent_error}")
                if draft:
                    body = textwrap.indent(draft.body, "            ").strip()
                    print(f"   draft v{draft.version} ({draft.model}): {body}")
                print()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=10)
    asyncio.run(main(p.parse_args().limit))
