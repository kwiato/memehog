from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from sqlalchemy import case
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db.models import Item, ItemTag, Tag
from ..search.base import SearchBackend
from .library import SPICY_TAG, delete_item_files
from .media import make_thumbnail, probe, sha256_file

PAGE_SIZE = 60


def _spicy_ids():
    return (
        select(ItemTag.item_id)
        .join(Tag, Tag.id == ItemTag.tag_id)
        .where(Tag.name == SPICY_TAG)
    )


async def list_items(
    session: AsyncSession,
    search: SearchBackend,
    *,
    q: str = "",
    tag: str = "",
    media_type: str = "",
    spicy: bool = False,
    lang: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
    model_profile_id: int | None = None,
) -> list[Item]:
    offset = (max(page, 1) - 1) * page_size

    if q.strip():
        # Over-fetch from FTS so post-filters (tag/type/spicy) can still fill a page.
        ids = await search.search(
            session, q, limit=page_size * 4 + offset, offset=0,
            profile_id=model_profile_id,
        )
        if not ids:
            return []
        stmt = (
            select(Item)
            .where(Item.id.in_(ids))
            .options(selectinload(Item.tags))
        )
        stmt = _apply_filters(
            stmt, tag=tag, media_type=media_type, spicy=spicy, lang=lang
        )
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
    stmt = _apply_filters(
        stmt, tag=tag, media_type=media_type, spicy=spicy, lang=lang
    )
    return list((await session.scalars(stmt)).all())


def _apply_filters(stmt, *, tag: str, media_type: str, spicy: bool, lang: str = ""):
    if media_type:
        stmt = stmt.where(Item.media_type == media_type)
    if lang:
        stmt = stmt.where(Item.lang == lang)
    if tag:
        stmt = stmt.join(ItemTag, ItemTag.item_id == Item.id).join(
            Tag, Tag.id == ItemTag.tag_id
        ).where(Tag.name == tag)
    if spicy:
        stmt = stmt.where(Item.id.in_(_spicy_ids()))
    else:
        stmt = stmt.where(Item.id.not_in(_spicy_ids()))
    return stmt


async def random_feed(
    session: AsyncSession,
    *,
    seed: int,
    tag: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> list[Item]:
    """Stable pseudo-random ordering for the public feed: the seed is drawn
    once per feed session and carried in the query string, so infinite scroll
    pages never repeat or reshuffle. Spicy is always excluded."""
    stmt = (
        select(Item)
        .options(selectinload(Item.tags))
        .order_by((Item.id * seed) % 999983, Item.id)
        .limit(page_size)
        .offset((max(page, 1) - 1) * page_size)
    )
    stmt = _apply_filters(stmt, tag=tag, media_type="", spicy=False)
    return list((await session.scalars(stmt)).all())


async def random_item(
    session: AsyncSession, *, spicy: bool = False, exclude_id: int | None = None
) -> Item | None:
    stmt = (
        select(Item)
        .options(selectinload(Item.tags))
        .order_by(func.random())
        .limit(1)
    )
    if exclude_id is not None:
        stmt = stmt.where(Item.id != exclude_id)
    stmt = _apply_filters(stmt, tag="", media_type="", spicy=spicy)
    return await session.scalar(stmt)


async def get_item(session: AsyncSession, item_id: int) -> Item | None:
    return await session.scalar(
        select(Item).where(Item.id == item_id).options(selectinload(Item.tags))
    )


async def count_items(session: AsyncSession) -> int:
    return (await session.scalar(select(func.count(Item.id)))) or 0


async def ai_tag_names(session: AsyncSession, item_id: int) -> set[str]:
    """Names of this item's tags that were attached by the AI indexer."""
    rows = await session.execute(
        select(Tag.name)
        .join(ItemTag, ItemTag.tag_id == Tag.id)
        .where(ItemTag.item_id == item_id, ItemTag.source == "ai")
    )
    return {name for (name,) in rows}


async def all_tags(session: AsyncSession, include_spicy: bool = False) -> list[Tag]:
    stmt = select(Tag).order_by(Tag.name)
    if not include_spicy:
        stmt = stmt.where(Tag.name != SPICY_TAG)
    return list((await session.scalars(stmt)).all())


async def replace_item_file(
    session: AsyncSession,
    settings: Settings,
    search: SearchBackend,
    item: Item,
    src_path: Path,
) -> tuple[bool, str]:
    """Swap an item's media file for a new version (e.g. a crop): new sha,
    new filename in the same folder, fresh thumbnail, FTS reindex. Returns
    (ok, message) — a content collision with another item refuses the swap."""
    new_sha = await asyncio.to_thread(sha256_file, src_path)
    collision = await session.scalar(
        select(Item).where(Item.sha256 == new_sha, Item.id != item.id)
    )
    if collision is not None:
        src_path.unlink(missing_ok=True)
        return False, f"identical to meme #{collision.id} — nothing changed"

    old_rel, old_thumb = item.filename, item.thumb_filename
    new_rel = str(
        Path(old_rel).parent / f"{new_sha[:16]}{src_path.suffix.lower()}"
    ).replace("\\", "/")
    dest = settings.library_dir / new_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src_path), str(dest))

    info = await asyncio.to_thread(probe, dest)
    new_thumb = f"{new_sha[:16]}.jpg"
    thumb_ok = await asyncio.to_thread(
        make_thumbnail, dest, settings.thumbs_dir / new_thumb, info.media_type
    )

    item.sha256 = new_sha
    item.filename = new_rel
    item.mime = info.mime
    item.file_size = dest.stat().st_size
    item.width = info.width
    item.height = info.height
    item.thumb_filename = new_thumb if thumb_ok else None
    await search.index_item(session, item, tags=[t.name for t in item.tags])
    await session.commit()

    if old_rel != new_rel:
        (settings.library_dir / old_rel).unlink(missing_ok=True)
    if old_thumb and old_thumb != new_thumb:
        (settings.thumbs_dir / old_thumb).unlink(missing_ok=True)
    return True, new_rel


async def tag_stats(session: AsyncSession) -> list[dict]:
    """All tags with usage counts, most-used first: name, items, ai_items."""
    rows = await session.execute(
        select(
            Tag.name,
            func.count(ItemTag.item_id),
            func.coalesce(
                func.sum(case((ItemTag.source == "ai", 1), else_=0)), 0
            ),
        )
        .join(ItemTag, ItemTag.tag_id == Tag.id, isouter=True)
        .group_by(Tag.id)
        .order_by(func.count(ItemTag.item_id).desc(), Tag.name)
    )
    return [
        {"name": name, "items": items, "ai_items": ai_items}
        for name, items, ai_items in rows
    ]


async def delete_tag(
    session: AsyncSession, search: SearchBackend, name: str
) -> int:
    """Remove a tag everywhere; returns how many items were affected.

    The reserved spicy tag is never deleted this way — it drives file
    placement and the hidden view."""
    name = name.strip().lower()
    if name == SPICY_TAG:
        return 0
    tag = await session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        return 0
    item_ids = list(
        await session.scalars(
            select(ItemTag.item_id).where(ItemTag.tag_id == tag.id)
        )
    )
    await session.execute(sa_delete(ItemTag).where(ItemTag.tag_id == tag.id))
    await session.execute(sa_delete(Tag).where(Tag.id == tag.id))
    await session.flush()
    session.expire_all()
    for item_id in item_ids:
        item = await get_item(session, item_id)
        if item is not None:
            await search.index_item(
                session, item, tags=[t.name for t in item.tags]
            )
    await session.commit()
    return len(item_ids)


async def clean_unused_tags(session: AsyncSession) -> int:
    """Delete every tag that isn't attached to any item; returns the count."""
    used = select(ItemTag.tag_id).distinct()
    unused_ids = list(
        await session.scalars(
            select(Tag.id).where(Tag.id.not_in(used), Tag.name != SPICY_TAG)
        )
    )
    if unused_ids:
        await session.execute(sa_delete(Tag).where(Tag.id.in_(unused_ids)))
        await session.commit()
    return len(unused_ids)


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


async def toggle_spicy(
    session: AsyncSession, settings: Settings, search: SearchBackend, item: Item
) -> Item:
    make_spicy = SPICY_TAG not in {t.name for t in item.tags}
    await _move_item_file(session, settings, item, spicy=make_spicy)
    if make_spicy:
        return await add_tag(session, search, item, SPICY_TAG)
    return await remove_tag(session, search, item, SPICY_TAG)


async def _move_item_file(
    session: AsyncSession, settings: Settings, item: Item, *, spicy: bool
) -> None:
    """Keep the on-disk split in sync with the tag: spicy files live under
    "spicy/" in the library. Missing files are left alone (path unchanged)."""
    old_rel = item.filename
    prefix = f"{SPICY_TAG}/"
    if spicy:
        new_rel = old_rel if old_rel.startswith(prefix) else prefix + old_rel
    else:
        new_rel = old_rel.removeprefix(prefix)
    src = settings.library_dir / old_rel
    if new_rel == old_rel or not src.exists():
        return
    dest = settings.library_dir / new_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src), str(dest))
    item.filename = new_rel
    await session.flush()


async def _reindex(session: AsyncSession, search: SearchBackend, item_id: int) -> Item:
    session.expire_all()
    item = await get_item(session, item_id)
    assert item is not None
    await search.index_item(session, item, tags=[t.name for t in item.tags])
    await session.commit()
    return item
