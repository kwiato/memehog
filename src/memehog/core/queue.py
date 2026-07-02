from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Item, Job
from ..search.base import SearchBackend
from .downloader import download_url
from .library import ingest_file

log = logging.getLogger(__name__)

# Called when a job finishes (any status): (job, ingested items)
JobCallback = Callable[[Job, list[Item]], Awaitable[None]]


class DownloadQueue:
    """Durable download queue: jobs live in SQLite, one async worker processes
    them sequentially (friendly to both the Pi and rate-limiting sites)."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        search: SearchBackend,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._search = search
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        # In-memory only: callbacks don't survive a restart (jobs do).
        self._callbacks: dict[int, JobCallback] = {}

    async def submit(
        self,
        url: str,
        *,
        origin: str,
        requested_by: str | None = None,
        spicy: bool = False,
        callback: JobCallback | None = None,
    ) -> Job:
        async with self._session_factory() as session:
            job = Job(url=url, origin=origin, requested_by=requested_by, spicy=spicy)
            session.add(job)
            await session.commit()
        if callback is not None:
            self._callbacks[job.id] = callback
        await self._queue.put(job.id)
        log.info("Queued job %s: %s", job.id, url)
        return job

    async def restore_pending(self) -> None:
        """Re-enqueue jobs interrupted by a restart."""
        async with self._session_factory() as session:
            await session.execute(
                update(Job).where(Job.status == "running").values(status="pending")
            )
            await session.commit()
            ids = (
                await session.scalars(
                    select(Job.id).where(Job.status == "pending").order_by(Job.id)
                )
            ).all()
        for job_id in ids:
            await self._queue.put(job_id)
        if ids:
            log.info("Restored %d pending job(s)", len(ids))

    async def run(self) -> None:
        log.info("Download worker started")
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(job_id)
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("Unexpected error processing job %s", job_id)
            finally:
                self._queue.task_done()

    async def _process(self, job_id: int) -> None:
        async with self._session_factory() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status not in ("pending", "running"):
                return
            job.status = "running"
            await session.commit()

            tmp_dir = self._settings.tmp_dir / f"job-{job_id}"
            items: list[Item] = []
            created_any = False
            try:
                files = await download_url(
                    job.url, tmp_dir, self._settings.cookies_path
                )
                for dl in files:
                    item, created = await ingest_file(
                        session,
                        self._settings,
                        self._search,
                        dl.path,
                        source_url=dl.source_url,
                        origin=job.origin,
                        caption=dl.caption,
                        uploader=dl.uploader,
                        spicy=job.spicy,
                    )
                    items.append(item)
                    created_any = created_any or created
                job.status = "done" if created_any else "duplicate"
                job.item_id = items[0].id if items else None
            except Exception as exc:  # noqa: BLE001 - the job must always end in a final state
                job.status = "error"
                job.error = str(exc)[:2000]
                log.warning("Job %s failed: %s", job_id, exc)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                await session.commit()

        callback = self._callbacks.pop(job_id, None)
        if callback is not None:
            try:
                await callback(job, items)
            except Exception:  # noqa: BLE001
                log.exception("Job %s callback failed", job_id)
