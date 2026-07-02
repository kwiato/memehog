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
        ocr_text: str = "",
    ) -> None:
        await self.remove_item(session, item.id)
        if not ocr_text:
            row = await session.execute(
                text("SELECT ocr_text FROM items_fts WHERE item_id = :id"),
                {"id": item.id},
            )
            existing = row.scalar_one_or_none()
            ocr_text = existing or ""
        await session.execute(
            text(
                "INSERT INTO items_fts (item_id, caption, filename, tags, ocr_text) "
                "VALUES (:id, :caption, :filename, :tags, :ocr)"
            ),
            {
                "id": item.id,
                "caption": item.caption or "",
                "filename": item.filename,
                "tags": " ".join(tags),
                "ocr": ocr_text,
            },
        )

    async def remove_item(self, session: AsyncSession, item_id: int) -> None:
        await session.execute(
            text("DELETE FROM items_fts WHERE item_id = :id"), {"id": item_id}
        )

    async def search(
        self, session: AsyncSession, query: str, *, limit: int, offset: int
    ) -> list[int]:
        match = build_match_query(query)
        if not match:
            return []
        rows = await session.execute(
            text(
                "SELECT item_id FROM items_fts WHERE items_fts MATCH :match "
                "ORDER BY rank LIMIT :limit OFFSET :offset"
            ),
            {"match": match, "limit": limit, "offset": offset},
        )
        return [row[0] for row in rows]
