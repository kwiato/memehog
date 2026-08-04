"""Swipe inbox for crawled meme candidates + crawler settings endpoints."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core import appsettings
from ..core.crawler import (
    CRAWLER_JOB_ID,
    USER_AGENT,
    crawl_once,
    download_media,
)
from ..core.library import ingest_file
from ..core.phash import phash_file
from ..db.models import Candidate, Item, RejectedHash
from .deps import get_search, get_session, get_settings

log = logging.getLogger(__name__)
router = APIRouter()
# Tests inject a MockTransport here; None = real network.
TRANSPORT: httpx.AsyncBaseTransport | None = None
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _today() -> str:
    return f"{datetime.now():%Y-%m-%d}"


async def _inbox_ctx(session: AsyncSession) -> dict:
    today = _today()
    pending = list(
        await session.scalars(
            select(Candidate)
            .where(Candidate.day == today, Candidate.status == "pending")
            .order_by(Candidate.id)
        )
    )
    done = await session.scalar(
        select(func.count(Candidate.id)).where(
            Candidate.day == today, Candidate.status != "pending"
        )
    ) or 0
    return {
        "candidates": pending,
        "done": done,
        "total": done + len(pending),
    }


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(
    request: Request, session: AsyncSession = Depends(get_session)
):
    return templates.TemplateResponse(
        request, "inbox.html", await _inbox_ctx(session)
    )


@router.get("/candidates/{filename}")
async def candidate_thumb(
    filename: str, settings: Settings = Depends(get_settings)
):
    path = (settings.candidates_dir / filename).resolve()
    if (
        settings.candidates_dir.resolve() not in path.parents
        or not path.is_file()
    ):
        raise HTTPException(404)
    return FileResponse(path)


@router.post("/ui/inbox/{cand_id}/accept")
async def inbox_accept(
    cand_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search=Depends(get_search),
):
    cand = await session.get(Candidate, cand_id)
    if cand is None or cand.status != "pending":
        raise HTTPException(404)

    async with httpx.AsyncClient(
        transport=TRANSPORT, headers={"User-Agent": USER_AGENT},
        timeout=60, follow_redirects=True,
    ) as client:
        data = await download_media(client, cand.media_url)
    if data is None:
        cand.status = "rejected"
        await session.commit()
        return JSONResponse(
            {"ok": False, "error": "download failed — skipped"}, status_code=502
        )

    suffix = Path(cand.media_url.split("?")[0]).suffix or ".jpg"
    tmp = settings.tmp_dir / f"crawl-{cand.id}{suffix}"
    tmp.write_bytes(data)
    item, created = await ingest_file(
        session, settings, search, tmp,
        source_url=cand.page_url or cand.media_url,
        origin="crawler",
        caption=cand.title or None,
    )
    cand.status = "accepted"
    cand.item_id = item.id
    await session.commit()
    return {"ok": True, "item_id": item.id, "duplicate": not created}


@router.post("/ui/inbox/{cand_id}/reject")
async def inbox_reject(
    cand_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    cand = await session.get(Candidate, cand_id)
    if cand is None or cand.status != "pending":
        raise HTTPException(404)
    cand.status = "rejected"
    if cand.phash:
        session.add(RejectedHash(phash=cand.phash))
    if cand.thumb_filename:
        (settings.candidates_dir / cand.thumb_filename).unlink(missing_ok=True)
        cand.thumb_filename = None
    await session.commit()
    return {"ok": True}


# --- crawler settings (General tab) ------------------------------------------


async def _crawler_ctx(session: AsyncSession, settings: Settings) -> dict:
    effective = await appsettings.effective_settings(session, settings)
    pending = await session.scalar(
        select(func.count(Candidate.id)).where(
            Candidate.day == _today(), Candidate.status == "pending"
        )
    ) or 0
    return {"crawler": effective, "inbox_pending": pending}


@router.post("/ui/settings/crawler", response_class=HTMLResponse)
async def set_crawler(
    request: Request,
    sources: str = Form(""),
    daily_target: int = Form(120),
    hour: int = Form(6),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    hour = min(max(hour, 0), 23)
    await appsettings.set_setting(session, "crawler_sources", sources)
    await appsettings.set_setting(
        session, "crawler_daily_target", str(max(daily_target, 1))
    )
    await appsettings.set_setting(session, "crawler_hour", str(hour))

    scheduler = request.app.state.scheduler
    if scheduler is not None:
        from apscheduler.triggers.cron import CronTrigger

        job = scheduler.get_job(CRAWLER_JOB_ID)
        if job is not None:
            job.reschedule(CronTrigger(hour=hour, minute=0))
    return templates.TemplateResponse(
        request,
        "partials/settings_crawler.html",
        await _crawler_ctx(session, settings),
    )


@router.post("/ui/crawler/run", response_class=HTMLResponse)
async def run_crawler_now(request: Request):
    factory = request.app.state.session_factory
    settings = request.app.state.settings

    async def _run() -> None:
        try:
            await crawl_once(factory, settings)
        except Exception:
            log.exception("manual crawl failed")

    asyncio.create_task(_run())
    return HTMLResponse(
        '<span class="vlm-test ok"><i class="bi bi-check-circle"></i>'
        " Crawl started — fresh memes land in the"
        ' <a href="/inbox">inbox</a> shortly.</span>'
    )
