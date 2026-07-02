from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db.models import Item, ItemTag, Tag
from ..search.base import SearchBackend
from .library import delete_item_files

PAGE_SIZE = 60


async def list_items(
    session: AsyncSession,
    search: SearchBackend,
    *,
    q: str = "",
    tag: str = "",
    media_type: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> list[Item]:
    offset = (max(page, 1) - 1) * page_size

    if q.strip():
        # Over-fetch from FTS so post-filters (tag/type) can still fill a page.
        ids = await search.search(
            session, q, limit=page_size * 4 + offset, offset=0
        )
        if not ids:
            return []
        stmt = (
            select(Item)
            .where(Item.id.in_(ids))
            .options(selectinload(Item.tags))
        )
        stmt = _apply_filters(stmt, tag=tag, media_type=media_type)
        rows = (await session.scalars(stmt)).all()
        by_id = {item.id: item for item in rows}
        ordered = [by_id[i] for i in ids if i in by_id]
        return ordered[offset : offset + page_size]

    stmt = (
        select(Item)
        .options(selectinload(Item.tags))
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(page_size)
        .offset(offset)
    )
    stmt = _apply_filters(stmt, tag=tag, media_type=media_type)
    return list((await session.scalars(stmt)).all())


def _apply_filters(stmt, *, tag: str, media_type: str):
    if media_type:
        stmt = stmt.where(Item.media_type == media_type)
    if tag:
        stmt = stmt.join(ItemTag, ItemTag.item_id == Item.id).join(
            Tag, Tag.id == ItemTag.tag_id
        ).where(Tag.name == tag)
    return stmt


async def get_item(session: AsyncSession, item_id: int) -> Item | None:
    return await session.scalar(
        select(Item).where(Item.id == item_id).options(selectinload(Item.tags))
    )


async def count_items(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count(Item.id)))) or 0


async def all_tags(session: AsyncSession) -> list[Tag]:
    return list((await session.scalars(select(Tag).order_by(Tag.name))).all())


async def delete_item(
    session: AsyncSession, settings: Settings, search: SearchBackend, item: Item
) -> None:
    await delete_item_files(settings, item)
    await search.remove_item(session, item.id)
    await session.execute(sa_delete(ItemTag).where(ItemTag.item_id == item.id))
    # Statement delete instead of session.delete(item): the ORM cascade would
    # try to lazy-load relationships, which is forbidden under asyncio.
    await session.execute(sa_delete(Item).where(Item.id == item.id))
    session.expunge(item)
    await session.commit()


async def add_tag(
    session: AsyncSession, search: SearchBackend, item: Item, name: str
) -> Item:
    name = name.strip().lower()
    if not name:
        return item
    tag = await session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name)
        session.add(tag)
        await session.flush()
    linked = await session.scalar(
        select(ItemTag).where(ItemTag.item_id == item.id, ItemTag.tag_id == tag.id)
    )
    if linked is None:
        session.add(ItemTag(item_id=item.id, tag_id=tag.id))
        await session.flush()
    return await _reindex(session, search, item.id)


async def remove_tag(
    session: AsyncSession, search: SearchBackend, item: Item, name: str
) -> Item:
    tag = await session.scalar(select(Tag).where(Tag.name == name.strip().lower()))
    if tag is not None:
        await session.execute(
            sa_delete(ItemTag).where(
                ItemTag.item_id == item.id, ItemTag.tag_id == tag.id
            )
        )
    return await _reindex(session, search, item.id)


async def _reindex(session: AsyncSession, search: SearchBackend, item_id: int) -> Item:
    session.expire_all()
    item = await get_item(session, item_id)
    assert item is not None
    await search.index_item(session, item, tags=[t.name for t in item.tags])
    await session.commit()
    return item
