"""Async SQLAlchemy engine over SQLite.

SQLite on a ReadWriteOnce PVC, matching the rest of the cluster's small
services. The Deployment is `strategy: Recreate` / `replicas: 1` for exactly
this reason — never run two writers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sms_relay.config import settings
from sms_relay.models import Base

logger = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _url() -> str:
    return f"sqlite+aiosqlite:///{settings.db_path}"


def get_engine():
    global _engine
    if _engine is None:
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(_url(), echo=False, future=True)

        # WAL lets the HTTP handlers read while the worker loop writes; without
        # it a busy worker blocks every GET behind the writer lock.
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def init_db() -> None:
    """Create tables if absent.

    Deliberately no Alembic: the schema is two tables owned entirely by this
    service, and create_all on startup is the whole migration story until it
    isn't. Additive columns can be handled here explicitly if that day comes.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database ready at %s", settings.db_path)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with get_sessionmaker()() as session:
        yield session


async def healthy() -> bool:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("database health check failed")
        return False
