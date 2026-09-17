"""Async engine / session factory. Everything goes through DATABASE_URL (Postgres-ready)."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from models.db import Base, User

logger = logging.getLogger("seo.db")

engine = create_async_engine(get_settings().database_url, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding one session per request."""
    async with async_session_factory() as session:
        yield session


async def ensure_user(session: AsyncSession, user_id: str) -> None:
    """Make sure a users row exists for this session id, tolerating a concurrent insert.

    Both OAuth integrations call this before writing their credential row. The
    obvious "SELECT then INSERT" loses a race when two callbacks for the same
    session land at once (a double-clicked Connect button is enough) and the
    loser fails the whole commit on the primary key. Anyone who inserted it is
    fine by us - only the row's existence matters.
    """
    if await session.get(User, user_id) is not None:
        return
    savepoint = await session.begin_nested()
    try:
        session.add(User(id=user_id))
        await session.flush()
    except IntegrityError:
        await savepoint.rollback()
        logger.debug("users row %s was created concurrently", user_id)
    else:
        await savepoint.commit()


def _add_missing_columns(sync_conn: Any) -> None:
    """Add columns that were introduced after a dev database was first created.

    `create_all` only creates missing TABLES, so a new column on an existing
    table would otherwise surface as "no such column" at query time. This is a
    dev-only stopgap for SQLite until Alembic lands.
    """
    if sync_conn.dialect.name != "sqlite":
        return
    inspector = inspect(sync_conn)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # freshly created by create_all, already correct
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            if not (column.nullable or column.server_default is not None):
                logger.warning(
                    "Cannot auto-add NOT NULL column %s.%s - recreate the dev database",
                    table.name, column.name,
                )
                continue
            ddl = column.type.compile(sync_conn.dialect)
            sync_conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {ddl}'))
            logger.info("Added missing column %s.%s to the dev database", table.name, column.name)


def _sqlite_path(database_url: str) -> Path | None:
    """Filesystem path behind a SQLite DATABASE_URL; None for any other backend."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    if not url.database or url.database == ":memory:":
        return None
    return Path(url.database)


async def init_db() -> None:
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_add_missing_columns)
    except OperationalError as exc:
        path = _sqlite_path(get_settings().database_url)
        if path is None or path.parent.exists():
            raise
        # sqlite creates the database FILE but never its parent directory, so an
        # absolute DATABASE_URL - an unmounted volume, or a Linux path used on
        # Windows - fails here with a bare "unable to open database file" buried in
        # a 200-line SQLAlchemy traceback. The directory is deliberately NOT created
        # automatically: on a production host that would silently put the database
        # outside the intended volume, to be lost on the next deploy.
        raise RuntimeError(
            f"Cannot open the SQLite database at {path.resolve()}: its directory "
            f"{path.parent.resolve()} does not exist. Create it (or mount the volume), "
            f"or point DATABASE_URL at a writable path. DATABASE_URL is "
            f"{get_settings().database_url!r}."
        ) from exc
