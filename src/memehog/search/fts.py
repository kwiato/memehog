from __future__ import annotations

import re
from typing import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Item

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def build_match_query(user_query: str) -> str:
    """Turn free-form user input into a safe FTS5 MATCH expression.

    Each token is quoted (so FTS5 operators/punctuation can't break the query)
    and prefix-matched, e.g. `kot w kapelu` → `"kot"* "w"* "kapelu"*`.
    """
    tokens = _TOKEN_RE.findall(user_query)
    return " ".join(f'"{token}"*' for token in tokens)


class FtsSearch:
    async def index_item(
        self,
        session: AsyncSession,
        item: Item,
        *,
        tags: Sequence[str] = (),
    ) -> None:
        await session.execute(
            text("DELETE FROM items_fts WHERE item_id = :id"), {"id": item.id}
        )
        await session.execute(
            text(
                "INSERT INTO items_fts (item_id, caption, filename, tags, ocr_text) "
                "VALUES (:id, :caption, :filename, :tags, '')"
            ),
            {
                "id": item.id,
                "caption": item.caption or "",
                "filename": item.filename,
                "tags": " ".join(tags),
            },
        )

    async def remove_item(self, session: AsyncSession, item_id: int) -> None:
        await session.execute(
            text("DELETE FROM items_fts WHERE item_id = :id"), {"id": item_id}
        )
        await session.execute(
            text("DELETE FROM vlm_fts WHERE item_id = :id"), {"id": item_id}
        )

    async def index_vlm(
        self, session: AsyncSession, item_id: int, profile_id: int, vlm_text: str
    ) -> None:
        """(Re)store one model's OCR+description text for one item."""
        await session.execute(
            text("DELETE FROM vlm_fts WHERE item_id = :id AND profile_id = :pid"),
            {"id": item_id, "pid": profile_id},
        )
        if vlm_text:
            await session.execute(
                text(
                    "INSERT INTO vlm_fts (item_id, profile_id, text) "
                    "VALUES (:id, :pid, :text)"
                ),
                {"id": item_id, "pid": profile_id, "text": vlm_text},
            )

    async def remove_profile(self, session: AsyncSession, profile_id: int) -> None:
        await session.execute(
            text("DELETE FROM vlm_fts WHERE profile_id = :pid"), {"pid": profile_id}
        )

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        limit: int,
        offset: int,
        profile_id: int | None = None,
    ) -> list[int]:
        """Match captions/filenames/tags plus VLM text — all models' text by
        default, or a single model's when `profile_id` is given."""
        match = build_match_query(query)
        if not match:
            return []
        rows = await session.execute(
            text(
                "SELECT item_id FROM ("
                "  SELECT item_id, rank FROM items_fts WHERE items_fts MATCH :match"
                "  UNION ALL"
                "  SELECT item_id, rank FROM vlm_fts WHERE vlm_fts MATCH :match"
                "    AND (:pid IS NULL OR profile_id = :pid)"
                ") GROUP BY item_id ORDER BY MIN(rank) "
                "LIMIT :limit OFFSET :offset"
            ),
            {
                "match": match,
                "pid": profile_id,
                "limit": limit,
                "offset": offset,
            },
        )
        return [row[0] for row in rows]
