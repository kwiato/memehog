from __future__ import annotations

import asyncio
import html
import io
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, select
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..config import Settings
from ..core import appsettings
from ..core import bench as bench_svc
from ..core import clients as clients_svc
from ..core import items as items_svc
from ..core.indexer import STATUS as indexer_status
from ..core.indexer import describe_image, reindex_item, requeue_items, run_indexing
from ..core.library import SPICY_TAG
from ..core.library import ingest_file
from ..core.queue import DownloadQueue
from ..db.models import (
    Item,
    ItemTag,
    Tag,
    VlmError,
    VlmProfile,
    VlmSample,
    VlmText,
    utcnow,
)
from ..search.base import SearchBackend
from .deps import get_queue, get_search, get_session, get_settings

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    tag: str = "",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    if getattr(request.state, "public", False):
        from .public import render_public_index

        return await render_public_index(request, session, settings)
    tags = await items_svc.all_tags(session)
    count = await items_svc.count_items(session)
    # Model filter dropdown — only profiles that have indexed anything yet.
    search_profiles = list(
        await session.scalars(
            select(VlmProfile)
            .where(VlmProfile.id.in_(select(VlmText.profile_id).distinct()))
            .order_by(VlmProfile.id)
        )
    )
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tags": tags,
            "count": count,
            "search_profiles": search_profiles,
            "langs": await _present_langs(session),
            "selected_tag": tag,
        },
    )


# (flag country code, native name) per language. SVG flags are vendored —
# emoji flags don't render on Windows. Unmapped codes degrade to 🌐 + code.
LANGUAGES: dict[str, tuple[str | None, str]] = {
    "pl": ("pl", "polski"),
    "en": ("gb", "English"),
    "de": ("de", "Deutsch"),
    "es": ("es", "español"),
    "fr": ("fr", "français"),
    "it": ("it", "italiano"),
    "pt": ("pt", "português"),
    "ru": ("ru", "русский"),
    "uk": ("ua", "українська"),
    "cs": ("cz", "čeština"),
    "sk": ("sk", "slovenčina"),
    "nl": ("nl", "Nederlands"),
    "ja": ("jp", "日本語"),
    "ko": ("kr", "한국어"),
    "zh": ("cn", "中文"),
}


def _lang_meta(code: str) -> dict:
    country, name = LANGUAGES.get(code, (None, code))
    return {
        "code": code,
        "name": name,
        "flag": f"/static/vendor/flags/{country}.svg" if country else None,
    }


async def _present_langs(session: AsyncSession) -> list[dict]:
    codes = await session.scalars(
        select(Item.lang).where(Item.lang.is_not(None), Item.lang != "").distinct()
    )
    return [_lang_meta(code) for code in sorted(codes)]


def _cron_hour(cron: str) -> int:
    parts = cron.split()
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return 3


async def _list_profiles(session: AsyncSession) -> list[VlmProfile]:
    return list(await session.scalars(select(VlmProfile).order_by(VlmProfile.id)))


async def _vlm_general_ctx(session: AsyncSession, settings: Settings) -> dict:
    profiles = await _list_profiles(session)
    return {
        "vlm": await appsettings.effective_settings(session, settings),
        "profiles": profiles,
        "active_profiles": [p for p in profiles if p.active],
        "status": indexer_status,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    tab: str = "general",
    mpage: int = 1,
    mfilter: str = "",
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    await appsettings.ensure_profile_from_env(session, settings)
    cron = await appsettings.get_setting(
        session, appsettings.SCAN_CRON_KEY, settings.scan_cron
    )
    ctx = {
        "tab": tab if tab in ("ai", "tags", "memes") else "general",
        "tag_stats": await items_svc.tag_stats(session),
        "clients": await clients_svc.list_clients(session),
        "owners": sorted(settings.allowed_ids),
        "scan_hour": _cron_hour(cron),
        "health": await _profile_health(session),
        "bench": bench_svc.BENCH_STATUS,
        "version": __version__,
        "build_sha": settings.memehog_build_sha,
        "build_date": settings.memehog_build_date,
        **await _vlm_general_ctx(session, settings),
    }
    if ctx["tab"] == "general":
        from .inbox import _crawler_ctx

        ctx.update(await _crawler_ctx(session, settings))
    if ctx["tab"] == "memes":
        ctx.update(await _memes_ctx(session, mpage, mfilter))
    return templates.TemplateResponse(request, "settings.html", ctx)


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
    return templates.TemplateResponse(
        request, "partials/settings_maintenance.html", {"scan_hour": hour}
    )


@router.post("/ui/settings/vlm", response_class=HTMLResponse)
async def set_vlm(
    request: Request,
    language: str = Form("English"),
    rpm: float = Form(10),
    max_per_run: int = Form(200),
    interval: int = Form(60),
    index_spicy: str = Form("0"),
    auto_tag: str = Form("0"),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    interval = max(interval, 0)
    values = {
        "vlm_language": language.strip() or "English",
        "vlm_rpm": f"{max(rpm, 0.0):g}",
        "vlm_max_per_run": str(max(max_per_run, 1)),
        "vlm_interval_minutes": str(interval),
        "vlm_index_spicy": "1" if index_spicy == "1" else "0",
        "vlm_auto_tag": "1" if auto_tag == "1" else "0",
    }
    for key, value in values.items():
        await appsettings.set_setting(session, key, value)

    # Apply the new interval to the live scheduler right away.
    scheduler = request.app.state.scheduler
    if scheduler is not None:
        from apscheduler.triggers.interval import IntervalTrigger

        job = scheduler.get_job(appsettings.VLM_INTERVAL_JOB_ID)
        if job is not None:
            job.remove()
        if interval > 0:
            scheduler.add_job(
                run_indexing,
                IntervalTrigger(minutes=interval),
                id=appsettings.VLM_INTERVAL_JOB_ID,
                args=[
                    request.app.state.session_factory,
                    request.app.state.settings,
                    request.app.state.search,
                ],
            )
    return templates.TemplateResponse(
        request,
        "partials/settings_vlm_general.html",
        await _vlm_general_ctx(session, settings),
    )


# --- VLM model profiles (AI models tab) --------------------------------------


# A profile goes 🟡 at this many junk responses within the health window.
HEALTH_WARN_RESPONSES = 3
HEALTH_WINDOW_HOURS = 24


async def _profile_health(session: AsyncSession) -> dict[int, dict]:
    """Per-profile error stats: recent (24h) counts by kind drive the badge
    color, the all-time total decides whether the log link shows at all.
    Successful-index counts ride along for contrast in the tooltip."""
    since = utcnow() - timedelta(hours=HEALTH_WINDOW_HOURS)
    health: dict[int, dict] = {}

    def entry(pid: int) -> dict:
        return health.setdefault(
            pid,
            {"connection": 0, "response": 0, "total": 0,
             "ok24": 0, "ok_total": 0},
        )

    recent = await session.execute(
        select(VlmError.profile_id, VlmError.kind, func.count())
        .where(VlmError.created_at >= since)
        .group_by(VlmError.profile_id, VlmError.kind)
    )
    for pid, kind, count in recent:
        entry(pid)[kind] = count
    totals = await session.execute(
        select(VlmError.profile_id, func.count()).group_by(VlmError.profile_id)
    )
    for pid, count in totals:
        entry(pid)["total"] = count
    ok_recent = await session.execute(
        select(VlmText.profile_id, func.count())
        .where(VlmText.created_at >= since)
        .group_by(VlmText.profile_id)
    )
    for pid, count in ok_recent:
        entry(pid)["ok24"] = count
    ok_totals = await session.execute(
        select(VlmText.profile_id, func.count()).group_by(VlmText.profile_id)
    )
    for pid, count in ok_totals:
        entry(pid)["ok_total"] = count
    for stats in health.values():
        if stats["connection"]:
            stats["level"] = "error"
        elif stats["response"] >= HEALTH_WARN_RESPONSES:
            stats["level"] = "warn"
        else:
            stats["level"] = "ok"
    return health


async def _profiles_response(request: Request, session: AsyncSession):
    return templates.TemplateResponse(
        request,
        "partials/vlm_profiles.html",
        {
            "profiles": await _list_profiles(session),
            "health": await _profile_health(session),
        },
    )


@router.get("/ui/vlm/profiles/{profile_id}/errors", response_class=HTMLResponse)
async def vlm_profile_errors(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    errors = list(
        await session.scalars(
            select(VlmError)
            .where(VlmError.profile_id == profile_id)
            .order_by(VlmError.created_at.desc())
            .limit(50)
        )
    )
    return templates.TemplateResponse(
        request,
        "partials/vlm_errors.html",
        {"profile": profile, "errors": errors},
    )


@router.get("/ui/vlm/profiles/new", response_class=HTMLResponse)
async def vlm_profile_modal(request: Request):
    return templates.TemplateResponse(
        request, "partials/vlm_profile_modal.html", {"profile": None}
    )


@router.get("/ui/vlm/profiles/{profile_id}/edit", response_class=HTMLResponse)
async def vlm_profile_edit_modal(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return templates.TemplateResponse(
        request, "partials/vlm_profile_modal.html", {"profile": profile}
    )


@router.post("/ui/vlm/profiles/{profile_id}", response_class=HTMLResponse)
async def vlm_profile_update(
    request: Request,
    profile_id: int,
    name: str = Form(""),
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    profile.name = (name.strip() or model.strip())[:128]
    profile.base_url = base_url.strip()
    profile.model = model.strip()
    profile.api_key = api_key.strip()
    await session.commit()
    return await _profiles_response(request, session)


@router.post("/ui/vlm/profiles", response_class=HTMLResponse)
async def vlm_profile_create(
    request: Request,
    name: str = Form(""),
    base_url: str = Form(...),
    model: str = Form(...),
    api_key: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    profile = VlmProfile(
        name=(name.strip() or model.strip())[:128],
        base_url=base_url.strip(),
        model=model.strip(),
        api_key=api_key.strip(),
        active=True,  # a freshly added model starts indexing right away
    )
    session.add(profile)
    await session.commit()
    return await _profiles_response(request, session)


@router.post("/ui/vlm/profiles/{profile_id}/toggle", response_class=HTMLResponse)
async def vlm_profile_toggle(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    profile.active = not profile.active
    await session.commit()
    return await _profiles_response(request, session)


@router.post("/ui/vlm/profiles/{profile_id}/delete", response_class=HTMLResponse)
async def vlm_profile_delete(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is not None:
        # The FK cascade drops vlm_texts; the FTS copy is ours to clean up.
        await search.remove_profile(session, profile_id)
        await session.delete(profile)
        await session.commit()
    return await _profiles_response(request, session)


@router.post("/ui/vlm/profiles/{profile_id}/test", response_class=HTMLResponse)
async def vlm_profile_test(
    request: Request,
    profile_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    profile = await session.get(VlmProfile, profile_id)
    if profile is None:
        raise HTTPException(404, "Profile not found")
    return await _run_vlm_test(
        request, settings, profile.base_url, profile.api_key, profile.model
    )


def _vlm_test_card() -> bytes:
    """A small JPEG with text on it, so the test exercises OCR too."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (240, 120), "#2b6cb0")
    draw = ImageDraw.Draw(img)
    draw.text((20, 45), "MEMEHOG TEST 123", fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


async def _run_vlm_test(
    request: Request, settings: Settings, base_url: str, api_key: str, model: str
) -> HTMLResponse:
    trial = settings.model_copy(
        update={
            "vlm_base_url": base_url.strip(),
            "vlm_api_key": api_key.strip(),
            "vlm_model": model.strip(),
        }
    )
    if not trial.vlm_enabled:
        return HTMLResponse(
            '<span class="vlm-test error">Fill in the endpoint and model first.</span>'
        )
    try:
        transport = getattr(request.app.state, "vlm_transport", None)
        async with httpx.AsyncClient(timeout=60, transport=transport) as client:
            ocr, description, _tags, _lang = await describe_image(
                client, trial, _vlm_test_card()
            )
        reply = description or ocr or "(empty reply)"
        return HTMLResponse(
            f'<span class="vlm-test ok"><i class="bi bi-check-circle"></i> '
            f'Works! Model replied: &bdquo;{html.escape(reply[:160])}&rdquo;</span>'
        )
    except httpx.HTTPStatusError as exc:
        detail = f"HTTP {exc.response.status_code}: {exc.response.text[:160]}"
        return HTMLResponse(
            f'<span class="vlm-test error"><i class="bi bi-x-circle"></i> '
            f'{html.escape(detail)}</span>'
        )
    except Exception as exc:  # noqa: BLE001 - show whatever went wrong
        return HTMLResponse(
            f'<span class="vlm-test error"><i class="bi bi-x-circle"></i> '
            f'{html.escape(str(exc)[:160])}</span>'
        )


@router.post("/ui/settings/vlm/test", response_class=HTMLResponse)
async def test_vlm(
    request: Request,
    base_url: str = Form(""),
    api_key: str = Form(""),
    model: str = Form(""),
    settings: Settings = Depends(get_settings),
):
    """Used by the add-model dialog to test a config before saving it."""
    return await _run_vlm_test(request, settings, base_url, api_key, model)


def _vlm_status_response(request: Request):
    return templates.TemplateResponse(
        request, "partials/vlm_status.html", {"status": indexer_status}
    )


@router.get("/ui/settings/vlm/status", response_class=HTMLResponse)
async def vlm_status(request: Request):
    return _vlm_status_response(request)


@router.post("/ui/settings/vlm/run", response_class=HTMLResponse)
async def vlm_run(request: Request):
    if not indexer_status.running:
        app = request.app
        task = asyncio.create_task(
            run_indexing(
                app.state.session_factory,
                app.state.settings,
                app.state.search,
                transport=getattr(app.state, "vlm_transport", None),
            )
        )
        # Keep a reference (avoids GC) and swallow any stray exception so the
        # event loop doesn't warn about an unobserved task failure.
        app.state.vlm_task = task
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    return _vlm_status_response(request)


def _bench_status_response(request: Request):
    return templates.TemplateResponse(
        request, "partials/vlm_bench_status.html", {"bench": bench_svc.BENCH_STATUS}
    )


@router.get("/ui/settings/vlm/bench/status", response_class=HTMLResponse)
async def vlm_bench_status(request: Request):
    return _bench_status_response(request)


@router.post("/ui/settings/vlm/bench", response_class=HTMLResponse)
async def vlm_bench_run(
    request: Request,
    sample_size: int = Form(10),
):
    if not bench_svc.BENCH_STATUS.running:
        app = request.app
        task = asyncio.create_task(
            bench_svc.run_benchmark(
                app.state.session_factory,
                app.state.settings,
                sample_size=sample_size,
                transport=getattr(app.state, "vlm_transport", None),
            )
        )
        app.state.vlm_bench_task = task
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    return _bench_status_response(request)


@router.get("/ui/vlm/bench", response_class=HTMLResponse)
async def vlm_bench_results(
    request: Request, session: AsyncSession = Depends(get_session)
):
    samples = (
        await session.scalars(
            select(VlmSample).order_by(VlmSample.item_id, VlmSample.model_label)
        )
    ).all()
    labels = sorted({s.model_label for s in samples})

    summary = {}
    for label in labels:
        rows = [s for s in samples if s.model_label == label]
        ok = [s for s in rows if not s.error]
        summary[label] = {
            "ok": len(ok),
            "total": len(rows),
            "avg_ms": int(sum(s.latency_ms for s in ok) / len(ok)) if ok else 0,
            "avg_desc": int(sum(len(s.description) for s in ok) / len(ok)) if ok else 0,
        }

    by_item: dict[int, dict[str, VlmSample]] = {}
    for s in samples:
        by_item.setdefault(s.item_id, {})[s.model_label] = s
    items = {}
    if by_item:
        items = {
            i.id: i
            for i in await session.scalars(
                select(Item).where(Item.id.in_(by_item.keys()))
            )
        }
    rows = [
        {"item": items.get(item_id), "cells": [cells.get(l) for l in labels]}
        for item_id, cells in by_item.items()
    ]
    return templates.TemplateResponse(
        request,
        "partials/vlm_bench_results.html",
        {"labels": labels, "summary": summary, "rows": rows},
    )


@router.get("/ui/about", response_class=HTMLResponse)
async def about_modal(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    count = await items_svc.count_items(session)
    return templates.TemplateResponse(
        request,
        "partials/about_modal.html",
        {
            "version": __version__,
            "count": count,
            "build_sha": settings.memehog_build_sha,
            "build_date": settings.memehog_build_date,
        },
    )


@router.get("/ui/items", response_class=HTMLResponse)
async def grid(
    request: Request,
    q: str = "",
    tag: str = "",
    type: str = "",
    spicy: str = "0",
    model: str = "",
    lang: str = "",
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    items = await items_svc.list_items(
        session, search, q=q, tag=tag, media_type=type,
        spicy=spicy == "1", lang=lang, page=page,
        model_profile_id=int(model) if model.isdigit() else None,
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
            "model": model,
            "lang": lang,
        },
    )


async def _detail_response(request: Request, session: AsyncSession, item: Item):
    return templates.TemplateResponse(
        request,
        "partials/detail.html",
        {"item": item, "ai_tags": await items_svc.ai_tag_names(session, item.id)},
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
    return await _detail_response(request, session, item)


async def _item_info_response(
    request: Request,
    session: AsyncSession,
    item_id: int,
    notices: list[tuple[str, str]] | None = None,
):
    """Everything we know about one meme — per-model descriptions and OCR,
    tags with their origin, benchmark samples, file metadata."""
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    model_texts = (
        await session.execute(
            select(VlmText, VlmProfile)
            .join(VlmProfile, VlmProfile.id == VlmText.profile_id, isouter=True)
            .where(VlmText.item_id == item_id)
            .order_by(VlmText.profile_id)
        )
    ).all()
    samples = list(
        await session.scalars(
            select(VlmSample)
            .where(VlmSample.item_id == item_id)
            .order_by(VlmSample.model_label)
        )
    )
    tag_rows = (
        await session.execute(
            select(Tag.name, ItemTag.source)
            .join(ItemTag, ItemTag.tag_id == Tag.id)
            .where(ItemTag.item_id == item_id)
            .order_by(Tag.name)
        )
    ).all()
    all_langs = [_lang_meta(code) for code in LANGUAGES]
    if item.lang and item.lang not in LANGUAGES:
        all_langs.append(_lang_meta(item.lang))
    return templates.TemplateResponse(
        request,
        "partials/item_info.html",
        {
            "item": item,
            "model_texts": model_texts,
            "samples": samples,
            "tag_rows": tag_rows,
            "notices": notices or [],
            "all_langs": all_langs,
        },
    )


@router.get("/ui/items/{item_id}/info", response_class=HTMLResponse)
async def item_info(
    request: Request,
    item_id: int,
    session: AsyncSession = Depends(get_session),
):
    return await _item_info_response(request, session, item_id)


@router.post("/ui/items/{item_id}/lang", response_class=HTMLResponse)
async def item_set_lang(
    request: Request,
    item_id: int,
    lang: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    lang = lang.strip().lower()
    item.lang = lang if (2 <= len(lang) <= 8 and lang.isalpha()) else None
    await session.commit()
    return await _item_info_response(request, session, item_id)


@router.post("/ui/items/{item_id}/reindex", response_class=HTMLResponse)
async def item_reindex(
    request: Request,
    item_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Re-run every active model over this one meme, then show the refreshed
    Info modal with a per-model outcome."""
    app = request.app
    notices = await reindex_item(
        app.state.session_factory,
        app.state.settings,
        app.state.search,
        item_id,
        transport=getattr(app.state, "vlm_transport", None),
    )
    session.expire_all()  # reindex ran in its own session — drop stale state
    return await _item_info_response(request, session, item_id, notices=notices)


@router.get("/ui/items/{item_id}/crop", response_class=HTMLResponse)
async def crop_modal(
    request: Request,
    item_id: int,
    session: AsyncSession = Depends(get_session),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    if item.media_type != "image":
        raise HTTPException(400, "Only still images can be cropped")
    return templates.TemplateResponse(
        request, "partials/crop_modal.html", {"item": item}
    )


@router.post("/ui/items/{item_id}/crop")
async def crop_apply(
    item_id: int,
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
):
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    if item.media_type != "image":
        raise HTTPException(400, "Only still images can be cropped")
    suffix = ".png" if (file.content_type or "") == "image/png" else ".jpg"
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.tmp_dir / f"crop-{uuid.uuid4().hex[:8]}{suffix}"
    with tmp_path.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)
    ok, message = await items_svc.replace_item_file(
        session, settings, search, item, tmp_path
    )
    if not ok:
        return JSONResponse({"ok": False, "error": message}, status_code=409)
    return JSONResponse({"ok": True})


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
    added = 0
    duplicates = 0
    for file in files or []:
        if not file.filename:
            continue
        settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = settings.tmp_dir / f"web-{uuid.uuid4().hex[:8]}-{file.filename}"
        with tmp_path.open("wb") as fh:
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
        _, created = await ingest_file(
            session, settings, search, tmp_path,
            origin="web", caption=caption or None, uploader="web",
            spicy=is_spicy,
        )
        if created:
            added += 1
        else:
            duplicates += 1
    queued = 0
    if url.strip():
        await queue.submit(
            url.strip(), origin="web", requested_by="web", spicy=is_spicy
        )
        queued = 1
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {"added": added, "duplicates": duplicates, "queued": queued}
        )
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
    return await _detail_response(request, session, item)


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
    return await _detail_response(request, session, item)


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
    return await _detail_response(request, session, item)


# --- memes table (settings page) ---------------------------------------------

MEMES_PAGE_SIZE = 50


async def _memes_ctx(session: AsyncSession, page: int, flt: str) -> dict:
    """Table data: which model has processed which meme, plus its tags."""
    profiles = await _list_profiles(session)
    active_ids = [p.id for p in profiles if p.active]

    stmt = select(Item).order_by(Item.created_at.desc(), Item.id.desc())
    if flt == "notags":
        tagged = (
            select(ItemTag.item_id)
            .join(Tag, Tag.id == ItemTag.tag_id)
            .where(Tag.name != SPICY_TAG)
        )
        stmt = stmt.where(Item.id.not_in(tagged))
    elif flt == "missing" and active_ids:
        covered = (
            select(func.count(func.distinct(VlmText.profile_id)))
            .where(
                VlmText.item_id == Item.id,
                VlmText.profile_id.in_(active_ids),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(covered < len(active_ids))

    total = (
        await session.scalar(select(func.count()).select_from(stmt.subquery()))
    ) or 0
    page = max(page, 1)
    memes = list(
        await session.scalars(
            stmt.limit(MEMES_PAGE_SIZE).offset((page - 1) * MEMES_PAGE_SIZE)
        )
    )
    ids = [m.id for m in memes]
    texts: set[tuple[int, int]] = set()
    tag_map: dict[int, list[tuple[str, str]]] = {}
    if ids:
        rows = await session.execute(
            select(VlmText.item_id, VlmText.profile_id).where(
                VlmText.item_id.in_(ids)
            )
        )
        texts = {(item_id, profile_id) for item_id, profile_id in rows}
        tag_rows = await session.execute(
            select(ItemTag.item_id, Tag.name, ItemTag.source)
            .join(Tag, Tag.id == ItemTag.tag_id)
            .where(ItemTag.item_id.in_(ids))
            .order_by(Tag.name)
        )
        for item_id, name, source in tag_rows:
            tag_map.setdefault(item_id, []).append((name, source))
    return {
        "memes": memes,
        "profiles": profiles,
        "texts": texts,
        "tag_map": tag_map,
        "mpage": page,
        "mfilter": flt,
        "mpages": max(1, -(-total // MEMES_PAGE_SIZE)),
        "mtotal": total,
    }


@router.post("/ui/memes/reindex", response_class=HTMLResponse)
async def memes_reindex(
    request: Request,
    ids: list[int] = Form(default=[]),
    mpage: int = Form(1),
    mfilter: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    count = await requeue_items(session, ids)
    ctx = await _memes_ctx(session, mpage, mfilter)
    if count:
        ctx["notice"] = (
            f"{count} meme(s) queued — every active model will re-process "
            f"them on its next run (or hit Run indexer now in General)."
        )
    return templates.TemplateResponse(
        request, "partials/settings_memes.html", ctx
    )


# --- tags management (settings page) -----------------------------------------


async def _tags_panel_response(request: Request, session: AsyncSession):
    return templates.TemplateResponse(
        request,
        "partials/settings_tags.html",
        {"tag_stats": await items_svc.tag_stats(session)},
    )


@router.post("/ui/tags/{name}/delete", response_class=HTMLResponse)
async def tags_delete(
    request: Request,
    name: str,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
):
    await items_svc.delete_tag(session, search, name)
    return await _tags_panel_response(request, session)


@router.post("/ui/tags/clean", response_class=HTMLResponse)
async def tags_clean(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await items_svc.clean_unused_tags(session)
    return await _tags_panel_response(request, session)


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
