from conftest import write_png

from memehog.core import items as items_svc
from memehog.core import submissions as subs_svc
from memehog.core.library import ingest_file


async def _submit(settings, session, name, color, submitter_id=1000, caption=None):
    src = write_png(settings.tmp_dir / name, color=color)
    return await subs_svc.create_submission(
        session, settings, src,
        submitter_id=submitter_id, submitter_name="guest", caption=caption,
    )


async def test_submission_is_quarantined(settings, session_factory, search):
    async with session_factory() as session:
        sub, reason = await _submit(settings, session, "a.png", "red", caption="lol")
        assert reason == "ok" and sub is not None
        assert sub.status == "pending"
        # file sits in the pending dir, NOT in the library
        assert (settings.pending_dir / sub.filename).exists()
        assert await items_svc.count_items(session) == 0
        # source tmp file was consumed
        assert not (settings.tmp_dir / "a.png").exists()


async def test_duplicate_submissions_refused(settings, session_factory, search):
    async with session_factory() as session:
        # already in the library -> refused
        await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "lib.png", color="red"), origin="web",
        )
        sub, reason = await _submit(settings, session, "dup.png", "red")
        assert sub is None and reason == "duplicate"
        assert not (settings.tmp_dir / "dup.png").exists()

        # already pending (from someone else) -> refused too
        sub1, _ = await _submit(settings, session, "b1.png", "blue", submitter_id=1)
        sub2, reason = await _submit(settings, session, "b2.png", "blue", submitter_id=2)
        assert sub1 is not None and sub2 is None and reason == "duplicate"


async def test_pending_limit_per_user(settings, session_factory, search):
    async with session_factory() as session:
        colors = ["red", "green", "blue", "yellow"]
        results = []
        for i in range(subs_svc.MAX_PENDING_PER_USER + 1):
            results.append(
                await _submit(settings, session, f"p{i}.png", colors[i])
            )
        for sub, reason in results[:-1]:
            assert reason == "ok"
        assert results[-1] == (None, "too_many_pending")


async def test_daily_limit_per_user(settings, session_factory, search):
    async with session_factory() as session:
        for i in range(subs_svc.MAX_PER_DAY_PER_USER):
            sub, reason = await _submit(
                settings, session, f"d{i}.png", (i * 20 + 5, 0, 0)
            )
            assert reason == "ok"
            # decide immediately so the pending cap never kicks in
            await subs_svc.reject_submission(session, settings, sub)
        sub, reason = await _submit(settings, session, "over.png", "white")
        assert sub is None and reason == "daily_limit"


async def test_approve_moves_into_library(settings, session_factory, search):
    async with session_factory() as session:
        sub, _ = await _submit(settings, session, "ok.png", "cyan", caption="good one")
        pending_path = settings.pending_dir / sub.filename

        item = await subs_svc.approve_submission(session, settings, search, sub)
        assert item is not None
        assert sub.status == "approved" and sub.item_id == item.id
        assert not pending_path.exists()
        assert (settings.library_dir / item.filename).exists()
        assert item.origin == "telegram" and item.uploader == "guest"
        assert item.caption == "good one"


async def test_reject_deletes_file(settings, session_factory, search):
    async with session_factory() as session:
        sub, _ = await _submit(settings, session, "no.png", "magenta")
        pending_path = settings.pending_dir / sub.filename

        await subs_svc.reject_submission(session, settings, sub)
        assert sub.status == "rejected"
        assert not pending_path.exists()
        assert await items_svc.count_items(session) == 0


async def test_vote_msgs_roundtrip(settings, session_factory, search):
    async with session_factory() as session:
        sub, _ = await _submit(settings, session, "v.png", "orange")
        subs_svc.set_vote_msgs(sub, [(111, 5), (222, 6)])
        await session.commit()
        assert subs_svc.get_vote_msgs(sub) == [(111, 5), (222, 6)]


async def test_random_item_never_spicy(settings, session_factory, search):
    async with session_factory() as session:
        normal, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "n.png", color="red"), origin="web",
        )
        spicy, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / "s.png", color="blue"),
            origin="web", spicy=True,
        )
        for _ in range(10):
            pick = await items_svc.random_item(session)
            assert pick is not None and pick.id == normal.id
        # excluding the only candidate -> nothing to send
        assert await items_svc.random_item(session, exclude_id=normal.id) is None


def test_is_nsfw_text():
    from memehog.core.library import is_nsfw_text

    assert is_nsfw_text("ale NSFW mem")
    assert is_nsfw_text("nsfw")
    assert is_nsfw_text("Nsfw, uwaga")
    assert not is_nsfw_text("normalny mem")
    assert not is_nsfw_text("")
    assert not is_nsfw_text(None)


async def test_nsfw_caption_lands_in_spicy(settings, session_factory, search):
    async with session_factory() as session:
        sub, reason = await _submit(
            settings, session, "hot.png", "magenta", caption="troche NSFW"
        )
        assert reason == "ok"
        item = await subs_svc.approve_submission(session, settings, search, sub)
        assert item is not None
        assert item.filename.startswith("spicy/")
        assert "spicy" in {t.name for t in item.tags}
