from conftest import write_png
from sqlalchemy import select

from memehog.core import items as items_svc
from memehog.core.library import ingest_file
from memehog.db.models import ItemTag, Tag


async def _two_tagged_items(settings, session_factory, search) -> tuple[int, int]:
    async with session_factory() as session:
        a, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "a.png", color="red"), origin="web",
        )
        b, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "b.png", color="blue"), origin="web",
        )
        a_id, b_id = a.id, b.id  # plain ints — survive expire_all in add_tag
        await items_svc.add_tag(session, search, a, "kot")
        b = await items_svc.get_item(session, b_id)
        await items_svc.add_tag(session, search, b, "kot")
        # mark b's link as AI-attached
        link = await session.scalar(
            select(ItemTag).where(ItemTag.item_id == b_id)
        )
        link.source = "ai"
        await session.commit()
        return a_id, b_id


async def test_tag_stats_counts(settings, session_factory, search):
    await _two_tagged_items(settings, session_factory, search)
    async with session_factory() as session:
        stats = await items_svc.tag_stats(session)
    kot = next(t for t in stats if t["name"] == "kot")
    assert kot["items"] == 2
    assert kot["ai_items"] == 1


async def test_delete_tag_reindexes(settings, session_factory, search):
    a_id, b_id = await _two_tagged_items(settings, session_factory, search)
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="kot")
        assert {h.id for h in hits} == {a_id, b_id}

        affected = await items_svc.delete_tag(session, search, "kot")
        assert affected == 2
        assert await session.scalar(select(Tag).where(Tag.name == "kot")) is None
        # FTS no longer matches the removed tag
        assert await items_svc.list_items(session, search, q="kot") == []


async def test_spicy_tag_protected(settings, session_factory, search):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "s.png", color="magenta"),
            origin="web", spicy=True,
        )
        assert await items_svc.delete_tag(session, search, "spicy") == 0
        assert (
            await session.scalar(select(Tag).where(Tag.name == "spicy"))
        ) is not None


async def test_clean_unused_tags(settings, session_factory, search):
    a_id, _b_id = await _two_tagged_items(settings, session_factory, search)
    async with session_factory() as session:
        a = await items_svc.get_item(session, a_id)
        await items_svc.add_tag(session, search, a, "jednorazowy")
        a = await items_svc.get_item(session, a_id)
        await items_svc.remove_tag(session, search, a, "jednorazowy")
        # removing the link leaves an orphaned Tag row behind
        assert await session.scalar(
            select(Tag).where(Tag.name == "jednorazowy")
        ) is not None

        removed = await items_svc.clean_unused_tags(session)
        assert removed == 1
        assert await session.scalar(
            select(Tag).where(Tag.name == "jednorazowy")
        ) is None


async def test_tags_tab_and_endpoints(client, settings, session_factory, search):
    await _two_tagged_items(settings, session_factory, search)

    page = await client.get("/settings?tab=tags")
    assert page.status_code == 200
    assert "kot" in page.text
    assert "Clean all unused" in page.text
    assert "(1 by AI" in page.text

    resp = await client.post("/ui/tags/kot/delete")
    assert resp.status_code == 200
    assert "kot" not in resp.text

    resp = await client.post("/ui/tags/clean")
    assert resp.status_code == 200


async def test_tag_deep_link_preselects_gallery(
    client, settings, session_factory, search
):
    await _two_tagged_items(settings, session_factory, search)

    page = await client.get("/settings?tab=tags")
    assert 'href="/?tag=kot"' in page.text

    gallery = await client.get("/?tag=kot")
    assert gallery.status_code == 200
    assert 'value="kot" selected' in gallery.text
    assert "tag=kot" in gallery.text  # initial grid load carries the filter
