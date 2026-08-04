from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from memehog.config import Settings
from memehog.core.queue import DownloadQueue
from memehog.db import create_engine, init_db
from memehog.search import FtsSearch
from memehog.web import create_app

TEST_TOKEN = "test-token-123"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(
        api_token=TEST_TOKEN,
        data_dir=tmp_path / "data",
        _env_file=None,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
async def session_factory(settings: Settings):
    engine = create_engine(settings.db_path)
    factory = await init_db(engine)
    yield factory
    await engine.dispose()


@pytest.fixture
def search() -> FtsSearch:
    return FtsSearch()


@pytest.fixture
async def client(settings, session_factory, search):
    queue = DownloadQueue(session_factory, settings, search)
    app = create_app(settings, session_factory, search, queue)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client


def make_png(color: str = "red", size: tuple[int, int] = (64, 48)) -> bytes:
    """Patterned, color-dependent test image. Flat fills would all share the
    degenerate all-zero dHash, so the near-duplicate detector needs test
    images that are genuinely different pictures per color."""
    from PIL import ImageDraw

    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    seed = sum(color) if isinstance(color, tuple) else sum(str(color).encode())
    # Relative coordinates: the same color at any size is the same picture,
    # just scaled — exactly what perceptual hashing must treat as equal.
    for i in range(5):
        fx = ((seed * (2 * i + 3)) % 97) / 97
        x = int(fx * size[0])
        draw.line(
            [(x, 0), (size[0] - x, size[1])],
            fill="white" if i % 2 else "black",
            width=max(2, size[0] // 16),
        )
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def write_png(path: Path, color: str = "red") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_png(color))
    return path


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}
