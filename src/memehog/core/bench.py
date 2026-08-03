"""VLM model benchmark.

Runs every configured vision model over the same random sample of memes and
stores the outputs side by side (`vlm_samples` table), so you can judge which
free API reads and describes your library best before committing to one.
Nothing here touches the search index — it's a pure experiment.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Item, VlmProfile, VlmSample
from .appsettings import effective_settings
from .indexer import IndexerStatus, _load_thumb_jpeg, describe_image

log = logging.getLogger(__name__)

BENCH_STATUS = IndexerStatus()
MAX_SAMPLE = 25


async def run_benchmark(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    sample_size: int = 10,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Run all configured models over one random sample; returns rows stored.

    Results replace the previous benchmark's rows.
    """
    if BENCH_STATUS.running:
        log.info("Benchmark already running — skipping this trigger")
        return 0
    BENCH_STATUS.start()

    done = 0
    try:
        async with session_factory() as session:
            settings = await effective_settings(session, settings)
            profiles = (
                await session.scalars(select(VlmProfile).order_by(VlmProfile.id))
            ).all()
            configs = [
                {
                    "label": p.name,
                    "base_url": p.base_url,
                    "api_key": p.api_key,
                    "model": p.model,
                }
                for p in profiles
            ]
            if not configs:
                BENCH_STATUS.note(
                    "No saved models — add some in the AI models tab first."
                )
                return 0

            sample_size = max(1, min(MAX_SAMPLE, sample_size))
            stmt = (
                select(Item.id)
                .where(Item.thumb_filename.is_not(None))
                .order_by(func.random())
                .limit(sample_size)
            )
            if not settings.vlm_index_spicy:
                stmt = stmt.where(Item.filename.not_like("spicy/%"))
            item_ids = list(await session.scalars(stmt))
            if not item_ids:
                BENCH_STATUS.note("No memes with thumbnails to sample.")
                return 0

            await session.execute(delete(VlmSample))
            await session.commit()

            BENCH_STATUS.total = len(item_ids) * len(configs)
            BENCH_STATUS.note(
                f"Benchmark: {len(configs)} model(s) × {len(item_ids)} meme(s)"
            )
            delay = 60.0 / settings.vlm_rpm if settings.vlm_rpm > 0 else 0.0

            async with httpx.AsyncClient(timeout=120, transport=transport) as client:
                for cfg in configs:
                    trial = settings.model_copy(
                        update={
                            "vlm_base_url": cfg["base_url"],
                            "vlm_api_key": cfg["api_key"],
                            "vlm_model": cfg["model"],
                        }
                    )
                    BENCH_STATUS.note(f"—— {cfg['label']} ——")
                    for item_id in item_ids:
                        item = await session.get(Item, item_id)
                        if item is None:
                            continue
                        jpeg = await asyncio.to_thread(
                            _load_thumb_jpeg, settings, item
                        )
                        started = time.perf_counter()
                        ocr = description = ""
                        error: str | None = None
                        if jpeg is None:
                            error = "no readable media"
                        else:
                            try:
                                ocr, description, _tags, _lang = (
                                    await describe_image(client, trial, jpeg)
                                )
                            except httpx.HTTPStatusError as exc:
                                error = (
                                    f"HTTP {exc.response.status_code}: "
                                    f"{exc.response.text[:200]}"
                                )
                            except Exception as exc:  # noqa: BLE001
                                error = str(exc)[:200]
                        latency_ms = int((time.perf_counter() - started) * 1000)
                        session.add(
                            VlmSample(
                                item_id=item_id,
                                model_label=cfg["label"],
                                ocr_text=ocr,
                                description=description,
                                latency_ms=latency_ms,
                                error=error,
                            )
                        )
                        await session.commit()
                        done += 1
                        BENCH_STATUS.processed = done
                        if error:
                            BENCH_STATUS.note(
                                f"{cfg['label']} / item {item_id}: "
                                f"ERROR {error[:80]}"
                            )
                        else:
                            BENCH_STATUS.indexed += 1
                            BENCH_STATUS.note(
                                f"{cfg['label']} / item {item_id}: {latency_ms} ms "
                                f"— „{description[:60]}”"
                            )
                        if delay:
                            await asyncio.sleep(delay)

        BENCH_STATUS.note("Benchmark finished — open 📊 Results to compare.")
        return done
    finally:
        BENCH_STATUS.running = False
