"""Verify a database end to end: apply migrations, check the schema matches the models, then boot
the app against it (background workers off) and call /readyz.

    uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL

The URL is read from the environment or backend/.env and is never printed (only its host).
Side effects: runs `alembic upgrade head` and the app's start-up registry sync (creates
connector_accounts rows). It does not enable any connector or start polling.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from asgi_lifespan import LifespanManager
from dotenv import dotenv_values
from sqlalchemy import Connection, text

import app.models  # noqa: F401
from app.config import Settings
from app.db.base import Base
from app.db.engine import create_engine
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, Base.metadata))


async def _schema_check(url: str) -> tuple[str, list[object]]:
    engine = create_engine(url, pool_size=1)
    try:
        async with engine.connect() as conn:
            revision = (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
            diff = await conn.run_sync(_diff)
    finally:
        await engine.dispose()
    return str(revision), diff


async def _readyz(url: str) -> tuple[int, str]:
    base = Settings()
    settings = base.model_copy(
        update={
            "database_url": url,
            "enable_scheduler": False,
            "enable_agent_worker": False,
            "demo_mode": False,
            "log_level": "WARNING",
        }
    )
    app = create_app(settings)
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://v") as c,
    ):
        r = await c.get("/readyz")
    return r.status_code, r.text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--var", default="NEON_DEV_DATABASE_URL")
    args = parser.parse_args()
    url = os.environ.get(args.var) or dotenv_values(BACKEND_DIR / ".env").get(args.var)
    if not url:
        print(f"{args.var} is not set (environment or backend/.env).")
        return 2
    print(f"target host: {urlsplit(url).hostname}")

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    revision, diff = asyncio.run(_schema_check(url))
    print(f"migrations: at revision {revision}; schema diff vs models: {diff or 'none'}")

    status, body = asyncio.run(_readyz(url))
    print(f"/readyz: HTTP {status} {body}")
    return 0 if (not diff and status == 200) else 1


if __name__ == "__main__":
    sys.exit(main())
