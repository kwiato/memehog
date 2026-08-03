from conftest import make_png, write_png

from memehog.core import items as items_svc
from memehog.core.library import ingest_file


async def _ingest(settings, session_factory, search, name="a.png", color="red"):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / name, color=color),
            origin="web", caption="do przycięcia",
        )
        return item


async def test_crop_replaces_original(client, settings, session_factory, search):
    item = await _ingest(settings, session_factory, search)
    old_rel, old_sha = item.filename, item.sha256

    resp = await client.post(
        f"/ui/items/{item.id}/crop",
        files={"file": ("crop.png", make_png("yellow", size=(32, 20)), "image/png")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.sha256 != old_sha
        assert fresh.filename != old_rel
        assert fresh.width == 32 and fresh.height == 20
        assert (settings.library_dir / fresh.filename).exists()
        assert not (settings.library_dir / old_rel).exists()
        assert (settings.thumbs_dir / fresh.thumb_filename).exists()
        # still searchable after the reindex
        hits = await items_svc.list_items(session, search, q="przycięcia")
        assert [h.id for h in hits] == [item.id]


async def test_crop_collision_refused(client, settings, session_factory, search):
    keeper = await _ingest(settings, session_factory, search, "k.png", "green")
    victim = await _ingest(settings, session_factory, search, "v.png", "blue")

    resp = await client.post(
        f"/ui/items/{victim.id}/crop",
        files={"file": ("crop.png", make_png("green"), "image/png")},
    )
    assert resp.status_code == 409
    assert f"#{keeper.id}" in resp.json()["error"]
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, victim.id)
        assert (settings.library_dir / fresh.filename).exists()  # untouched


async def test_crop_rejects_non_images(client, settings, session_factory, search):
    item = await _ingest(settings, session_factory, search)
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        fresh.media_type = "video"
        await session.commit()

    resp = await client.get(f"/ui/items/{item.id}/crop")
    assert resp.status_code == 400
    resp = await client.post(
        f"/ui/items/{item.id}/crop",
        files={"file": ("crop.png", make_png("red"), "image/png")},
    )
    assert resp.status_code == 400
