from conftest import make_png, write_png
from sqlalchemy import select

from memehog.core import items as items_svc
from memehog.core.library import ingest_file
from memehog.db.models import Submission, Visitor

PUB = {"X-Memehog-Public": "1"}


async def _ingest(settings, session_factory, search, name, color, **kwargs):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / name, color=color),
            origin="web", **kwargs,
        )
        return item


async def test_public_gate_default_deny(client, settings, session_factory, search):
    # admin surfaces are invisible to public traffic — default deny
    for path in ("/settings", "/ui/items", "/ui/settings/vlm/status",
                 "/api/v1/items", "/api/docs"):
        resp = await client.get(path, headers=PUB)
        assert resp.status_code == 404, path
    # ...but keep working without the header (Tailscale / basic-auth side)
    assert (await client.get("/settings")).status_code == 200
    assert (await client.get("/api/v1/health", headers=PUB)).status_code == 200


async def test_public_spicy_media_blocked(client, settings, session_factory, search):
    item = await _ingest(
        settings, session_factory, search, "hot.png", "magenta", spicy=True
    )
    url = f"/media/{item.filename}"
    assert item.filename.startswith("spicy/")
    assert (await client.get(url, headers=PUB)).status_code == 403
    assert (await client.get(url)).status_code == 200  # admin side unaffected


async def test_public_index_and_feed(client, settings, session_factory, search):
    a = await _ingest(settings, session_factory, search, "a.png", "red",
                      caption="pierwszy mem")
    b = await _ingest(settings, session_factory, search, "b.png", "blue")

    page = await client.get("/", headers=PUB)
    assert page.status_code == 200
    assert "Feed the hog" not in page.text  # not hit the wall yet
    assert "/public/feed" in page.text

    feed = await client.get("/public/feed?mode=latest&page=1", headers=PUB)
    assert feed.status_code == 200
    assert f"/media/{b.filename}" in feed.text  # newest first
    assert "pierwszy mem" in feed.text
    # spicy never appears
    hot = await _ingest(settings, session_factory, search, "h.png", "green",
                        spicy=True)
    feed = await client.get("/public/feed?mode=latest&page=1", headers=PUB)
    assert hot.filename not in feed.text


async def test_random_feed_is_stable_per_seed(
    client, settings, session_factory, search
):
    for i, color in enumerate(["red", "blue", "green", "yellow", "cyan"]):
        await _ingest(settings, session_factory, search, f"r{i}.png", color)

    async with session_factory() as session:
        first = await items_svc.random_feed(session, seed=123, page=1, page_size=3)
        again = await items_svc.random_feed(session, seed=123, page=1, page_size=3)
        second_page = await items_svc.random_feed(
            session, seed=123, page=2, page_size=3
        )
    assert [i.id for i in first] == [i.id for i in again]
    assert not set(i.id for i in first) & set(i.id for i in second_page)


async def test_quota_wall_and_hog_unlock(client, settings, session_factory, search):
    settings.public_daily_limit = 2
    for i, color in enumerate(["red", "blue", "green"]):
        await _ingest(settings, session_factory, search, f"q{i}.png", color)

    # 3 items > limit 2 → the wall comes up instead of the feed
    feed = await client.get("/public/feed?mode=latest&page=1", headers=PUB)
    assert "Feed the hog!" in feed.text
    assert "hx-post=\"/public/hog\"" in feed.text

    # feeding the hog: new meme → quarantined submission + credits
    resp = await client.post(
        "/public/hog",
        headers=PUB,
        files={"file": ("snack.png", make_png("magenta"), "image/png")},
        data={"mode": "latest", "page": "1", "seed": "17"},
    )
    assert resp.status_code == 200
    assert "unlocked" in resp.text

    async with session_factory() as session:
        subs = list(await session.scalars(select(Submission)))
        assert len(subs) == 1
        assert subs[0].status == "pending"
        assert subs[0].submitter_id < 0  # web visitor, not a telegram id
        visitors = list(await session.scalars(select(Visitor)))
        assert any(v.credits == settings.public_unlock_credits for v in visitors)

    # with credits the feed flows again (same client → same cookie)
    feed = await client.get("/public/feed?mode=latest&page=1", headers=PUB)
    assert "Feed the hog!" not in feed.text
    assert "feed-item" in feed.text


async def test_hog_duplicate_refused(client, settings, session_factory, search):
    settings.public_daily_limit = 1000
    await _ingest(settings, session_factory, search, "lib.png", "red")

    resp = await client.post(
        "/public/hog",
        headers=PUB,
        files={"file": ("dup.png", make_png("red"), "image/png")},
    )
    assert resp.status_code == 200
    assert "tasted that one before" in resp.text
    async with session_factory() as session:
        assert list(await session.scalars(select(Submission))) == []
