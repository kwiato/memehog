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

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from ..config import Settings
from ..db.models import Item
from ..search.base import SearchBackend
from .appsettings import effective_settings
from .media import make_thumbnail

log = logging.getLogger(__name__)

PROMPT = """\
You are indexing a meme library for full-text search.
Return ONLY a JSON object with exactly these keys:
{{"ocr_text": "...", "description": "..."}}
- ocr_text: all text visible in the image, transcribed verbatim in its original
  language ("" if there is none).
- description: 1-3 sentences in {language} describing what the image shows and,
  if recognizable, the meme template or character names. Use words people would
  type when searching for this meme.
No markdown fences, no commentary — just the JSON object."""

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


def _parse_response(content: str) -> tuple[str, str]:
    match = _JSON_RE.search(content)
    if not match:
        raise ValueError(f"no JSON object in VLM response: {content[:200]!r}")
    data = json.loads(match.group(0))
    ocr = str(data.get("ocr_text") or "").strip()
    description = str(data.get("description") or "").strip()
    return ocr, description


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
    client: httpx.AsyncClient, settings: Settings, jpeg: bytes
) -> tuple[str, str]:
    """One chat-completions call; returns (ocr_text, description)."""
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    payload = {
        "model": settings.vlm_model,
        "max_tokens": 600,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {
                        "type": "text",
                        "text": PROMPT.format(language=settings.vlm_language),
                    },
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


async def _index_one(
    session: AsyncSession,
    settings: Settings,
    search: SearchBackend,
    client: httpx.AsyncClient,
    item: Item,
) -> bool:
    jpeg = await asyncio.to_thread(_load_thumb_jpeg, settings, item)
    if jpeg is None:
        log.warning("Item %s: no readable media for VLM, marking failed", item.id)
        STATUS.note(f"Item {item.id}: no readable media — marked failed")
        item.index_status = "failed"
        await session.commit()
        return False
    ocr, description = await describe_image(client, settings, jpeg)
    fts_text = "\n".join(part for part in (ocr, description) if part)
    await search.index_item(
        session, item, tags=[t.name for t in item.tags], ocr_text=fts_text
    )
    item.index_status = "indexed"
    await session.commit()
    log.info("Indexed item %s (%d chars OCR, %d chars description)",
             item.id, len(ocr), len(description))
    STATUS.note(f"Item {item.id}: ok — „{(description or ocr)[:70]}”")
    return True


async def run_indexing(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    search: SearchBackend,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Index pending items via the configured VLM; returns how many succeeded.

    `transport` is injectable for tests.
    """
    if STATUS.running:
        log.info("VLM indexer already running — skipping this trigger")
        return 0
    STATUS.start()

    indexed = 0
    consecutive_failures = 0
    try:
        async with session_factory() as session:
            # Settings saved in the web UI override the .env values.
            settings = await effective_settings(session, settings)
            if not settings.vlm_enabled:
                log.debug(
                    "VLM indexer not configured (VLM_BASE_URL / VLM_MODEL) — skipping"
                )
                STATUS.note("Not configured — set the endpoint and model first.")
                return 0
            delay = 60.0 / settings.vlm_rpm if settings.vlm_rpm > 0 else 0.0

            # Only ids here — items are (re)fetched one by one inside the loop,
            # so a session.rollback() after a failure can't leave us holding
            # expired ORM objects (lazy refresh outside the async context).
            stmt = (
                select(Item.id)
                .where(Item.index_status == "pending")
                .order_by(Item.id)
                .limit(settings.vlm_max_per_run)
            )
            if not settings.vlm_index_spicy:
                stmt = stmt.where(Item.filename.not_like("spicy/%"))
            item_ids = list(await session.scalars(stmt))
            if not item_ids:
                STATUS.note("Nothing to do — no pending items.")
                return 0
            total = len(item_ids)
            STATUS.total = total
            log.info("VLM indexer: %d item(s) to process (model %s)",
                     total, settings.vlm_model)
            STATUS.note(f"Starting: {total} item(s), model {settings.vlm_model}")

            async with httpx.AsyncClient(timeout=120, transport=transport) as client:
                stop_run = False
                for i, item_id in enumerate(item_ids):
                    attempts = 0
                    try:
                        while True:
                            item = await session.get(
                                Item, item_id, options=[selectinload(Item.tags)]
                            )
                            if item is None:  # deleted while we were running
                                break
                            try:
                                if await _index_one(
                                    session, settings, search, client, item
                                ):
                                    indexed += 1
                                    consecutive_failures = 0
                                break
                            except httpx.HTTPStatusError as exc:
                                code = exc.response.status_code
                                await session.rollback()
                                if (
                                    code in TRANSIENT_HTTP
                                    and attempts < len(TRANSIENT_BACKOFF)
                                ):
                                    # Overloaded model / rate limit — wait and
                                    # retry the same item.
                                    wait = TRANSIENT_BACKOFF[attempts]
                                    attempts += 1
                                    log.info(
                                        "VLM API HTTP %s — retry %d/%d in %ds",
                                        code, attempts, len(TRANSIENT_BACKOFF), wait,
                                    )
                                    STATUS.note(
                                        f"HTTP {code} (overloaded?) — retry "
                                        f"{attempts}/{len(TRANSIENT_BACKOFF)} "
                                        f"in {wait}s"
                                    )
                                    await asyncio.sleep(wait)
                                    continue
                                # Auth/model errors, or transient errors that
                                # survived all retries — stop the whole run and
                                # leave the rest pending for next night.
                                log.warning(
                                    "VLM API returned %s — stopping this run: %s",
                                    code, exc.response.text[:300],
                                )
                                STATUS.note(
                                    f"API error HTTP {code}: "
                                    f"{exc.response.text[:120]} — run stopped, "
                                    f"the rest stays pending"
                                )
                                stop_run = True
                                break
                        if stop_run:
                            break
                    except Exception as exc:  # noqa: BLE001 - keep the batch alive
                        log.exception("VLM indexing failed for item %s", item_id)
                        STATUS.note(f"Item {item_id}: error — {str(exc)[:120]}")
                        await session.rollback()
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            log.warning(
                                "%d consecutive failures — stopping this run",
                                consecutive_failures,
                            )
                            STATUS.note(
                                f"{consecutive_failures} consecutive failures "
                                f"— run stopped"
                            )
                            break
                    finally:
                        STATUS.processed = i + 1
                        STATUS.indexed = indexed
                    if delay and i < total - 1:
                        await asyncio.sleep(delay)

        log.info("VLM indexer: %d/%d item(s) indexed", indexed, total)
        STATUS.note(f"Done: {indexed}/{total} item(s) indexed.")
        return indexed
    finally:
        STATUS.running = False
