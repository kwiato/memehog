from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..core import items as items_svc
from ..core.library import ingest_file
from ..core.queue import DownloadQueue
from ..db.models import Item, Job
from ..search.base import SearchBackend
from .deps import get_queue, get_search, get_session, get_settings, require_token

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_token)])
public_router = APIRouter(prefix="/api/v1")


def item_to_dict(item: Item) -> dict:
    return {
        "id": item.id,
        "sha256": item.sha256,
        "media_type": item.media_type,
        "mime": item.mime,
        "file_size": item.file_size,
        "url": f"/media/{item.filename}",
        "thumb_url": f"/thumbs/{item.thumb_filename}" if item.thumb_filename else None,
        "source_url": item.source_url,
        "origin": item.origin,
        "caption": item.caption,
        "uploader": item.uploader,
        "width": item.width,
        "height": item.height,
        "duration": item.duration,
        "tags": [tag.name for tag in item.tags],
        "created_at": item.created_at.isoformat(),
    }


def job_to_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "url": job.url,
        "status": job.status,
        "error": job.error,
        "item_id": job.item_id,
        "created_at": job.created_at.isoformat(),
    }


@public_router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/items")
async def list_items(
    q: str = "",
    tag: str = "",
    type: str = "",
    page: int = 1,
    page_size: int = items_svc.PAGE_SIZE,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
) -> dict:
    items = await items_svc.list_items(
        session, search, q=q, tag=tag, media_type=type,
        page=page, page_size=min(page_size, 200),
    )
    return {"page": page, "items": [item_to_dict(i) for i in items]}


@router.get("/items/{item_id}")
async def get_item(
    item_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    return item_to_dict(item)


@router.post("/items", status_code=201)
async def upload_item(
    file: UploadFile,
    caption: str = Form(""),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
) -> dict:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    name = file.filename or "upload.bin"
    tmp_path = settings.tmp_dir / f"api-{uuid.uuid4().hex[:8]}-{name}"
    with tmp_path.open("wb") as fh:
        while chunk := await file.read(1024 * 1024):
            fh.write(chunk)
    item, created = await ingest_file(
        session, settings, search, tmp_path,
        origin="api", caption=caption or None,
    )
    return {"created": created, **item_to_dict(item)}


@router.delete("/items/{item_id}", status_code=204)
async def delete_item(
    item_id: int,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    search: SearchBackend = Depends(get_search),
) -> None:
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    await items_svc.delete_item(session, settings, search, item)


class TagBody(BaseModel):
    name: str


@router.post("/items/{item_id}/tags")
async def add_tag(
    item_id: int,
    body: TagBody,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
) -> dict:
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    item = await items_svc.add_tag(session, search, item, body.name)
    return item_to_dict(item)


@router.delete("/items/{item_id}/tags/{name}")
async def remove_tag(
    item_id: int,
    name: str,
    session: AsyncSession = Depends(get_session),
    search: SearchBackend = Depends(get_search),
) -> dict:
    item = await items_svc.get_item(session, item_id)
    if item is None:
        raise HTTPException(404, "Item not found")
    item = await items_svc.remove_tag(session, search, item, name)
    return item_to_dict(item)


class JobBody(BaseModel):
    url: str


@router.post("/jobs", status_code=202)
async def submit_job(
    body: JobBody, queue: DownloadQueue = Depends(get_queue)
) -> dict:
    job = await queue.submit(body.url, origin="api")
    return job_to_dict(job)


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job_to_dict(job)
