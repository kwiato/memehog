"""Perceptual hashing (dHash) for near-duplicate detection.

The same meme circulates re-encoded, re-scaled and re-compressed — sha256
only catches byte-identical copies. dHash reduces the image to a 64-bit
gradient fingerprint; two memes whose hashes differ in a few bits are the
same picture for our purposes.

Hashes are stored as 16-char hex strings (SQLite INTEGER is signed and the
top bit would go negative; hex sidesteps that).
"""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Hamming distance at or below which two images count as the same meme.
# 0–2 = re-encodes, ~5–8 = resized/recompressed copies; >10 risks false
# positives on template memes (same picture, different caption), which we
# actually want to keep as distinct entries.
DUP_THRESHOLD = 8


def dhash_image(img: Image.Image) -> str:
    """64-bit difference hash: 9×8 grayscale, each bit = left pixel brighter
    than its right neighbour."""
    small = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = small.tobytes()  # L mode: one byte per pixel, row-major
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return f"{bits:016x}"


def phash_file(path: Path) -> str | None:
    """Hash an image file; None when Pillow can't read it (videos — hash
    their thumbnail instead)."""
    try:
        with Image.open(path) as img:
            return dhash_image(img)
    except OSError as exc:
        log.warning("phash failed for %s: %s", path, exc)
        return None


# A flat-color image has no gradients — every bit comes out identical. Such
# hashes carry no signal, so they never count as matches (otherwise one plain
# background would swallow every other).
_DEGENERATE = {0, (1 << 64) - 1}


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def is_near(a: str | None, b: str | None, threshold: int = DUP_THRESHOLD) -> bool:
    if not a or not b:
        return False
    if int(a, 16) in _DEGENERATE or int(b, 16) in _DEGENERATE:
        return False
    return hamming(a, b) <= threshold


def near_any(
    needle: str | None,
    hashes,
    threshold: int = DUP_THRESHOLD,
) -> bool:
    """True when `needle` is a near-duplicate of any hash in the iterable.
    Linear scan — a few thousand 64-bit XORs is microseconds."""
    if not needle:
        return False
    n = int(needle, 16)
    if n in _DEGENERATE:
        return False
    for other in hashes:
        if not other:
            continue
        o = int(other, 16)
        if o not in _DEGENERATE and (n ^ o).bit_count() <= threshold:
            return True
    return False


async def backfill_phashes(session_factory, settings) -> int:
    """Fill in phash for items from before the column existed (nightly)."""
    import asyncio

    from sqlalchemy import select

    from ..db.models import Item

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    Item.id, Item.filename, Item.media_type, Item.thumb_filename
                ).where(Item.phash.is_(None))
            )
        ).all()
    done = 0
    for item_id, filename, media_type, thumb in rows:
        path = (
            settings.library_dir / filename
            if media_type != "video"
            else (settings.thumbs_dir / thumb if thumb else None)
        )
        if path is None or not path.exists():
            continue
        value = await asyncio.to_thread(phash_file, path)
        if value is None:
            continue
        async with session_factory() as session:
            item = await session.get(Item, item_id)
            if item is not None:
                item.phash = value
                await session.commit()
                done += 1
    if done:
        log.info("Backfilled phash for %d item(s)", done)
    return done


