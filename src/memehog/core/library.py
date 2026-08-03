from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Item, Tag, utcnow
from ..search.base import SearchBackend
from .media import make_thumbnail, probe, sha256_file

log = logging.getLogger(__name__)

# Items tagged with this are hidden from the default view and only shown in
# 🔥 mode. Their files also live under a "spicy/" subfolder of the library
# so the folder on disk mirrors the split.
SPICY_TAG = "spicy"


def is_nsfw_text(text: str | None) -> bool:
    """Marker convention: "nsfw" anywhere in a message/caption (any case)
    files the meme straight into the spicy stash."""
    return "nsfw" in (text or "").lower()


async def ingest_file(
    session: AsyncSession,
    settings: Settings,
    search: SearchBackend,
    src_path: Path,
    *,
    source_url: str | None = None,
    origin: str = "web",
    caption: str | None = None,
    uploader: str | None = None,
    spicy: bool = False,
) -> tuple[Item, bool]:
    """Move a downloaded/uploaded file into the library and register it.

    Returns (item, created). When the file already exists (same sha256) the
    source file is discarded and the existing item is returned with False.
    """
    sha = await asyncio.to_thread(sha256_file, src_path)
    existing = await session.scalar(select(Item).where(Item.sha256 == sha))
    if existing is not None:
        src_path.unlink(missing_ok=True)
        return existing, False

    info = await asyncio.to_thread(probe, src_path)
    now = utcnow()
    ext = src_path.suffix.lower() or ".bin"
    rel_path = f"{now:%Y}/{sha[:16]}{ext}"
    if spicy:
        rel_path = f"{SPICY_TAG}/{rel_path}"
    dest = settings.library_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.move, str(src_path), str(dest))

    thumb_rel = f"{sha[:16]}.jpg"
    thumb_ok = await asyncio.to_thread(
        make_thumbnail, dest, settings.thumbs_dir / thumb_rel, info.media_type
    )

    tags: list[Tag] = []
    if spicy:
        tag = await session.scalar(select(Tag).where(Tag.name == SPICY_TAG))
        if tag is None:
            tag = Tag(name=SPICY_TAG)
            session.add(tag)
            await session.flush()
        tags.append(tag)

    item = Item(
        sha256=sha,
        filename=rel_path,
        media_type=info.media_type,
        mime=info.mime,
        file_size=dest.stat().st_size,
        source_url=source_url,
        origin=origin,
        caption=caption,
        uploader=uploader,
        width=info.width,
        height=info.height,
        duration=info.duration,
        thumb_filename=thumb_rel if thumb_ok else None,
        # Initialize the collection so accessing .tags on this fresh instance
        # never triggers a sync lazy-load (forbidden under asyncio).
        tags=tags,
    )
    session.add(item)
    await session.flush()
    await search.index_item(session, item, tags=[t.name for t in tags])
    await session.commit()
    log.info("Ingested item %s (%s) from %s", item.id, rel_path, origin)
    return item, True


async def delete_item_files(settings: Settings, item: Item) -> None:
    (settings.library_dir / item.filename).unlink(missing_ok=True)
    if item.thumb_filename:
        (settings.thumbs_dir / item.thumb_filename).unlink(missing_ok=True)
