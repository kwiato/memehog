from conftest import write_png

from memehog.core import items as items_svc
from memehog.core.library import ingest_file


async def test_ingest_creates_item_and_thumbnail(settings, session_factory, search):
    src = write_png(settings.tmp_dir / "meme.png")
    async with session_factory() as session:
        item, created = await ingest_file(
            session, settings, search, src,
            origin="web", caption="funny cat meme",
        )
    assert created
    assert item.media_type == "image"
    assert item.width == 64 and item.height == 48
    assert (settings.library_dir / item.filename).exists()
    assert (settings.thumbs_dir / item.thumb_filename).exists()
    assert not src.exists()  # moved into the library


async def test_ingest_deduplicates_by_sha256(settings, session_factory, search):
    async with session_factory() as session:
        first, created1 = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png"), origin="web",
        )
        second, created2 = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "b.png"), origin="telegram",
        )
    assert created1 and not created2
    assert first.id == second.id


async def test_fts_search_by_caption(settings, session_factory, search):
    async with session_factory() as session:
        await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png", "red"),
            origin="web", caption="grumpy cat monday",
        )
        await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "b.png", "blue"),
            origin="web", caption="happy dog friday",
        )
        hits = await items_svc.list_items(session, search, q="grumpy")
        assert len(hits) == 1
        assert hits[0].caption == "grumpy cat monday"
        assert await items_svc.list_items(session, search, q="zebra") == []


async def test_tags_and_search_by_tag(settings, session_factory, search):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png"), origin="web",
        )
        item = await items_svc.add_tag(session, search, item, "Wholesome")
        assert [t.name for t in item.tags] == ["wholesome"]

        hits = await items_svc.list_items(session, search, q="wholesome")
        assert [h.id for h in hits] == [item.id]

        item = await items_svc.remove_tag(session, search, item, "wholesome")
        assert item.tags == []
        assert await items_svc.list_items(session, search, q="wholesome") == []


async def test_delete_removes_files_and_index(settings, session_factory, search):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png"),
            origin="web", caption="delete me",
        )
        media_path = settings.library_dir / item.filename
        thumb_path = settings.thumbs_dir / item.thumb_filename

        await items_svc.delete_item(session, settings, search, item)

        assert not media_path.exists()
        assert not thumb_path.exists()
        assert await items_svc.get_item(session, item.id) is None
        assert await items_svc.list_items(session, search, q="delete") == []
