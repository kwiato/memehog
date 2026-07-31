from __future__ import annotations

from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import (  # noqa: F401
    AppSetting,
    Base,
    Embedding,
    Item,
    ItemTag,
    Job,
    Submission,
    Tag,
    TelegramClient,
    VlmProfile,
    VlmSample,
)

# Regular FTS5 table (not contentless) so per-row DELETE works everywhere;
# the indexed text is tiny compared to the media files.
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED,
    caption,
    filename,
    tags,
    ocr_text
)
"""


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def create_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _sqlite_pragmas)
    return engine


async def init_db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text(FTS_DDL))
        await _migrate(conn)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _migrate(conn) -> None:
    """create_all only creates missing tables; columns added in later versions
    are patched in by hand (no alembic for a hobby app)."""
    cols = {row[1] for row in await conn.execute(text("PRAGMA table_info(jobs)"))}
    if "spicy" not in cols:
        await conn.execute(
            text("ALTER TABLE jobs ADD COLUMN spicy BOOLEAN NOT NULL DEFAULT 0")
        )
