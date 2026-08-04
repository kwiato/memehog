"""Daily meme crawler feeding the swipe inbox.

Sources (settings, one per line):
    reddit:<subreddit>      — top posts of the day via the public JSON listing
    rss:<feed url>          — any RSS/Atom feed with images (enclosure,
                              media:content or an <img> in the description)

Each day builds one batch of at most `crawler_daily_target` candidates.
Candidates are just URL + local thumbnail; the full file is downloaded only
when the owner swipes right, and then goes through the normal ingest
pipeline (sha dedupe, conversion, AI indexing queue). Near-duplicates of
library items, rejected memes and other candidates are filtered by dHash.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx
from PIL import Image
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from ..config import Settings
from ..db.models import Candidate, Item, RejectedHash, utcnow
from .phash import dhash_image, near_any

log = logging.getLogger(__name__)

USER_AGENT = "memehog crawler (self-hosted meme library)"
MEDIA_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
CAND_THUMB_SIZE = 640
CRAWLER_JOB_ID = "crawler-daily"

_IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"


@dataclass
class Found:
    source: str
    page_url: str
    media_url: str
    title: str
    score: int = 0


def parse_sources(raw: str) -> list[tuple[str, int | None]]:
    """One source per line, with an optional per-day cap after whitespace:
    "reddit:memes 40" takes at most 40 candidates from that subreddit."""
    out: list[tuple[str, int | None]] = []
    for line in (raw or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        cap = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        out.append((parts[0], cap))
    return out


def _is_image_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(MEDIA_EXTS)


async def _fetch_reddit(client: httpx.AsyncClient, sub: str) -> list[Found]:
    resp = await client.get(
        f"https://www.reddit.com/r/{sub}/top.json",
        params={"t": "day", "limit": 60},
    )
    resp.raise_for_status()
    found: list[Found] = []
    for child in resp.json().get("data", {}).get("children", []):
        post = child.get("data", {})
        if post.get("over_18") or post.get("stickied"):
            continue
        url = post.get("url_overridden_by_dest") or post.get("url") or ""
        if not _is_image_url(url):
            continue  # galleries, videos, crossposts — keep it simple
        found.append(
            Found(
                source=f"reddit:{sub}",
                page_url="https://www.reddit.com" + (post.get("permalink") or ""),
                media_url=url,
                title=(post.get("title") or "").strip(),
                score=int(post.get("ups") or 0),
            )
        )
    found.sort(key=lambda f: f.score, reverse=True)
    return found


def _rss_image(entry: ET.Element) -> str:
    for enc in entry.iter("enclosure"):
        if (enc.get("type") or "").startswith("image/") or _is_image_url(
            enc.get("url") or ""
        ):
            return enc.get("url") or ""
    for media in entry.iter(f"{_MEDIA_NS}content"):
        if _is_image_url(media.get("url") or ""):
            return media.get("url") or ""
    for field in ("description", "{http://www.w3.org/2005/Atom}content"):
        node = entry.find(field)
        if node is not None and node.text:
            match = _IMG_SRC_RE.search(node.text)
            if match:
                return match.group(1)
    return ""


async def _fetch_rss(client: httpx.AsyncClient, url: str) -> list[Found]:
    resp = await client.get(url)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    host = urlparse(url).hostname or "rss"
    found: list[Found] = []
    entries = root.iter("item")  # RSS 2.0
    atom_entries = list(root.iter("{http://www.w3.org/2005/Atom}entry"))
    for entry in list(entries) + atom_entries:
        media = _rss_image(entry)
        if not media:
            continue
        # NB: explicit None checks — ElementTree elements are falsy when
        # they have no children, so `find(...) or find(...)` misfires.
        title_node = entry.find("title")
        if title_node is None:
            title_node = entry.find("{http://www.w3.org/2005/Atom}title")
        link_node = entry.find("link")
        link = ""
        if link_node is not None:
            link = (link_node.text or link_node.get("href") or "").strip()
        found.append(
            Found(
                source=f"rss:{host}",
                page_url=link,
                media_url=media,
                title=(title_node.text or "").strip() if title_node is not None else "",
            )
        )
    return found


async def fetch_source(client: httpx.AsyncClient, source: str) -> list[Found]:
    kind, _, arg = source.partition(":")
    if kind == "reddit":
        return await _fetch_reddit(client, arg)
    if kind == "rss":
        return await _fetch_rss(client, arg)
    raise ValueError(f"unknown source type: {source!r}")


async def download_media(
    client: httpx.AsyncClient, url: str
) -> bytes | None:
    """Fetch media bytes with a size cap; None on any failure."""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        if len(resp.content) > MAX_DOWNLOAD_BYTES:
            return None
        return resp.content
    except httpx.HTTPError as exc:
        log.info("crawler: download failed %s: %s", url, exc)
        return None


def _thumb_and_hash(data: bytes) -> tuple[bytes, str] | None:
    """JPEG inbox thumbnail + dHash from raw image bytes; None if unreadable."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            phash = dhash_image(img)
            img = img.convert("RGB")
            img.thumbnail((CAND_THUMB_SIZE, CAND_THUMB_SIZE))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return buf.getvalue(), phash
    except OSError:
        return None


def _interleave(per_source: list[list[Found]]) -> list[Found]:
    """Round-robin across sources so one huge subreddit doesn't crowd out
    the small feeds; each source's list stays in its own quality order."""
    out: list[Found] = []
    while any(per_source):
        for bucket in per_source:
            if bucket:
                out.append(bucket.pop(0))
    return out


async def crawl_once(
    session_factory,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    today: str | None = None,
) -> int:
    """Build (or top up) today's candidate batch. Returns candidates added."""
    from . import appsettings

    async with session_factory() as session:
        effective = await appsettings.effective_settings(session, settings)
    sources = parse_sources(effective.crawler_sources)
    if not sources:
        return 0
    today = today or f"{datetime.now():%Y-%m-%d}"

    async with session_factory() as session:
        # Yesterday's unswiped leftovers go away — every day starts fresh.
        stale = (
            await session.scalars(
                select(Candidate).where(
                    Candidate.day < today, Candidate.status == "pending"
                )
            )
        ).all()
        for cand in stale:
            if cand.thumb_filename:
                (settings.candidates_dir / cand.thumb_filename).unlink(
                    missing_ok=True
                )
        await session.execute(
            sa_delete(Candidate).where(
                Candidate.day < today, Candidate.status == "pending"
            )
        )
        await session.commit()

        remaining = effective.crawler_daily_target - (
            await session.scalar(
                select(func.count(Candidate.id)).where(Candidate.day == today)
            )
            or 0
        )
        if remaining <= 0:
            return 0

        known_urls = set(
            (await session.scalars(select(Candidate.media_url))).all()
        )
        seen_hashes = list(
            (await session.scalars(
                select(Item.phash).where(Item.phash.is_not(None))
            )).all()
        ) + list(
            (await session.scalars(select(RejectedHash.phash))).all()
        ) + list(
            (await session.scalars(
                select(Candidate.phash).where(Candidate.phash.is_not(None))
            )).all()
        )

    added = 0
    async with httpx.AsyncClient(
        transport=transport,
        headers={"User-Agent": USER_AGENT},
        timeout=30,
        follow_redirects=True,
    ) as client:
        per_source: list[list[Found]] = []
        # Per-day cap for each source, keyed by the label its Found rows
        # carry (== the config token for reddit; "rss:<host>" for feeds).
        caps: dict[str, int | None] = {}
        for source, cap in sources:
            try:
                fetched = await fetch_source(client, source)
            except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
                log.warning("crawler: source %s failed: %s", source, exc)
                continue
            per_source.append(fetched)
            if fetched:
                caps[fetched[0].source] = cap
        queue = _interleave(per_source)
        log.info(
            "crawler: %d candidate(s) from %d source(s), need %d",
            len(queue), len(per_source), remaining,
        )

        per_label_added: dict[str, int] = {}
        for found in queue:
            if added >= remaining:
                break
            cap = caps.get(found.source)
            if cap is not None and per_label_added.get(found.source, 0) >= cap:
                continue
            if found.media_url in known_urls:
                continue
            known_urls.add(found.media_url)
            data = await download_media(client, found.media_url)
            if data is None:
                continue
            packed = await asyncio.to_thread(_thumb_and_hash, data)
            if packed is None:
                continue
            thumb_bytes, phash = packed
            if near_any(phash, seen_hashes):
                continue
            seen_hashes.append(phash)

            thumb_rel = (
                hashlib.sha256(found.media_url.encode()).hexdigest()[:16] + ".jpg"
            )
            settings.candidates_dir.mkdir(parents=True, exist_ok=True)
            (settings.candidates_dir / thumb_rel).write_bytes(thumb_bytes)
            async with session_factory() as session:
                session.add(
                    Candidate(
                        source=found.source,
                        page_url=found.page_url,
                        media_url=found.media_url,
                        title=found.title[:500],
                        score=found.score,
                        phash=phash,
                        thumb_filename=thumb_rel,
                        day=today,
                        created_at=utcnow(),
                    )
                )
                await session.commit()
            added += 1
            per_label_added[found.source] = (
                per_label_added.get(found.source, 0) + 1
            )

    log.info("crawler: added %d candidate(s) for %s", added, today)
    return added
