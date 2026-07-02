import io
import shutil

import pytest
from PIL import Image

from memehog.core import appsettings
from memehog.core.convert import run_conversions
from memehog.core.library import ingest_file


def write_webp(path, color="red"):
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, "WEBP")
    path.write_bytes(buf.getvalue())
    return path


async def test_webp_converted_to_jpg(settings, session_factory, search):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_webp(settings.tmp_dir / "meme.webp"),
            origin="web", caption="webp meme",
        )
        assert item.filename.endswith(".webp")
        old_rel = item.filename
        old_sha = item.sha256

    converted = await run_conversions(session_factory, settings, search)
    assert converted == 1

    async with session_factory() as session:
        from memehog.core import items as items_svc

        fresh = await items_svc.get_item(session, item.id)
        assert fresh.filename.endswith(".jpg")
        assert fresh.mime == "image/jpeg"
        assert fresh.sha256 != old_sha
        assert (settings.library_dir / fresh.filename).exists()
        assert not (settings.library_dir / old_rel).exists()
        assert (settings.thumbs_dir / fresh.thumb_filename).exists()
        # still searchable after reindex
        hits = await items_svc.list_items(session, search, q="webp meme")
        assert [h.id for h in hits] == [item.id]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_webm_conversion_requires_ffmpeg(settings, session_factory, search):
    import subprocess

    src = settings.tmp_dir / "clip.webm"
    src.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=10",
            "-c:v", "libvpx", str(src),
        ],
        check=True, capture_output=True, timeout=120,
    )
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search, src, origin="web", caption="webm clip"
        )

    assert await run_conversions(session_factory, settings, search) == 1
    async with session_factory() as session:
        from memehog.core import items as items_svc

        fresh = await items_svc.get_item(session, item.id)
        assert fresh.filename.endswith(".mp4")
        assert (settings.library_dir / fresh.filename).exists()


async def test_yearly_folder_layout(settings, session_factory, search):
    from conftest import write_png
    from memehog.db.models import utcnow

    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png"), origin="web",
        )
    # library/YYYY/<name>, no month subfolder
    parts = item.filename.split("/")
    assert len(parts) == 2
    assert parts[0] == f"{utcnow():%Y}"


async def test_scan_hour_setting(client, session_factory):
    resp = await client.post("/ui/settings/scan-hour", data={"hour": "5"})
    assert resp.status_code == 200
    assert 'value="5" selected' in resp.text

    async with session_factory() as session:
        cron = await appsettings.get_setting(session, appsettings.SCAN_CRON_KEY)
    assert cron == "0 5 * * *"
