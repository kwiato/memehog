"""Public feed routes ("Feed the hog!").

Requests reaching these carry the reverse proxy's public header (see
`web.__init__.PUBLIC_HEADER`) — the middleware there guarantees public
traffic can only ever hit `/`, `/public/*` and the static mounts. Visitors
get a daily meme allowance tracked against a signed cookie (plus a blunt
per-IP cap); uploading a meme through "Feed the hog!" tops the allowance up
and drops the file into the existing guest-submission quarantine, voted on
by the owner in Telegram."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..config import Settings
from ..core import items as items_svc
from ..core import submissions as subs_svc
from ..db.models import Visitor
from ..search.base import SearchBackend
from .deps import get_search, get_session, get_settings
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

FEED_PAGE_SIZE = 10
VISITOR_COOKIE = "memehog_visitor"
# Clearing cookies must not grant infinite scrolling: a per-IP counter (no
# credits attached) caps the total at a small multiple of the daily limit.
IP_LIMIT_FACTOR = 4

HOG_REPLIES = {
    "too_many_pending": "The hog is still chewing your last memes — "
                        "wait for the owner's votes!",
    "daily_limit": "The hog is full for today — try again tomorrow.",
    "duplicate": "The hog has tasted that one before. Feed it something new!",
}


def _sign(value: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), value.encode(), hashlib.sha256
    ).hexdigest()[:32]


def _visitor_from_cookie(request: Request, settings: Settings) -> tuple[str, str | None]:
    """(visitor_id, cookie_to_set_or_None) — HMAC-signed, stdlib only."""
    raw = request.cookies.get(VISITOR_COOKIE, "")
    if raw:
        vid, _, sig = raw.partition(".")
        if vid and hmac.compare_digest(_sign(vid, settings.api_token), sig):
            return vid, None
    vid = secrets.token_hex(8)
    return vid, f"{vid}.{_sign(vid, settings.api_token)}"


def _ip_key(request: Request) -> str:
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    return "ip:" + hashlib.sha256(ip.encode()).hexdigest()[:24]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _get_visitor(session: AsyncSession, key: str) -> Visitor:
    visitor = await session.get(Visitor, key)
    if visitor is None:
        visitor = Visitor(id=key, day=_today())
        session.add(visitor)
        await session.flush()
    if visitor.day != _today():
        visitor.day = _today()
        visitor.served = 0
    return visitor


async def _consume_quota(
    session: AsyncSession,
    settings: Settings,
    visitor_key: str,
    ip_key: str,
    count: int,
) -> bool:
    """True if `count` more memes may be served; consumes credits past the
    daily limit. The IP counter is a hard secondary cap without credits."""
    visitor = await _get_visitor(session, visitor_key)
    ip_row = await _get_visitor(session, ip_key)
    limit = settings.public_daily_limit

    if ip_row.served + count > limit * IP_LIMIT_FACTOR:
        return False
    over = max(0, (visitor.served + count) - limit)
    if over > visitor.credits:
        return False

    visitor.credits -= over
    visitor.served += count
    ip_row.served += count
    await session.commit()
    return True


def _feed_ctx(request: Request, **extra) -> dict:
    return {"request": request, **extra}


async def render_public_index(
    request: Request, session: AsyncSession, settings: Settings
):
    """The public landing page — served on `/` for public traffic
    (see ui.index, which delegates here when request.state.public)."""
    count = await items_svc.count_items(session)
    seed = secrets.randbelow(999_000) + 17
    return templates.TemplateResponse(
        request, "public.html", {"count": count, "seed": seed}
    )


@router.get("/public/feed", response_class=HTMLResponse)
async def public_feed(
    request: Request,
    mode: str = "latest",
    q: str = "",
    tag: str = "",
    seed: int = 17,
    page: int = 1,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
    settings: Settings = Depends(get_settings),
):
    vid, new_cookie = _visitor_from_cookie(request, settings)

    if q.strip():
        items = await items_svc.list_items(
            session, search, q=q, tag=tag, page=page, page_size=FEED_PAGE_SIZE
        )
    elif mode == "random":
        items = await items_svc.random_feed(
            session, seed=seed, tag=tag, page=page, page_size=FEED_PAGE_SIZE
        )
    else:
        items = await items_svc.list_items(
            session, search, tag=tag, page=page, page_size=FEED_PAGE_SIZE
        )

    allowed = True
    if items:
        allowed = await _consume_quota(
            session, settings, vid, _ip_key(request), len(items)
        )

    if not allowed:
        response = templates.TemplateResponse(
            request, "partials/feed_the_hog.html",
            {"mode": mode, "q": q, "tag": tag, "seed": seed, "page": page},
        )
    else:
        response = templates.TemplateResponse(
            request,
            "partials/public_feed.html",
            {
                "items": items,
                "has_more": len(items) == FEED_PAGE_SIZE,
                "mode": mode, "q": q, "tag": tag, "seed": seed, "page": page,
            },
        )
    if new_cookie:
        response.set_cookie(
            VISITOR_COOKIE, new_cookie,
            max_age=365 * 24 * 3600, httponly=True, samesite="lax",
        )
    return response


@router.post("/public/hog", response_class=HTMLResponse)
async def feed_the_hog(
    request: Request,
    file: UploadFile,
    mode: str = Form("latest"),
    q: str = Form(""),
    tag: str = Form(""),
    seed: int = Form(17),
    page: int = Form(1),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    vid, new_cookie = _visitor_from_cookie(request, settings)

    if not (file.content_type or "").startswith(("image/", "video/")):
        return _hog_error(request, "The hog only eats images and videos.")

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "meme.bin").suffix or ".bin"
    tmp_path = settings.tmp_dir / f"hog-{uuid.uuid4().hex[:8]}{suffix}"
    with tmp_path.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)

    # Reuse the guest-submission pipeline: quarantine, sha dedupe, per-sender
    # rate limits — keyed by a negative int derived from the visitor id so it
    # can't collide with real Telegram ids.
    submitter_id = -int(hashlib.sha256(vid.encode()).hexdigest()[:12], 16)
    submission, reason = await subs_svc.create_submission(
        session, settings, tmp_path,
        submitter_id=submitter_id,
        submitter_name=f"web:{vid[:8]}",
        caption=None,
    )
    if submission is None:
        return _hog_error(request, HOG_REPLIES.get(reason, "That didn't work."))

    # Unlock immediately — moderation decides later whether the meme stays.
    visitor = await _get_visitor(session, vid)
    visitor.credits += settings.public_unlock_credits
    await session.commit()

    bot = request.app.state.bot
    if bot is not None:
        from ..bot import send_submission_votes

        try:
            await send_submission_votes(
                bot, settings, session, submission,
                header=f"🗳 Meme #{submission.id} from the public feed "
                       f"(web:{vid[:8]})",
            )
        except Exception:  # noqa: BLE001 - the unlock must not fail on TG hiccups
            log.exception("Couldn't send public submission %s to Telegram",
                          submission.id)

    response = templates.TemplateResponse(
        request,
        "partials/hog_fed.html",
        {
            "credits": settings.public_unlock_credits,
            "mode": mode, "q": q, "tag": tag, "seed": seed, "page": page,
        },
    )
    if new_cookie:
        response.set_cookie(
            VISITOR_COOKIE, new_cookie,
            max_age=365 * 24 * 3600, httponly=True, samesite="lax",
        )
    return response


def _hog_error(request: Request, message: str):
    return templates.TemplateResponse(
        request, "partials/hog_fed.html", {"error": message}
    )
