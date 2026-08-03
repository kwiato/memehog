from __future__ import annotations

from typing import Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Item


class SearchBackend(Protocol):
    """Search over library items.

    The MVP ships :class:`~memehog.search.fts.FtsSearch` — SQLite FTS5 over
    caption/filename/tags plus per-model VLM text (OCR + AI descriptions,
    one copy per active model profile). A future vector backend (embeddings
    over the VLM descriptions — the `embeddings` table is in place) can
    implement the same protocol and be combined with FTS for hybrid search.
    """

    async def index_item(
        self,
        session: AsyncSession,
        item: Item,
        *,
        tags: Sequence[str] = (),
    ) -> None:
        """(Re)index one item's metadata. Called after ingest and tag changes."""
        ...

    async def remove_item(self, session: AsyncSession, item_id: int) -> None:
        ...

    async def index_vlm(
        self, session: AsyncSession, item_id: int, profile_id: int, vlm_text: str
    ) -> None:
        """(Re)store one model's OCR+description text for one item."""
        ...

    async def remove_profile(self, session: AsyncSession, profile_id: int) -> None:
        """Drop all of one model profile's text (profile deleted)."""
        ...

    async def search(
        self,
        session: AsyncSession,
        query: str,
        *,
        limit: int,
        offset: int,
        profile_id: int | None = None,
    ) -> list[int]:
        """Return matching item ids, best first; `profile_id` restricts the
        VLM text portion to a single model's data."""
        ...
