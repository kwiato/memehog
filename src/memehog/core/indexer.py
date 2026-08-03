"""Nightly VLM indexer.

Sends each new item's thumbnail to a vision model behind any OpenAI-compatible
chat-completions endpoint (Gemini, OpenRouter, Groq, Mistral, local Ollama...)
and stores the returned OCR text + description in the FTS index, making memes
searchable by the text on them and by what they show.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Item, ItemTag, Tag, VlmProfile, VlmText
from ..search.base import SearchBackend
from .appsettings import effective_settings, ensure_profile_from_env
from .library import SPICY_TAG
from .media import make_thumbnail

log = logging.getLogger(__name__)

PROMPT = """\
You are indexing a meme library for full-text search.
Return ONLY a JSON object with exactly these keys:
{{"ocr_text": "...", "description": "...", "tags": ["..."]}}
- ocr_text: all text visible in the image, transcribed verbatim in its original
  language ("" if there is none). For long walls of text, transcribe up to
  roughly the first 200 words and stop.
- description: 1-3 sentences in {language} describing what the image shows and,
  if recognizable, the meme template or character names. Use words people would
  type when searching for this meme.
- tags: 0-{max_tags} short lowercase tags in {language} for the topic, meme
  template or vibe.{tags_hint}
No markdown fences, no commentary — just the JSON object."""

# At most this many AI tags may ride on one item, across all models.
MAX_AI_TAGS_PER_ITEM = 4
# How many existing tags to offer the model as its preferred vocabulary.
TAGS_HINT_LIMIT = 40

# Models often wrap JSON in ```json fences despite instructions.
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# Stop the run after this many failures in a row (bad config / provider down);
# isolated bad items just stay pending and are retried next night.
MAX_CONSECUTIVE_FAILURES = 3

# Overload/rate-limit responses are retried with these back-off delays (s)
# before giving up on the run; auth/config errors (401, 404...) stop at once.
TRANSIENT_HTTP = {429, 500, 502, 503, 504, 529}
TRANSIENT_BACKOFF = (15, 30, 60)


class IndexerStatus:
    """In-memory progress of the current/last indexer run.

    One process, one indexer — a module-level singleton is enough. The web UI
    polls it to show a live log while a run is in progress.
    """

    def __init__(self) -> None:
        self.running = False
        self.total = 0
        self.processed = 0
        self.indexed = 0
        self.log: deque[str] = deque(maxlen=30)

    def start(self) -> None:
        self.running = True
        self.total = 0
        self.processed = 0
        self.indexed = 0
        self.log.clear()

    def note(self, message: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {message}")
        log.debug("indexer: %s", message)


STATUS = IndexerStatus()


def _parse_response(content: str) -> tuple[str, str, list[str]]:
    match = _JSON_RE.search(content)
    if not match:
        raise ValueError(f"no JSON object in VLM response: {content[:200]!r}")
    data = json.loads(match.group(0))
    ocr = str(data.get("ocr_text") or "").strip()
    description = str(data.get("description") or "").strip()
    raw_tags = data.get("tags") or []
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    return ocr, description, tags


def _load_thumb_jpeg(settings: Settings, item: Item) -> bytes | None:
    """The 512px thumbnail (made at ingest for images and videos alike) is the
    ideal VLM input: small, JPEG, and for videos already a sampled frame."""
    if item.thumb_filename:
        thumb = settings.thumbs_dir / item.thumb_filename
        if thumb.exists():
            return thumb.read_bytes()
    src = settings.library_dir / item.filename
    if not src.exists():
        return None
    tmp = settings.tmp_dir / f"vlm-{item.id}.jpg"
    try:
        if make_thumbnail(src, tmp, item.media_type):
            return tmp.read_bytes()
        return None
    finally:
        tmp.unlink(missing_ok=True)


async def describe_image(
    client: httpx.AsyncClient,
    settings: Settings,
    jpeg: bytes,
    tags_hint: str = "",
) -> tuple[str, str, list[str]]:
    """One chat-completions call; returns (ocr_text, description, tags)."""
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    prompt = PROMPT.format(
        language=settings.vlm_language,
        max_tags=MAX_AI_TAGS_PER_ITEM - 1,
        tags_hint=(
            f" Prefer reusing these existing tags when they fit: {tags_hint}; "
            f"invent a new tag only when none of them fits."
            if tags_hint else ""
        ),
    )
    payload = {
        "model": settings.vlm_model,
        # Text-heavy memes (chat screenshots) need generous room — a tight cap
        # truncates the JSON mid-string. Output tokens are billed as used, so
        # the high ceiling costs nothing on ordinary memes.
        "max_tokens": 2048,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    }
    headers = {}
    if settings.vlm_api_key:
        headers["Authorization"] = f"Bearer {settings.vlm_api_key}"
    resp = await client.post(
        settings.vlm_base_url.rstrip("/") + "/chat/completions",
        json=payload,
        headers=headers,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_response(content)


def _normalize_tag(raw: str) -> str:
    return " ".join(raw.lower().split())[:32]


async def _apply_ai_tags(
    session: AsyncSession,
    search: SearchBackend,
    item: Item,
    tag_names: list[str],
) -> list[str]:
    """Attach model-proposed tags (marked source='ai') and refresh the FTS row.

    Dedupes against the item's existing tags, never touches the reserved
    spicy tag, and caps the number of AI tags per item."""
    existing = dict(
        (
            await session.execute(
                select(Tag.name, ItemTag.source)
                .join(ItemTag, ItemTag.tag_id == Tag.id)
                .where(ItemTag.item_id == item.id)
            )
        ).all()
    )
    ai_count = sum(1 for source in existing.values() if source == "ai")
    added: list[str] = []
    for raw in tag_names:
        name = _normalize_tag(raw)
        if not name or name == SPICY_TAG or name in existing:
            continue
        if ai_count + len(added) >= MAX_AI_TAGS_PER_ITEM:
            break
        tag = await session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            await session.flush()
        session.add(ItemTag(item_id=item.id, tag_id=tag.id, source="ai"))
        added.append(name)
    if added:
        await session.flush()
        await search.index_item(session, item, tags=list(existing) + added)
    return added


async def _index_one(
    session: AsyncSession,
    trial: Settings,
    search: SearchBackend,
    client: httpx.AsyncClient,
    item: Item,
    profile: SimpleNamespace,  # plain snapshot — survives session.rollback()
    tags_hint: str = "",
) -> bool:
    jpeg = await asyncio.to_thread(_load_thumb_jpeg, trial, item)
    if jpeg is None:
        log.warning("Item %s: no readable media for VLM, marking failed", item.id)
        STATUS.note(f"Item {item.id}: no readable media — marked failed")
        item.index_status = "failed"
        await session.commit()
        return False
    ocr, description, tags = await describe_image(client, trial, jpeg, tags_hint)
    fts_text = "\n".join(part for part in (ocr, description) if part)
    session.add(VlmText(item_id=item.id, profile_id=profile.id, text=fts_text))
    await search.index_vlm(session, item.id, profile.id, fts_text)
    added_tags: list[str] = []
    if trial.vlm_auto_tag and tags:
        added_tags = await _apply_ai_tags(session, search, item, tags)
    item.index_status = "indexed"
    await session.commit()
    log.info("Indexed item %s with %s (%d chars OCR, %d chars description)",
             item.id, profile.name, len(ocr), len(description))
    tag_note = f" 🏷 {', '.join(added_tags)}" if added_tags else ""
    STATUS.note(
        f"{profile.name} / item {item.id}: ok — "
        f"„{(description or ocr)[:70]}”{tag_note}"
    )
    return True


async def run_indexing(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    search: SearchBackend,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run every active model profile over the items it hasn't seen yet.

    Each profile keeps its own copy of the OCR/description text, so one
    provider having a bad night doesn't leave memes unindexed — the other
    active models still do their pass. Returns how many (item, model) pairs
    were indexed. `transport` is injectable for tests.
    """
    if STATUS.running:
        log.info("VLM indexer already running — skipping this trigger")
        return 0
    STATUS.start()

    indexed = 0
    try:
        async with session_factory() as session:
            # Settings saved in the web UI override the .env values.
            settings = await effective_settings(session, settings)
            await ensure_profile_from_env(session, settings)
            profiles = list(
                await session.scalars(
                    select(VlmProfile)
                    .where(VlmProfile.active.is_(True))
                    .order_by(VlmProfile.id)
                )
            )
            if not profiles:
                log.debug("VLM indexer: no active model profiles — skipping")
                STATUS.note(
                    "No active models — add or enable one in the AI models tab."
                )
                return 0
            delay = 60.0 / settings.vlm_rpm if settings.vlm_rpm > 0 else 0.0

            # Per-profile work queues. Only plain values (id snapshots) are
            # kept for the processing loop — a session.rollback() after a
            # failure expires ORM objects, and touching one then blows up
            # outside the async context.
            queues: list[tuple[SimpleNamespace, list[int]]] = []
            for orm_profile in profiles:
                snap = SimpleNamespace(
                    id=orm_profile.id,
                    name=orm_profile.name,
                    model=orm_profile.model,
                    base_url=orm_profile.base_url,
                    api_key=orm_profile.api_key,
                )
                done = select(VlmText.item_id).where(
                    VlmText.profile_id == snap.id
                )
                stmt = (
                    select(Item.id)
                    .where(Item.index_status != "failed", Item.id.not_in(done))
                    .order_by(Item.id)
                    .limit(settings.vlm_max_per_run)
                )
                if not settings.vlm_index_spicy:
                    stmt = stmt.where(Item.filename.not_like("spicy/%"))
                ids = list(await session.scalars(stmt))
                if ids:
                    queues.append((snap, ids))
            if not queues:
                STATUS.note("Nothing to do — every active model is up to date.")
                return 0
            STATUS.total = sum(len(ids) for _, ids in queues)
            summary = ", ".join(f"{p.name}: {len(ids)}" for p, ids in queues)
            log.info("VLM indexer: %s item(s) to process (%s)",
                     STATUS.total, summary)
            STATUS.note(f"Starting — {summary}")

            # Existing tags (most-used first) as the model's preferred
            # vocabulary, so auto-tagging converges instead of sprawling.
            tags_hint = ""
            if settings.vlm_auto_tag:
                rows = await session.execute(
                    select(Tag.name)
                    .join(ItemTag, ItemTag.tag_id == Tag.id)
                    .where(Tag.name != SPICY_TAG)
                    .group_by(Tag.id)
                    .order_by(func.count(ItemTag.item_id).desc())
                    .limit(TAGS_HINT_LIMIT)
                )
                tags_hint = ", ".join(name for (name,) in rows)

            async with httpx.AsyncClient(timeout=120, transport=transport) as client:
                for profile, item_ids in queues:
                    trial = settings.model_copy(
                        update={
                            "vlm_base_url": profile.base_url,
                            "vlm_api_key": profile.api_key,
                            "vlm_model": profile.model,
                        }
                    )
                    STATUS.note(f"—— {profile.name} ({profile.model}) ——")
                    consecutive_failures = 0
                    skip_profile = False
                    for i, item_id in enumerate(item_ids):
                        attempts = 0
                        try:
                            while True:
                                item = await session.get(Item, item_id)
                                if item is None:  # deleted while we were running
                                    break
                                try:
                                    if await _index_one(
                                        session, trial, search, client,
                                        item, profile, tags_hint,
                                    ):
                                        indexed += 1
                                        consecutive_failures = 0
                                    break
                                except (
                                    httpx.HTTPStatusError,
                                    httpx.TransportError,
                                ) as exc:
                                    # Timeouts and network errors are transient
                                    # by nature; HTTP errors only for overload
                                    # codes.
                                    if isinstance(exc, httpx.HTTPStatusError):
                                        code = exc.response.status_code
                                        label = f"HTTP {code}"
                                        detail = exc.response.text[:120]
                                        transient = code in TRANSIENT_HTTP
                                    else:
                                        label = type(exc).__name__
                                        detail = str(exc)[:120]
                                        transient = True
                                    await session.rollback()
                                    if (
                                        transient
                                        and attempts < len(TRANSIENT_BACKOFF)
                                    ):
                                        wait = TRANSIENT_BACKOFF[attempts]
                                        attempts += 1
                                        log.info(
                                            "%s: VLM API %s — retry %d/%d in %ds",
                                            profile.name, label, attempts,
                                            len(TRANSIENT_BACKOFF), wait,
                                        )
                                        STATUS.note(
                                            f"{profile.name}: {label} (transient) "
                                            f"— retry {attempts}/"
                                            f"{len(TRANSIENT_BACKOFF)} in {wait}s"
                                        )
                                        await asyncio.sleep(wait)
                                        continue
                                    # Auth/model errors, or transient errors
                                    # that survived all retries — give up on
                                    # THIS model and move on to the next one;
                                    # its items stay queued for next run.
                                    log.warning(
                                        "%s: VLM API error %s — skipping this "
                                        "model: %s", profile.name, label, detail,
                                    )
                                    STATUS.note(
                                        f"{profile.name}: API error {label}: "
                                        f"{detail} — skipping this model, "
                                        f"moving on"
                                    )
                                    skip_profile = True
                                    break
                            if skip_profile:
                                break
                        except Exception as exc:  # noqa: BLE001 - keep batch alive
                            log.exception(
                                "VLM indexing failed for item %s (%s)",
                                item_id, profile.name,
                            )
                            STATUS.note(
                                f"{profile.name} / item {item_id}: error — "
                                f"{type(exc).__name__}: {str(exc)[:110]}"
                            )
                            await session.rollback()
                            consecutive_failures += 1
                            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                                log.warning(
                                    "%s: %d consecutive failures — skipping "
                                    "this model", profile.name,
                                    consecutive_failures,
                                )
                                STATUS.note(
                                    f"{profile.name}: {consecutive_failures} "
                                    f"consecutive failures — skipping this "
                                    f"model, moving on"
                                )
                                skip_profile = True
                        finally:
                            STATUS.processed += 1
                            STATUS.indexed = indexed
                        if skip_profile:
                            # Remaining items won't be attempted this run —
                            # keep the progress bar honest.
                            STATUS.total -= len(item_ids) - (i + 1)
                            break
                        if delay and i < len(item_ids) - 1:
                            await asyncio.sleep(delay)

        log.info("VLM indexer: %d/%d (item, model) pair(s) indexed",
                 indexed, STATUS.total)
        STATUS.note(f"Done: {indexed}/{STATUS.total} indexed.")
        return indexed
    finally:
        STATUS.running = False
