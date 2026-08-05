import io
import json

import httpx
import pytest
from conftest import make_png
from PIL import Image
from sqlalchemy import select

from memehog.core.crawler import crawl_once, parse_sources
from memehog.core.phash import dhash_image, hamming, is_near, near_any
from memehog.db.models import Candidate, Item, RejectedHash
from memehog.web import inbox as inbox_module

TODAY = "2026-08-04"


# --- phash unit tests --------------------------------------------------------


def _hash_of(png: bytes) -> str:
    with Image.open(io.BytesIO(png)) as img:
        return dhash_image(img)


def test_dhash_survives_reencoding_but_separates_images():
    original = _hash_of(make_png("red", size=(640, 480)))
    # the same picture, scaled down hard
    resized = _hash_of(make_png("red", size=(64, 48)))
    other = _hash_of(make_png("blue", size=(640, 480)))
    assert is_near(original, resized)
    assert hamming(original, other) > 10
    assert not is_near(original, other)


def test_degenerate_hash_never_matches():
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), "gray").save(buf, "PNG")
    flat = _hash_of(buf.getvalue())
    assert not is_near(flat, flat)
    assert not near_any(flat, [flat])


def test_parse_sources():
    assert parse_sources(
        "reddit:memes 40\n\n  rss:https://x/feed  \nreddit:dank\n"
    ) == [
        ("reddit:memes", 40),
        ("rss:https://x/feed", None),
        ("reddit:dank", None),
    ]


# --- crawl_once --------------------------------------------------------------


def reddit_listing(posts: list[dict]) -> dict:
    return {"data": {"children": [{"data": p} for p in posts]}}


def crawl_transport(listing: dict, images: dict[str, bytes]):
    """Mock network: reddit JSON + direct image downloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "top.json" in url:
            return httpx.Response(200, json=listing)
        for suffix, data in images.items():
            if url.endswith(suffix):
                return httpx.Response(
                    200, content=data, headers={"content-type": "image/png"}
                )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def post(name: str, ups: int, **extra) -> dict:
    return {
        "title": f"meme {name}",
        "url": f"https://i.redd.it/{name}.png",
        "permalink": f"/r/memes/comments/{name}/",
        "ups": ups,
        **extra,
    }


async def test_crawl_builds_todays_batch(settings, session_factory):
    settings.crawler_sources = "reddit:memes"
    images = {
        "a.png": make_png("red", size=(320, 240)),
        "b.png": make_png("blue", size=(320, 240)),
    }
    listing = reddit_listing([
        post("a", 500),
        post("b", 300),
        post("nsfw", 900, over_18=True),          # filtered
        post("video", 800, url="https://v.redd.it/xyz"),  # not an image
    ])
    added = await crawl_once(
        session_factory, settings,
        transport=crawl_transport(listing, images), today=TODAY,
    )
    assert added == 2
    async with session_factory() as session:
        cands = list(await session.scalars(select(Candidate).order_by(Candidate.id)))
        assert [c.title for c in cands] == ["meme a", "meme b"]
        assert cands[0].score == 500
        assert cands[0].day == TODAY
        assert cands[0].phash
        assert (settings.candidates_dir / cands[0].thumb_filename).exists()

    # a re-run the same day adds nothing new (urls + phash already known)
    assert await crawl_once(
        session_factory, settings,
        transport=crawl_transport(listing, images), today=TODAY,
    ) == 0


async def test_crawl_skips_known_and_rejected_memes(
    settings, session_factory, search
):
    from test_indexer import ingest_png

    settings.crawler_sources = "reddit:memes"
    # "red" is already in the library (crawled copy is just re-scaled)
    await ingest_png(session_factory, settings, search, color="red")
    # "green" was swiped away in the past
    async with session_factory() as session:
        with Image.open(io.BytesIO(make_png("green"))) as img:
            session.add(RejectedHash(phash=dhash_image(img)))
        await session.commit()

    images = {
        "red.png": make_png("red", size=(500, 375)),
        "green.png": make_png("green", size=(500, 375)),
        "fresh.png": make_png("purple", size=(500, 375)),
    }
    listing = reddit_listing(
        [post("red", 900), post("green", 700), post("fresh", 100)]
    )
    added = await crawl_once(
        session_factory, settings,
        transport=crawl_transport(listing, images), today=TODAY,
    )
    assert added == 1
    async with session_factory() as session:
        cands = list(await session.scalars(select(Candidate)))
        assert [c.title for c in cands] == ["meme fresh"]


async def test_crawl_respects_daily_target(settings, session_factory):
    settings.crawler_sources = "reddit:memes"
    settings.crawler_daily_target = 1
    images = {
        "a.png": make_png("red"),
        "b.png": make_png("blue"),
    }
    listing = reddit_listing([post("a", 500), post("b", 300)])
    added = await crawl_once(
        session_factory, settings,
        transport=crawl_transport(listing, images), today=TODAY,
    )
    assert added == 1


async def test_reddit_oauth_path(settings, session_factory):
    """With app credentials set the crawler grabs a token and talks to
    oauth.reddit.com with a bearer header."""
    settings.crawler_sources = "reddit:memes"
    settings.crawler_reddit_client_id = "cid"
    settings.crawler_reddit_secret = "sec"
    png = make_png("red", size=(320, 240))
    seen = {"token": False, "oauth": False}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/v1/access_token"):
            seen["token"] = True
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok-123"})
        if url.startswith("https://oauth.reddit.com/r/memes/top"):
            seen["oauth"] = True
            assert request.headers["Authorization"] == "bearer tok-123"
            return httpx.Response(200, json=reddit_listing([post("a", 500)]))
        if url.endswith("a.png"):
            return httpx.Response(200, content=png)
        raise AssertionError(f"unexpected URL {url}")

    added = await crawl_once(
        session_factory, settings,
        transport=httpx.MockTransport(handler), today=TODAY,
    )
    assert added == 1
    assert seen == {"token": True, "oauth": True}


async def test_per_source_cap(settings, session_factory):
    """"reddit:memes 1" takes at most one meme from that subreddit even
    when the daily target has room for more."""
    settings.crawler_sources = "reddit:memes 1"
    images = {
        "a.png": make_png("red"),
        "b.png": make_png("blue"),
    }
    listing = reddit_listing([post("a", 500), post("b", 300)])
    added = await crawl_once(
        session_factory, settings,
        transport=crawl_transport(listing, images), today=TODAY,
    )
    assert added == 1
    async with session_factory() as session:
        cands = list(await session.scalars(select(Candidate)))
        assert [c.title for c in cands] == ["meme a"]  # the higher-voted one


async def test_source_stats_scoreboard(client, settings, session_factory):
    """Swiped candidates feed the per-source keep-rate table in settings."""
    kept = await seed_candidate(session_factory, settings, color="red")
    lost = await seed_candidate(session_factory, settings, color="blue")
    async with session_factory() as session:
        (await session.get(Candidate, kept.id)).status = "accepted"
        (await session.get(Candidate, lost.id)).status = "rejected"
        await session.commit()

    page = await client.get("/settings")
    assert page.status_code == 200
    assert "reddit:memes" in page.text
    assert "1/2" in page.text
    assert "50%" in page.text


async def test_rss_source(settings, session_factory):
    settings.crawler_sources = "rss:https://memes.example/feed.xml"
    feed = """<?xml version="1.0"?>
    <rss version="2.0"><channel><title>memy</title>
      <item>
        <title>świeży mem</title>
        <link>https://memes.example/post/1</link>
        <enclosure url="https://memes.example/img/1.png" type="image/png"/>
      </item>
    </channel></rss>"""
    png = make_png("orange", size=(300, 200))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("feed.xml"):
            return httpx.Response(200, text=feed)
        return httpx.Response(200, content=png)

    added = await crawl_once(
        session_factory, settings,
        transport=httpx.MockTransport(handler), today=TODAY,
    )
    assert added == 1
    async with session_factory() as session:
        cand = (await session.scalars(select(Candidate))).one()
        assert cand.title == "świeży mem"
        assert cand.source == "rss:memes.example"
        assert cand.page_url == "https://memes.example/post/1"


async def test_rss_with_junk_after_document(settings, session_factory):
    """Real feeds (demotywatory, mistrzowie) append an anti-bot <script>
    after </rss>; the parser must survive it."""
    settings.crawler_sources = "rss:https://memes.example/feed.xml"
    feed = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item>
        <title>mem</title>
        <enclosure url="https://memes.example/img/1.png" type="image/png"/>
      </item>
    </channel></rss><script>(function(){var junk=1;})()</script>"""
    png = make_png("brown", size=(300, 200))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("feed.xml"):
            return httpx.Response(200, text=feed)
        return httpx.Response(200, content=png)

    assert await crawl_once(
        session_factory, settings,
        transport=httpx.MockTransport(handler), today=TODAY,
    ) == 1


# --- inbox swipe endpoints ---------------------------------------------------


async def seed_candidate(session_factory, settings, color="red") -> Candidate:
    png = make_png(color, size=(320, 240))
    with Image.open(io.BytesIO(png)) as img:
        phash = dhash_image(img)
    thumb = f"cand-{color}.jpg"
    settings.candidates_dir.mkdir(parents=True, exist_ok=True)
    (settings.candidates_dir / thumb).write_bytes(png)
    async with session_factory() as session:
        cand = Candidate(
            source="reddit:memes",
            page_url=f"https://reddit.test/{color}",
            media_url=f"https://i.redd.it/{color}.png",
            title=f"meme {color}",
            score=42,
            phash=phash,
            thumb_filename=thumb,
            day=f"{__import__('datetime').datetime.now():%Y-%m-%d}",
        )
        session.add(cand)
        await session.commit()
        return cand


async def test_inbox_page_lists_candidates(client, settings, session_factory):
    await seed_candidate(session_factory, settings)
    page = await client.get("/inbox")
    assert page.status_code == 200
    assert "meme red" in page.text
    assert "swipe-card" in page.text


async def test_swipe_right_ingests_the_meme(
    client, settings, session_factory, search
):
    cand = await seed_candidate(session_factory, settings)
    full = make_png("red", size=(640, 480))
    inbox_module.TRANSPORT = httpx.MockTransport(
        lambda req: httpx.Response(200, content=full)
    )
    try:
        resp = await client.post(f"/ui/inbox/{cand.id}/accept")
    finally:
        inbox_module.TRANSPORT = None
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True

    async with session_factory() as session:
        item = await session.get(Item, body["item_id"])
        assert item is not None
        assert item.origin == "crawler"
        assert item.caption == "meme red"
        assert item.source_url == "https://reddit.test/red"
        assert item.index_status == "pending"  # queued for the AI models
        fresh = await session.get(Candidate, cand.id)
        assert fresh.status == "accepted"
        assert fresh.item_id == item.id


async def test_swipe_left_blacklists_the_meme(
    client, settings, session_factory
):
    cand = await seed_candidate(session_factory, settings, color="blue")
    resp = await client.post(f"/ui/inbox/{cand.id}/reject")
    assert resp.status_code == 200
    async with session_factory() as session:
        fresh = await session.get(Candidate, cand.id)
        assert fresh.status == "rejected"
        rejected = (await session.scalars(select(RejectedHash))).all()
        assert [r.phash for r in rejected] == [cand.phash]
    assert not (settings.candidates_dir / "cand-blue.jpg").exists()
    # double-swipe is a 404, not a second blacklist entry
    assert (await client.post(f"/ui/inbox/{cand.id}/reject")).status_code == 404


async def test_candidate_thumbs_are_served_and_traversal_blocked(
    client, settings, session_factory
):
    await seed_candidate(session_factory, settings)
    assert (await client.get("/candidates/cand-red.jpg")).status_code == 200
    assert (
        await client.get("/candidates/..%2F..%2Fmemehog.db")
    ).status_code in (403, 404)


async def test_submission_near_duplicate_is_rejected(
    settings, session_factory, search
):
    """Feeding the hog a re-encoded copy of a library meme bounces."""
    from conftest import write_png
    from memehog.core.submissions import create_submission
    from test_indexer import ingest_png

    await ingest_png(session_factory, settings, search, color="red")
    # same picture, different size → different sha, near-identical dHash
    copy = settings.tmp_dir / "copy.png"
    copy.write_bytes(make_png("red", size=(512, 384)))
    async with session_factory() as session:
        sub, reason = await create_submission(
            session, settings, copy, submitter_id=123
        )
    assert sub is None
    assert reason == "duplicate"

    # a genuinely new meme still goes through
    fresh = write_png(settings.tmp_dir / "fresh.png", color="teal")
    async with session_factory() as session:
        sub, reason = await create_submission(
            session, settings, fresh, submitter_id=123
        )
    assert reason == "ok"
    assert sub is not None
