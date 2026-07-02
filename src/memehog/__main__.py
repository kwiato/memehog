from __future__ import annotations

import asyncio
import logging

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from .config import Settings
from .core.queue import DownloadQueue
from .db import create_engine, init_db
from .db.models import Item
from .search import FtsSearch
from .web import create_app

log = logging.getLogger("memehog")


async def nightly_index(session_factory) -> None:
    """Placeholder for the future OCR / embedding indexer.

    Items are ingested with index_status='pending'; this job will pick them
    up, run Tesseract (and later an embedding backend) and update items_fts.
    """
    async with session_factory() as session:
        pending = await session.scalar(
            select(func.count(Item.id)).where(Item.index_status == "pending")
        )
    log.info("Nightly index: %s item(s) awaiting OCR (indexer not implemented yet)", pending)


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

    app = create_app(settings, session_factory, search, queue)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level.lower(),
        )
    )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        nightly_index,
        CronTrigger.from_crontab(settings.scan_cron),
        args=[session_factory],
    )
    scheduler.start()

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
