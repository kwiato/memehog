from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

THUMB_SIZE = 512

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".avif"}
ANIMATION_EXTS = {".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v", ".ts"}
MEDIA_EXTS = IMAGE_EXTS | ANIMATION_EXTS | VIDEO_EXTS


@dataclass
class MediaInfo:
    media_type: str  # image | video | animation
    mime: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTS


def classify_extension(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ANIMATION_EXTS:
        return "animation"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def ffprobe(path: Path) -> dict | None:
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return None


def probe(path: Path) -> MediaInfo:
    media_type = classify_extension(path) or "image"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    info = MediaInfo(media_type=media_type, mime=mime)

    if media_type in ("image", "animation"):
        try:
            with Image.open(path) as img:
                info.width, info.height = img.size
        except OSError as exc:
            log.warning("Pillow failed to open %s: %s", path, exc)
    else:
        data = ffprobe(path)
        if data:
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    info.width = stream.get("width")
                    info.height = stream.get("height")
                    break
            duration = data.get("format", {}).get("duration")
            if duration is not None:
                info.duration = float(duration)
    return info


def make_thumbnail(src: Path, dst: Path, media_type: str) -> bool:
    """Create a JPEG thumbnail; returns False if it couldn't be made."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if media_type in ("image", "animation"):
        try:
            with Image.open(src) as img:
                img = img.convert("RGB")
                img.thumbnail((THUMB_SIZE, THUMB_SIZE))
                img.save(dst, "JPEG", quality=85)
            return True
        except OSError as exc:
            log.warning("Thumbnail failed for %s: %s", src, exc)
            return False

    if not shutil.which("ffmpeg"):
        log.warning("ffmpeg not found; no thumbnail for %s", src)
        return False
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(src),
            "-frames:v", "1", "-vf", f"scale='min({THUMB_SIZE},iw)':-2", str(dst),
        ],
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0 or not dst.exists():
        # Videos shorter than 1s: retry from the first frame
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(src),
                "-frames:v", "1", "-vf", f"scale='min({THUMB_SIZE},iw)':-2", str(dst),
            ],
            capture_output=True,
            timeout=120,
        )
    if result.returncode != 0:
        log.warning("ffmpeg thumbnail failed for %s: %s", src, result.stderr.decode(errors="replace"))
    return dst.exists()
