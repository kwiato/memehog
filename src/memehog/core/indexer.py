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
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import json_repair
from sqlalchemy import case
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Item, ItemTag, Tag, VlmError, VlmProfile, VlmText, utcnow
from ..search.base import SearchBackend
from .appsettings import effective_settings, ensure_profile_from_env
from .library import SPICY_TAG
from .media import make_thumbnail

log = logging.getLogger(__name__)

PROMPT = """\
You are indexing a meme library for full-text search.
Return ONLY a JSON object with exactly these keys:
{{"ocr_text": "...", "description": "...", "tags": ["..."], "lang": "..."}}
- ocr_text: all text visible in the image, transcribed verbatim in its original
  language ("" if there is none). For long walls of text, transcribe up to
  roughly the first 200 words and stop.
- description: 1-3 sentences in {language} describing what the image shows and,
  if recognizable, the meme template or character names. Use words people would
  type when searching for this meme.
- tags: a JSON array of 0-{max_tags} short lowercase tags in {language} for
  the topic, meme template or vibe (an array of strings, never one comma-
  separated string).{tags_hint}
- lang: ISO 639-1 code of the dominant language of the text ON the image,
  e.g. "pl" or "en" ("" when there is no text).
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


def _parse_response(content: str) -> tuple[str, str, list[str], str]:
    # Reasoning models (qwen3.6 on groq, deepseek-r1...) prepend a
    # <think>...</think> monologue that may itself contain {braces} — drop it
    # so the JSON matcher only sees the actual answer. An unclosed <think>
    # means the model burned its whole token budget thinking; no JSON follows
    # and the error below reports it.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    match = _JSON_RE.search(content)
    if not match:
        raise ValueError(f"no JSON object in VLM response: {content[:200]!r}")
    # strict=False: some models (pixtral among them) put literal newlines
    # inside JSON strings when transcribing multi-line memes.
    try:
        data = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError:
        # Smaller models produce almost-JSON in creative ways (trailing
        # commas, stray quotes...) — best-effort repair before giving up.
        data = json_repair.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError(f"VLM response is not a JSON object: {content[:200]!r}")
    ocr = str(data.get("ocr_text") or "").strip()
    description = str(data.get("description") or "").strip()
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, str):
        # Some models return "kot, mem, humor" instead of a JSON array.
        raw_tags = [part for part in re.split(r"[,;#]+", raw_tags) if part.strip()]
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
    lang = str(data.get("lang") or "").strip().lower()
    if not (2 <= len(lang) <= 8 and lang.isalpha()):
        lang = ""
    return ocr, description, tags, lang


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
) -> tuple[str, str, list[str], str]:
    """One chat-completions call; returns (ocr_text, description, tags, lang)."""
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
        # truncates the JSON mid-string — and reasoning models spend most of
        # the budget on <think> monologue before emitting any JSON. Output
        # tokens are billed as used, so the high ceiling costs nothing on
        # ordinary memes.
        "max_tokens": 4096,
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


async def _build_tags_hint(session: AsyncSession, settings: Settings) -> str:
    """Existing tags (most-used first) as the model's preferred vocabulary,
    so auto-tagging converges instead of sprawling."""
    if not settings.vlm_auto_tag:
        return ""
    rows = await session.execute(
        select(Tag.name)
        .join(ItemTag, ItemTag.tag_id == Tag.id)
        .where(Tag.name != SPICY_TAG)
        .group_by(Tag.id)
        .order_by(func.count(ItemTag.item_id).desc())
        .limit(TAGS_HINT_LIMIT)
    )
    return ", ".join(name for (name,) in rows)


async def _record_error(
    session: AsyncSession,
    profile_id: int,
    item_id: int | None,
    kind: str,
    message: str,
) -> None:
    """Persist a failed attempt for the per-model health badge. Called after
    session.rollback(), so it commits its own tiny transaction."""
    session.add(
        VlmError(
            profile_id=profile_id,
            item_id=item_id,
            kind=kind,
            message=message[:300],
        )
    )
    await session.commit()


def _profile_snapshot(profile: VlmProfile) -> SimpleNamespace:
    """Plain-values copy — survives session.rollback(), unlike ORM objects."""
    return SimpleNamespace(
        id=profile.id,
        name=profile.name,
        model=profile.model,
        base_url=profile.base_url,
        api_key=profile.api_key,
    )


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
    ocr, description, tags, lang = await describe_image(
        client, trial, jpeg, tags_hint
    )
    fts_text = "\n".join(part for part in (ocr, description) if part)
    # First model to detect a language wins — avoids flip-flopping when
    # models disagree.
    if lang and not item.lang:
        item.lang = lang
    session.add(
        VlmText(
            item_id=item.id,
            profile_id=profile.id,
            text=fts_text,
            ocr_text=ocr,
            description=description,
        )
    )
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
                snap = _profile_snapshot(orm_profile)
                done = select(VlmText.item_id).where(
                    VlmText.profile_id == snap.id
                )
                # Memes NO model has touched yet jump the queue — they're
                # invisible to search until someone describes them. Items that
                # only miss this particular model fill in afterwards, once the
                # fresh backlog is clear. Newest first within each bucket.
                coverage = (
                    select(func.count(VlmText.id))
                    .where(VlmText.item_id == Item.id)
                    .correlate(Item)
                    .scalar_subquery()
                )
                stmt = (
                    select(Item.id)
                    .where(Item.index_status != "failed", Item.id.not_in(done))
                    .order_by(case((coverage == 0, 0), else_=1), Item.id.desc())
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

            tags_hint = await _build_tags_hint(session, settings)

            # Old health-log entries have served their purpose.
            await session.execute(
                sa_delete(VlmError).where(
                    VlmError.created_at < utcnow() - timedelta(days=7)
                )
            )
            await session.commit()

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
                                    await _record_error(
                                        session, profile.id, item_id,
                                        "connection", f"{label}: {detail}",
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
                            await _record_error(
                                session, profile.id, item_id, "response",
                                f"{type(exc).__name__}: {str(exc)[:250]}",
                            )
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


async def requeue_items(session: AsyncSession, item_ids: list[int]) -> int:
    """Drop the stored model outputs for these items so every active model
    re-processes them on its next run (nightly/interval/Run now). The FTS
    copies stay until replaced, so search keeps working in the meantime."""
    if not item_ids:
        return 0
    await session.execute(
        sa_delete(VlmText).where(VlmText.item_id.in_(item_ids))
    )
    from sqlalchemy import update

    await session.execute(
        update(Item)
        .where(Item.id.in_(item_ids), Item.index_status == "failed")
        .values(index_status="pending")
    )
    await session.commit()
    return len(item_ids)


async def reindex_item(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    search: SearchBackend,
    item_id: int,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[tuple[str, str]]:
    """Manually run every active model over ONE item, replacing its previous
    output. Returns (profile_name, outcome) per model, where outcome is "ok"
    or a short error description. Used by the Info modal's re-run button."""
    if STATUS.running:
        return [("indexer", "a full indexing run is in progress — try again "
                            "in a moment")]
    results: list[tuple[str, str]] = []
    async with session_factory() as session:
        settings = await effective_settings(session, settings)
        profiles = list(
            await session.scalars(
                select(VlmProfile)
                .where(VlmProfile.active.is_(True))
                .order_by(VlmProfile.id)
            )
        )
        if not profiles:
            return [("indexer", "no active models — enable one in the "
                                "AI models tab")]
        snapshots = [_profile_snapshot(p) for p in profiles]
        item = await session.get(Item, item_id)
        if item is None:
            return []
        if item.filename.startswith(f"{SPICY_TAG}/") and not settings.vlm_index_spicy:
            return [("indexer", "spicy meme — enable 'Index spicy memes' "
                                "in Settings first")]
        tags_hint = await _build_tags_hint(session, settings)

        async with httpx.AsyncClient(timeout=120, transport=transport) as client:
            for snap in snapshots:
                trial = settings.model_copy(
                    update={
                        "vlm_base_url": snap.base_url,
                        "vlm_api_key": snap.api_key,
                        "vlm_model": snap.model,
                    }
                )
                try:
                    # Replace the previous output instead of stacking rows.
                    await session.execute(
                        sa_delete(VlmText).where(
                            VlmText.item_id == item_id,
                            VlmText.profile_id == snap.id,
                        )
                    )
                    ok = await _index_one(
                        session, trial, search, client, item, snap, tags_hint
                    )
                    results.append((snap.name, "ok" if ok else "no readable media"))
                except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                    await session.rollback()
                    if isinstance(exc, httpx.HTTPStatusError):
                        detail = (
                            f"HTTP {exc.response.status_code}: "
                            f"{exc.response.text[:100]}"
                        )
                    else:
                        detail = f"{type(exc).__name__}: {str(exc)[:100]}"
                    await _record_error(
                        session, snap.id, item_id, "connection", detail
                    )
                    results.append((snap.name, detail))
                except Exception as exc:  # noqa: BLE001 - report, keep going
                    log.exception("Manual reindex failed for item %s (%s)",
                                  item_id, snap.name)
                    await session.rollback()
                    detail = f"{type(exc).__name__}: {str(exc)[:100]}"
                    await _record_error(
                        session, snap.id, item_id, "response", detail
                    )
                    results.append((snap.name, detail))
                # A rollback expires the ORM item — reload for the next model.
                item = await session.get(Item, item_id)
                if item is None:
                    break
    return results
