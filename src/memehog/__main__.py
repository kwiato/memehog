from __future__ import annotations

import asyncio
import logging

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select

from .config import Settings
from .core import appsettings
from .core.appsettings import NIGHTLY_JOB_ID, SCAN_CRON_KEY, get_setting
from .core.convert import run_conversions
from .core.indexer import run_indexing
from .core.queue import DownloadQueue
from .db import create_engine, init_db
from .db.models import Item
from .search import FtsSearch
from .web import create_app

log = logging.getLogger("memehog")


async def nightly_maintenance(session_factory, settings, search) -> None:
    """Nightly batch: transcode webp/webm, then VLM-index new items."""
    converted = await run_conversions(session_factory, settings, search)
    indexed = await run_indexing(session_factory, settings, search)
    async with session_factory() as session:
        pending = await session.scalar(
            select(func.count(Item.id)).where(Item.index_status == "pending")
        )
        effective = await appsettings.effective_settings(session, settings)
    log.info(
        "Nightly maintenance done: %d file(s) converted, %d item(s) indexed, "
        "%s still pending%s",
        converted, indexed, pending,
        "" if effective.vlm_enabled else " (VLM indexer not configured)",
    )


async def run() -> None:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings.ensure_dirs()

    engine = create_engine(settings.db_path)
    session_factory = await init_db(engine)
    search = FtsSearch()
    queue = DownloadQueue(session_factory, settings, search)
    await queue.restore_pending()

    # The web UI can override the cron/interval from .env (app_settings rows).
    async with session_factory() as session:
        scan_cron = await get_setting(session, SCAN_CRON_KEY, settings.scan_cron)
        effective = await appsettings.effective_settings(session, settings)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        nightly_maintenance,
        CronTrigger.from_crontab(scan_cron),
        id=NIGHTLY_JOB_ID,
        args=[session_factory, settings, search],
    )
    if effective.vlm_interval_minutes > 0:
        # Extra indexer-only runs between the nightly ones — free-tier quotas
        # reset at odd hours, so quick retries beat one 3 AM attempt.
        scheduler.add_job(
            run_indexing,
            IntervalTrigger(minutes=effective.vlm_interval_minutes),
            id=appsettings.VLM_INTERVAL_JOB_ID,
            args=[session_factory, settings, search],
        )
    scheduler.start()
    log.info(
        "Nightly maintenance scheduled: %s; indexer interval: %s",
        scan_cron,
        f"{effective.vlm_interval_minutes} min"
        if effective.vlm_interval_minutes > 0 else "nightly only",
    )

    app = create_app(settings, session_factory, search, queue, scheduler=scheduler)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
    )

    tasks = [
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(queue.run(), name="download-worker"),
    ]
    if settings.bot_token:
        from .bot import run_bot

        tasks.append(
            asyncio.create_task(
                run_bot(settings, session_factory, search, queue), name="bot"
            )
        )
    else:
        log.warning("BOT_TOKEN not set — running without the Telegram bot")

    log.info("Memehog is up: http://%s:%s", settings.host, settings.port)
    try:
        # If any long-running task dies (or the web server is stopped with
        # Ctrl+C), tear everything down.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception():
                log.error("Task %s crashed", task.get_name(), exc_info=task.exception())
    finally:
        scheduler.shutdown(wait=False)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


def cli() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
