from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..core.queue import DownloadQueue
from ..search.base import SearchBackend
from . import api, ui
from .styles import ensure_css


def create_app(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    search: SearchBackend,
    queue: DownloadQueue,
    scheduler=None,
) -> FastAPI:
    settings.ensure_dirs()
    app = FastAPI(title="Memehog", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.search = search
    app.state.queue = queue
    app.state.scheduler = scheduler

    app.include_router(api.public_router)
    app.include_router(api.router)
    app.include_router(ui.router)

    ensure_css()
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.mount("/media", StaticFiles(directory=str(settings.library_dir)), name="media")
    app.mount("/thumbs", StaticFiles(directory=str(settings.thumbs_dir)), name="thumbs")
    return app
