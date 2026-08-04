from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Item, RejectedHash, Submission, utcnow
from ..search.base import SearchBackend
from .library import ingest_file, is_nsfw_text
from .media import sha256_file
from .phash import near_any, phash_file

log = logging.getLogger(__name__)

# Anti-spam limits for non-whitelisted senders.
MAX_PENDING_PER_USER = 3
MAX_PER_DAY_PER_USER = 10


async def create_submission(
    session: AsyncSession,
    settings: Settings,
    src_path: Path,
    *,
    submitter_id: int,
    submitter_name: str | None = None,
    caption: str | None = None,
) -> tuple[Submission | None, str]:
    """Quarantine a guest upload for moderation.

    Returns (submission, "ok") or (None, reason) where reason is one of
    "duplicate" (already in the library or already submitted) or a limit name.
    The source file is consumed either way.
    """
    try:
        if await _pending_count(session, submitter_id) >= MAX_PENDING_PER_USER:
            return None, "too_many_pending"
        if await _today_count(session, submitter_id) >= MAX_PER_DAY_PER_USER:
            return None, "daily_limit"

        sha = await asyncio.to_thread(sha256_file, src_path)
        in_library = await session.scalar(select(Item).where(Item.sha256 == sha))
        already_submitted = await session.scalar(
            select(Submission).where(
                Submission.sha256 == sha, Submission.status == "pending"
            )
        )
        if in_library is not None or already_submitted is not None:
            return None, "duplicate"

        # sha only catches byte-identical files — dHash also rejects the same
        # meme re-encoded/re-scaled, or one the owner already swiped away.
        phash = await asyncio.to_thread(phash_file, src_path)
        if phash is not None:
            known = list(
                (await session.scalars(
                    select(Item.phash).where(Item.phash.is_not(None))
                )).all()
            ) + list(
                (await session.scalars(select(RejectedHash.phash))).all()
            )
            if near_any(phash, known):
                return None, "duplicate"

        ext = src_path.suffix.lower() or ".bin"
        rel = f"{sha[:16]}{ext}"
        dest = settings.pending_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(src_path), str(dest))
    finally:
        src_path.unlink(missing_ok=True)

    submission = Submission(
        sha256=sha,
        filename=rel,
        caption=caption,
        submitter_id=submitter_id,
        submitter_name=submitter_name,
    )
    session.add(submission)
    await session.commit()
    log.info(
        "Quarantined submission %s from %s (%s)",
        submission.id, submitter_id, rel,
    )
    return submission, "ok"


async def approve_submission(
    session: AsyncSession,
    settings: Settings,
    search: SearchBackend,
    submission: Submission,
) -> Item | None:
    """Ingest an approved submission into the library. Returns the item
    (existing one if the content raced in through another door meanwhile)."""
    src = settings.pending_dir / submission.filename
    item = None
    if src.exists():
        item, _ = await ingest_file(
            session, settings, search, src,
            origin="telegram",
            caption=submission.caption,
            uploader=submission.submitter_name or str(submission.submitter_id),
            # Guests can self-mark: "nsfw" in the caption files it as spicy.
            spicy=is_nsfw_text(submission.caption),
        )
    submission.status = "approved"
    submission.item_id = item.id if item is not None else None
    await session.commit()
    return item


async def reject_submission(
    session: AsyncSession, settings: Settings, submission: Submission
) -> None:
    (settings.pending_dir / submission.filename).unlink(missing_ok=True)
    submission.status = "rejected"
    await session.commit()


async def get_submission(
    session: AsyncSession, submission_id: int
) -> Submission | None:
    return await session.get(Submission, submission_id)


def set_vote_msgs(submission: Submission, refs: list[tuple[int, int]]) -> None:
    submission.vote_msgs = json.dumps(refs)


def get_vote_msgs(submission: Submission) -> list[tuple[int, int]]:
    if not submission.vote_msgs:
        return []
    return [(int(c), int(m)) for c, m in json.loads(submission.vote_msgs)]


async def _pending_count(session: AsyncSession, submitter_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(Submission.id)).where(
                Submission.submitter_id == submitter_id,
                Submission.status == "pending",
            )
        )
    ) or 0


async def _today_count(session: AsyncSession, submitter_id: int) -> int:
    since = utcnow() - timedelta(days=1)
    return (
        await session.scalar(
            select(func.count(Submission.id)).where(
                Submission.submitter_id == submitter_id,
                Submission.created_at >= since,
            )
        )
    ) or 0
