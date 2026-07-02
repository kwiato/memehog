from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import Settings
from ..core import appsettings
from ..core import clients as clients_svc
from ..core import items as items_svc
from ..core.library import ingest_file
from ..core.queue import DownloadQueue
from ..search.base import SearchBackend
from .deps import get_queue, get_search, get_session, get_settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session)):
    tags = await items_svc.all_tags(session)
    count = await items_svc.count_items(session)
    return templates.TemplateResponse(
        request, "index.html", {"tags": tags, "count": count}
    )


async def _settings_modal(
    request: Request, session: AsyncSession, settings: Settings
):
    clients = await clients_svc.list_clients(session)
    cron = await appsettings.get_setting(
        session, appsettings.SCAN_CRON_KEY, settings.scan_cron
    )
    return templates.TemplateResponse(
        request,
        "partials/settings_modal.html",
        {
            "clients": clients,
            "owners": sorted(settings.allowed_ids),
            "scan_hour": _cron_hour(cron),
        },
    )


def _cron_hour(cron: str) -> int:
    parts = cron.split()
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return 3


@router.get("/ui/settings", response_class=HTMLResponse)
async def settings_modal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    return await _settings_modal(request, session, settings)


@router.post("/ui/settings/scan-hour", response_class=HTMLResponse)
async def set_scan_hour(
    request: Request,
    hour: int = Form(...),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    hour = max(0, min(23, hour))
    cron = f"0 {hour} * * *"
    await appsettings.set_setting(session, appsettings.SCAN_CRON_KEY, cron)
    scheduler = request.app.state.scheduler
    if scheduler is not None:
        from apscheduler.triggers.cron import CronTrigger

        scheduler.reschedule_job(
            appsettings.NIGHTLY_JOB_ID, trigger=CronTrigger.from_crontab(cron)
        )
    return await _settings_modal(request, session, settings)


@router.get("/ui/about", response_class=HTMLResponse)
async def about_modal(
    request: Request, session: AsyncSession = Depends(get_session)
):
    count = await items_svc.count_items(session)
    return templates.TemplateResponse(
        request, "partials/about_modal.html", {"version": __version__, "count": count}
    )


@router.get("/ui/items", response_class=HTMLResponse)
async def grid(
    request: Request,
    q: str = "",
    tag: str = "",
    type: str = "",
    spicy: str = "0",
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    items = await items_svc.list_items(
        session, search, q=q, tag=tag, media_type=type,
        spicy=spicy == "1", page=page,
    )
    has_more = len(items) == items_svc.PAGE_SIZE
    return templates.TemplateResponse(
        request,
        "partials/grid.html",
        {
            "items": items,
            "page": page,
            "has_more": has_more,
            "q": q,
            "tag": tag,
            "type": type,
            "spicy": spicy,
        },
    )


@router.get("/ui/items/{item_id}/detail", response_class=HTMLResponse)
async def detail(
    request: Request,
    item_id: int,
    session: AsyncSession = Depends(get_session),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return templates.TemplateResponse(request, "partials/detail.html", {"item": item})


@router.post("/ui/upload")
async def upload(
    request: Request,
    files: list[UploadFile] | None = None,
    url: str = Form(""),
    caption: str = Form(""),
    spicy: str = Form("0"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
    queue: DownloadQueue = Depends(get_queue),
):
    is_spicy = spicy == "1"
    for file in files or []:
        if not file.filename:
            continue
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.tmp_dir / f"web-{uuid.uuid4().hex[:8]}-{file.filename}"
        with tmp_path.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        await ingest_file(
            session, settings, search, tmp_path,
            origin="web", caption=caption or None, uploader="web",
            spicy=is_spicy,
        )
    if url.strip():
        await queue.submit(
            url.strip(), origin="web", requested_by="web", spicy=is_spicy
        )
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return Response(status_code=204)
    return RedirectResponse("/", status_code=303)


@router.post("/ui/items/{item_id}/delete")
async def delete(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
):
    item = await items_svc.get_item(session, item_id)
    if item is not None:
        await items_svc.delete_item(session, settings, search, item)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/ui/items/{item_id}/spicy", response_class=HTMLResponse)
async def toggle_spicy(
    request: Request,
    item_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    item = await items_svc.toggle_spicy(session, settings, search, item)
    return templates.TemplateResponse(request, "partials/detail.html", {"item": item})


@router.post("/ui/items/{item_id}/tags", response_class=HTMLResponse)
async def add_tag(
    request: Request,
    item_id: int,
    name: str = Form(...),
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    item = await items_svc.add_tag(session, search, item, name)
    return templates.TemplateResponse(request, "partials/detail.html", {"item": item})


@router.post("/ui/items/{item_id}/tags/{name}/delete", response_class=HTMLResponse)
async def remove_tag(
    request: Request,
    item_id: int,
    name: str,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    item = await items_svc.remove_tag(session, search, item, name)
    return templates.TemplateResponse(request, "partials/detail.html", {"item": item})


# --- Telegram clients management (settings page) -----------------------------


async def _clients_partial(request: Request, session: AsyncSession, settings: Settings):
    clients = await clients_svc.list_clients(session)
    return templates.TemplateResponse(
        request,
        "partials/clients.html",
        {"clients": clients, "owners": sorted(settings.allowed_ids)},
    )


@router.post("/ui/clients", response_class=HTMLResponse)
async def add_client(
    request: Request,
    telegram_id: int = Form(...),
    note: str = Form(""),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await clients_svc.add_client(
        session, telegram_id, note=note or None, status="approved"
    )
    return await _clients_partial(request, session, settings)


@router.post("/ui/clients/{telegram_id}/approve", response_class=HTMLResponse)
async def approve_client(
    request: Request,
    telegram_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await clients_svc.approve_client(session, telegram_id)
    return await _clients_partial(request, session, settings)


@router.post("/ui/clients/{telegram_id}/delete", response_class=HTMLResponse)
async def delete_client(
    request: Request,
    telegram_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await clients_svc.remove_client(session, telegram_id)
    return await _clients_partial(request, session, settings)
