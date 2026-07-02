from __future__ import annotations

from typing import Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Item


class SearchBackend(Protocol):
    """Search over library items.

    The MVP ships :class:`~memehog.search.fts.FtsSearch` (SQLite FTS5 over
    caption/filename/tags/OCR text). A future vector backend (local CLIP model
    or a cloud embedding API) implements the same protocol and can be combined
    with FTS for hybrid search — the `embeddings` table and the `ocr_text`
    column are already in place for it.
    """

    async def index_item(
        self,
        session: AsyncSession,
        item: Item,
        *,
        tags: Sequence[str] = (),
        ocr_text: str = "",
    ) -> None:
        """(Re)index one item. Called after ingest and after tag changes."""
        ...

    async def remove_item(self, session: AsyncSession, item_id: int) -> None:
        ...

    async def search(
        self, session: AsyncSession, query: str, *, limit: int, offset: int
    ) -> list[int]:
        """Return matching item ids, best first."""
        ...
