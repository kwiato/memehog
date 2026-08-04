from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..core.library import SPICY_TAG
from ..core.queue import DownloadQueue
from ..search.base import SearchBackend
from . import api, inbox, public, ui
from .styles import ensure_css

# Set by the reverse proxy on the public vhost (and stripped from incoming
# requests there). Requests carrying it are untrusted public traffic and only
# ever see the allowlist below — default deny, so a new admin route can never
# leak by accident. Admin access comes in WITHOUT the header (Tailscale
# directly, or the basic-auth vhost which proxies without it).
PUBLIC_HEADER = "x-memehog-public"

_PUBLIC_PREFIXES = ("/public/", "/static/", "/thumbs/", "/media/")
_PUBLIC_EXACT = ("/", "/api/v1/health", "/favicon.ico")


def _public_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def public_gate(request: Request, call_next):
        request.state.public = request.headers.get(PUBLIC_HEADER) == "1"
        if request.state.public:
            path = request.url.path
            if not (path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)):
                return Response(status_code=404)
            if path.startswith(f"/media/{SPICY_TAG}/"):
                return Response(status_code=403)
        return await call_next(request)


def create_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    search: SearchBackend,
    queue: DownloadQueue,
    scheduler=None,
    bot=None,
) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="Memehog", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.search = search
    app.state.queue = queue
    app.state.scheduler = scheduler
    app.state.bot = bot

    _public_gate(app)
    app.include_router(api.public_router)
    app.include_router(api.router)
    app.include_router(public.router)
    app.include_router(ui.router)
    app.include_router(inbox.router)

    ensure_css()
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.mount("/media", StaticFiles(directory=str(settings.library_dir)), name="media")
    app.mount("/thumbs", StaticFiles(directory=str(settings.thumbs_dir)), name="thumbs")
    return app
