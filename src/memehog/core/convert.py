from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path

from PIL import Image
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db.models import Item
from ..search.base import SearchBackend
from .media import make_thumbnail, probe, sha256_file

log = logging.getLogger(__name__)

# webp/webm are poorly supported by many services (messengers, older
# browsers/apps), so the nightly job transcodes them to jpg/mp4.
CONVERT_EXTS = {".webp": ".jpg", ".webm": ".mp4"}


def _convert_webp(src: Path, dst: Path) -> bool:
    with Image.open(src) as img:
        if getattr(img, "is_animated", False):
            log.info("Skipping animated webp %s (not supported yet)", src.name)
            return False
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            background.save(dst, "JPEG", quality=92)
        else:
            img.convert("RGB").save(dst, "JPEG", quality=92)
    return True


def _convert_webm(src: Path, dst: Path) -> bool:
    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found; cannot convert %s", src.name)
        return False
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst),
        ],
        capture_output=True,
        timeout=1800,
    )
    if result.returncode != 0:
        log.warning(
            "ffmpeg conversion failed for %s: %s",
            src.name, result.stderr.decode(errors="replace")[:500],
        )
        return False
    return dst.exists()


def _convert_file(src: Path, dst: Path) -> bool:
    if src.suffix.lower() == ".webp":
        return _convert_webp(src, dst)
    return _convert_webm(src, dst)


async def convert_item(
    session: AsyncSession,
    settings: Settings,
    search: SearchBackend,
    item: Item,
) -> bool:
    """Transcode one webp/webm item in place: new file, sha, thumb, index."""
    old_rel = item.filename
    src = settings.library_dir / old_rel
    if not src.exists():
        log.warning("Item %s file missing: %s", item.id, old_rel)
        return False
    target_ext = CONVERT_EXTS[src.suffix.lower()]

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = settings.tmp_dir / f"conv-{item.id}{target_ext}"
    try:
        ok = await asyncio.to_thread(_convert_file, src, tmp_out)
        if not ok:
            return False

        new_sha = await asyncio.to_thread(sha256_file, tmp_out)
        collision = await session.scalar(
            select(Item).where(Item.sha256 == new_sha, Item.id != item.id)
        )
        if collision is not None:
            log.info(
                "Converted %s collides with item %s — keeping the original",
                old_rel, collision.id,
            )
            return False

        new_rel = str(Path(old_rel).parent / f"{new_sha[:16]}{target_ext}").replace("\\", "/")
        dest = settings.library_dir / new_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(tmp_out), str(dest))

        info = await asyncio.to_thread(probe, dest)
        old_thumb = item.thumb_filename
        new_thumb = f"{new_sha[:16]}.jpg"
        thumb_ok = await asyncio.to_thread(
            make_thumbnail, dest, settings.thumbs_dir / new_thumb, info.media_type
        )

        item.sha256 = new_sha
        item.filename = new_rel
        item.media_type = info.media_type
        item.mime = info.mime
        item.file_size = dest.stat().st_size
        item.width = info.width
        item.height = info.height
        item.duration = info.duration
        item.thumb_filename = new_thumb if thumb_ok else None
        await search.index_item(session, item, tags=[t.name for t in item.tags])
        await session.commit()

        src.unlink(missing_ok=True)
        if old_thumb and old_thumb != new_thumb:
            (settings.thumbs_dir / old_thumb).unlink(missing_ok=True)
        log.info("Converted item %s: %s → %s", item.id, old_rel, new_rel)
        return True
    finally:
        tmp_out.unlink(missing_ok=True)


async def run_conversions(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    search: SearchBackend,
) -> int:
    """Convert all pending webp/webm items; returns how many were converted."""
    async with session_factory() as session:
        items = (
            await session.scalars(
                select(Item)
                .where(
                    or_(
                        Item.filename.like("%.webp"),
                        Item.filename.like("%.webm"),
                    )
                )
                .options(selectinload(Item.tags))
            )
        ).all()

        converted = 0
        for item in items:
            try:
                if await convert_item(session, settings, search, item):
                    converted += 1
            except Exception:  # noqa: BLE001 - one bad file must not stop the batch
                log.exception("Conversion failed for item %s", item.id)
                await session.rollback()
    if items:
        log.info("Nightly conversion: %d/%d file(s) converted", converted, len(items))
    return converted
