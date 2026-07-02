from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .media import is_media_file

log = logging.getLogger(__name__)

MAX_PLAYLIST_ITEMS = 20
MAX_DIRECT_SIZE = 500 * 1024 * 1024  # 500 MB

# Honest, descriptive UA: some CDNs (e.g. Wikimedia) reject the library
# default AND fake browser UAs, but accept identified tools.
USER_AGENT = "Memehog/0.1 (self-hosted media library; +https://github.com/memehog)"

INSTAGRAM_HOSTS = {"instagram.com", "instagr.am", "ddinstagram.com"}
TIKTOK_HOSTS = {"tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}


class DownloadError(Exception):
    pass


@dataclass
class DownloadedFile:
    path: Path
    source_url: str
    caption: str | None = None
    uploader: str | None = None


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def classify_url(url: str) -> str:
    """→ "instagram" | "tiktok" | "direct" | "generic" """
    host = _host(url)
    if host in INSTAGRAM_HOSTS or host.endswith(".instagram.com"):
        return "instagram"
    if host in TIKTOK_HOSTS or host.endswith(".tiktok.com"):
        return "tiktok"
    path = urlparse(url).path
    if is_media_file(Path(path)):
        return "direct"
    return "generic"


async def download_url(
    url: str, dest_dir: Path, cookies: Path | None = None
) -> list[DownloadedFile]:
    """Download whatever `url` points at into `dest_dir`.

    Returns one entry per media file (an Instagram carousel yields several).
    Raises DownloadError when nothing could be fetched.
    """
    kind = classify_url(url)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if kind == "direct":
        return [await _download_direct(url, dest_dir)]

    if kind == "instagram":
        strategies = (_download_gallery_dl, _download_ytdlp)
    else:  # tiktok, generic
        strategies = (_download_ytdlp, _download_gallery_dl)

    errors: list[str] = []
    for strategy in strategies:
        try:
            files = await strategy(url, dest_dir, cookies)
            if files:
                return files
            errors.append(f"{strategy.__name__}: no media files produced")
        except Exception as exc:  # noqa: BLE001 - collect and report per strategy
            log.info("%s failed for %s: %s", strategy.__name__, url, exc)
            errors.append(f"{strategy.__name__}: {exc}")
    raise DownloadError("; ".join(errors) or "no downloader could handle this URL")


async def _download_direct(url: str, dest_dir: Path) -> DownloadedFile:
    name = Path(urlparse(url).path).name or "download"
    name = re.sub(r"[^\w.\-]", "_", name)
    target = dest_dir / name
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=120, headers={"User-Agent": USER_AGENT}
    ) as client:
        async with client.stream("GET", url) as response:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise DownloadError(f"HTTP {exc.response.status_code} for {url}") from exc
            content_type = response.headers.get("content-type", "").split(";")[0]
            if not content_type.startswith(("image/", "video/")):
                raise DownloadError(f"not a media file (content-type: {content_type})")
            if not target.suffix:
                target = target.with_suffix(mimetypes.guess_extension(content_type) or ".bin")
            size = 0
            with target.open("wb") as fh:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DIRECT_SIZE:
                        raise DownloadError("file too large")
                    fh.write(chunk)
    # Keep the original filename searchable — the library renames files to hashes.
    stem = target.stem.replace("_", " ").replace("-", " ").strip()
    caption = stem if stem.lower() not in ("download", "file", "image", "video") else None
    return DownloadedFile(path=target, source_url=url, caption=caption)


async def _download_ytdlp(
    url: str, dest_dir: Path, cookies: Path | None
) -> list[DownloadedFile]:
    return await asyncio.to_thread(_ytdlp_sync, url, dest_dir, cookies)


def _ytdlp_sync(url: str, dest_dir: Path, cookies: Path | None) -> list[DownloadedFile]:
    from yt_dlp import YoutubeDL

    before = set(dest_dir.iterdir())
    opts = {
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "playlist_items": f"1-{MAX_PLAYLIST_ITEMS}",
        "noprogress": True,
    }
    if cookies:
        opts["cookiefile"] = str(cookies)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    caption = (info.get("title") or info.get("description") or "").strip() or None
    uploader = info.get("uploader") or info.get("uploader_id")
    new_files = [
        p for p in set(dest_dir.iterdir()) - before if p.is_file() and is_media_file(p)
    ]
    return [
        DownloadedFile(path=p, source_url=url, caption=caption, uploader=uploader)
        for p in sorted(new_files)
    ]


async def _download_gallery_dl(
    url: str, dest_dir: Path, cookies: Path | None
) -> list[DownloadedFile]:
    before = set(dest_dir.rglob("*"))
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "-D", str(dest_dir),
        "--range", f"1-{MAX_PLAYLIST_ITEMS}",
        "-q",
    ]
    if cookies:
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise DownloadError("gallery-dl timed out")

    new_files = [
        p for p in set(dest_dir.rglob("*")) - before if p.is_file() and is_media_file(p)
    ]
    if proc.returncode != 0 and not new_files:
        raise DownloadError(
            f"gallery-dl exited with {proc.returncode}: "
            f"{stderr.decode(errors='replace').strip()[:500]}"
        )
    return [DownloadedFile(path=p, source_url=url) for p in sorted(new_files)]
