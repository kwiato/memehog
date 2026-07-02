from conftest import write_png

from memehog.core import items as items_svc
from memehog.core.library import ingest_file


async def _make_item(settings, session, search, name, caption, spicy=False):
    item, _ = await ingest_file(
        session, settings, search,
        write_png(settings.tmp_dir / name, color=name.split(".")[0]),
        origin="web", caption=caption, spicy=spicy,
    )
    return item


async def test_spicy_items_hidden_by_default(settings, session_factory, search):
    async with session_factory() as session:
        normal = await _make_item(settings, session, search, "red.png", "normal cat")
        spicy = await _make_item(settings, session, search, "blue.png", "spicy cat")
        spicy = await items_svc.toggle_spicy(session, settings, search, spicy)

        default_view = await items_svc.list_items(session, search)
        assert [i.id for i in default_view] == [normal.id]

        spicy_view = await items_svc.list_items(session, search, spicy=True)
        assert [i.id for i in spicy_view] == [spicy.id]

        # search respects the mode too
        assert [
            i.id for i in await items_svc.list_items(session, search, q="cat")
        ] == [normal.id]
        assert [
            i.id
            for i in await items_svc.list_items(session, search, q="cat", spicy=True)
        ] == [spicy.id]


async def test_toggle_spicy_back(settings, session_factory, search):
    async with session_factory() as session:
        item = await _make_item(settings, session, search, "green.png", "meme")
        item = await items_svc.toggle_spicy(session, settings, search, item)
        assert "spicy" in {t.name for t in item.tags}
        item = await items_svc.toggle_spicy(session, settings, search, item)
        assert "spicy" not in {t.name for t in item.tags}
        assert [
            i.id for i in await items_svc.list_items(session, search)
        ] == [item.id]


async def test_spicy_ingest_lands_in_spicy_folder(settings, session_factory, search):
    async with session_factory() as session:
        item = await _make_item(
            settings, session, search, "purple.png", "hot", spicy=True
        )
        assert item.filename.startswith("spicy/")
        assert (settings.library_dir / item.filename).exists()
        assert "spicy" in {t.name for t in item.tags}
        # hidden from the default view straight away
        assert await items_svc.list_items(session, search) == []
        assert [
            i.id for i in await items_svc.list_items(session, search, spicy=True)
        ] == [item.id]


async def test_toggle_spicy_moves_file(settings, session_factory, search):
    async with session_factory() as session:
        item = await _make_item(settings, session, search, "orange.png", "meme")
        plain_rel = item.filename

        item = await items_svc.toggle_spicy(session, settings, search, item)
        assert item.filename == f"spicy/{plain_rel}"
        assert (settings.library_dir / item.filename).exists()
        assert not (settings.library_dir / plain_rel).exists()

        item = await items_svc.toggle_spicy(session, settings, search, item)
        assert item.filename == plain_rel
        assert (settings.library_dir / plain_rel).exists()
        assert not (settings.library_dir / "spicy" / plain_rel).exists()


async def test_spicy_tag_not_in_filter_dropdown(settings, session_factory, search):
    async with session_factory() as session:
        item = await _make_item(settings, session, search, "red.png", "x")
        await items_svc.toggle_spicy(session, settings, search, item)
        names = [t.name for t in await items_svc.all_tags(session)]
        assert "spicy" not in names


async def test_ui_spicy_toggle_endpoint(client):
    from conftest import auth, make_png

    resp = await client.post(
        "/api/v1/items", headers=auth(),
        files={"file": ("a.png", make_png("red"), "image/png")},
    )
    item_id = resp.json()["id"]

    resp = await client.post(f"/ui/items/{item_id}/spicy")
    assert resp.status_code == 200
    assert "Unmark spicy" in resp.text

    # hidden from the default grid, visible in spicy mode
    grid = await client.get("/ui/items", params={"page": 1})
    assert 'hx-get="/ui/items/' + str(item_id) not in grid.text
    grid = await client.get("/ui/items", params={"page": 1, "spicy": "1"})
    assert f'/ui/items/{item_id}/detail' in grid.text


async def test_ui_upload_spicy(client):
    from conftest import make_png

    resp = await client.post(
        "/ui/upload",
        data={"spicy": "1"},
        files={"files": ("hot.png", make_png("magenta"), "image/png")},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 204

    grid = await client.get("/ui/items", params={"page": 1})
    assert "/detail" not in grid.text
    grid = await client.get("/ui/items", params={"page": 1, "spicy": "1"})
    assert "/detail" in grid.text
    # the file itself is served from the spicy subfolder
    assert "spicy/" in (await client.get("/ui/items/1/detail")).text
