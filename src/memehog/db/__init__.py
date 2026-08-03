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
    VlmText,
)

# Regular FTS5 tables (not contentless) so per-row DELETE works everywhere;
# the indexed text is tiny compared to the media files. `ocr_text` is legacy —
# per-model VLM text lives in vlm_fts now (one row per item × profile).
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    item_id UNINDEXED,
    caption,
    filename,
    tags,
    ocr_text
)
"""

VLM_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS vlm_fts USING fts5(
    item_id UNINDEXED,
    profile_id UNINDEXED,
    text
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
        await conn.execute(text(VLM_FTS_DDL))
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

    # v0.5: single "selected" VLM profile → per-profile active toggles, with
    # OCR/description text stored per profile (vlm_texts + vlm_fts). Migrate
    # the legacy single-model data under the previously selected profile.
    cols = {
        row[1] for row in await conn.execute(text("PRAGMA table_info(vlm_profiles)"))
    }
    if "active" not in cols:
        await conn.execute(
            text("ALTER TABLE vlm_profiles ADD COLUMN active BOOLEAN NOT NULL DEFAULT 0")
        )
        selected = (
            await conn.execute(
                text("SELECT value FROM app_settings WHERE key = 'vlm_profile_id'")
            )
        ).scalar_one_or_none()
        if selected and selected.strip().isdigit():
            pid = int(selected)
            await conn.execute(
                text("UPDATE vlm_profiles SET active = 1 WHERE id = :pid"),
                {"pid": pid},
            )
            await conn.execute(
                text(
                    "INSERT INTO vlm_texts (item_id, profile_id, text, created_at) "
                    "SELECT item_id, :pid, ocr_text, CURRENT_TIMESTAMP "
                    "FROM items_fts WHERE ocr_text != ''"
                ),
                {"pid": pid},
            )
            await conn.execute(
                text(
                    "INSERT INTO vlm_fts (item_id, profile_id, text) "
                    "SELECT item_id, :pid, ocr_text "
                    "FROM items_fts WHERE ocr_text != ''"
                ),
                {"pid": pid},
            )
            await conn.execute(text("UPDATE items_fts SET ocr_text = ''"))
        await conn.execute(
            text("DELETE FROM app_settings WHERE key = 'vlm_profile_id'")
        )
