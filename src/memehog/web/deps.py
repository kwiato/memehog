from __future__ import annotations

import secrets
from typing import AsyncIterator

from fastapi import Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core.queue import DownloadQueue
from ..search.base import SearchBackend


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_search(request: Request) -> SearchBackend:
    return request.app.state.search


def get_queue(request: Request) -> DownloadQueue:
    return request.app.state.queue


async def require_token(
    request: Request, authorization: str = Header(default="")
) -> None:
    expected = request.app.state.settings.api_token
    provided = authorization.removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
