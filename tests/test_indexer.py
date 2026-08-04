import asyncio
import json

import httpx
import pytest
from conftest import write_png

from memehog.core import items as items_svc
from memehog.core.indexer import STATUS, run_indexing
from memehog.core.library import ingest_file
from memehog.core.queue import DownloadQueue
from memehog.web import create_app


@pytest.fixture(autouse=True)
def reset_indexer_status():
    STATUS.running = False
    STATUS.total = STATUS.processed = STATUS.indexed = 0
    STATUS.log.clear()
    yield


async def wait_for_run_end(timeout: float = 5.0) -> None:
    """Wait until the background indexer task has finished and logged."""
    for _ in range(int(timeout / 0.05)):
        if not STATUS.running and STATUS.log:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("indexer run did not finish in time")


def vlm_settings(settings, **overrides):
    settings.vlm_base_url = "https://vlm.test/v1"
    settings.vlm_api_key = "test-key"
    settings.vlm_model = "test-vision"
    settings.vlm_rpm = 0  # no throttling in tests
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def vlm_transport(reply: dict | str, status_code: int = 200) -> httpx.MockTransport:
    content = reply if isinstance(reply, str) else json.dumps(reply)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer test-key"
        body = {"choices": [{"message": {"content": content}}]}
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


async def ingest_png(
    session_factory, settings, search, name="meme.png", color="red", **kwargs
):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / name, color=color),
            origin="web", **kwargs,
        )
        return item


async def test_indexes_and_makes_searchable(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    reply = {
        "ocr_text": "BOBER KURWA",
        "description": "Zdziwiony bóbr patrzy w kamerę.",
    }
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 1

    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "indexed"
        # searchable both by OCR'd text and by description words
        for query in ("bober", "bóbr"):
            hits = await items_svc.list_items(session, search, q=query)
            assert [h.id for h in hits] == [item.id]


async def test_disabled_without_config(settings, session_factory, search):
    item = await ingest_png(session_factory, settings, search)
    assert await run_indexing(session_factory, settings, search) == 0
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "pending"


async def test_spicy_items_skipped_by_default(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        fresh.filename = f"spicy/{fresh.filename}"
        await session.commit()

    reply = {"ocr_text": "", "description": "spicy"}
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 0
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "pending"


async def test_literal_newlines_in_json_strings_are_tolerated(
    settings, session_factory, search
):
    """Some models emit raw newlines inside JSON strings (invalid JSON, but
    trivially recoverable) when transcribing multi-line memes."""
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    raw = '{"ocr_text": "linia pierwsza\nlinia druga", "description": "wieloliniowy mem"}'
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(raw)
    )
    assert indexed == 1
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="wieloliniowy")
        assert [h.id for h in hits] == [item.id]


async def test_broken_json_is_repaired(settings, session_factory, search):
    """Trailing commas and other almost-JSON from small models goes through
    the json-repair fallback instead of erroring the item out."""
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    broken = (
        '{\n'
        '  "ocr_text": "PONIEDZIAŁEK",\n'
        '  "description": "mem o poniedziałku",\n'   # trailing comma
        '}'
    )
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(broken)
    )
    assert indexed == 1
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="poniedziałek")
        assert [h.id for h in hits] == [item.id]


async def test_json_in_markdown_fences_is_tolerated(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    fenced = '```json\n{"ocr_text": "hello", "description": "a red square"}\n```'
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(fenced)
    )
    assert indexed == 1
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="red square")
        assert [h.id for h in hits] == [item.id]


async def test_saved_profile_drives_the_indexer(
    client, settings, session_factory, search
):
    """A model profile saved via the web UI is picked up by the indexer."""
    item = await ingest_png(session_factory, settings, search)

    resp = await client.post(
        "/ui/vlm/profiles",
        data={
            "name": "test-profile",
            "base_url": "https://vlm.test/v1",
            "model": "test-vision",
            "api_key": "test-key",
        },
    )
    assert resp.status_code == 200
    # a freshly added profile is active right away
    assert "test-profile" in resp.text
    assert "checked" in resp.text

    resp = await client.post(
        "/ui/settings/vlm",
        data={
            "language": "Polish",
            "rpm": "0",
            "max_per_run": "50",
            "index_spicy": "0",
        },
    )
    assert resp.status_code == 200
    assert 'value="Polish"' in resp.text

    # .env settings are empty — the profile alone must enable the indexer
    assert not settings.vlm_enabled
    reply = {"ocr_text": "", "description": "kot w kapeluszu"}
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 1
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="kapelusz")
        assert [h.id for h in hits] == [item.id]


async def test_multiple_active_models_and_search_filter(
    settings, session_factory, search
):
    """Two active models index independently; search can use one model's data."""
    from memehog.db.models import VlmProfile

    settings.vlm_rpm = 0
    item = await ingest_png(session_factory, settings, search)
    async with session_factory() as session:
        session.add(VlmProfile(
            name="model-a", base_url="https://x.test/v1", model="vision-a"
        ))
        session.add(VlmProfile(
            name="model-b", base_url="https://y.test/v1", model="vision-b"
        ))
        await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        desc = "smutna żaba" if model == "vision-a" else "zielona ropucha"
        content = json.dumps({"ocr_text": "", "description": desc})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    indexed = await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    )
    assert indexed == 2  # one item × two models

    async with session_factory() as session:
        # each model's text is searchable on its own...
        a = await items_svc.list_items(
            session, search, q="żaba", model_profile_id=1
        )
        assert [h.id for h in a] == [item.id]
        b = await items_svc.list_items(
            session, search, q="żaba", model_profile_id=2
        )
        assert b == []
        # ...and "all models" finds both wordings
        assert [h.id for h in await items_svc.list_items(
            session, search, q="ropucha"
        )] == [item.id]


def test_reasoning_model_think_block_is_stripped():
    """Reasoning models (qwen3.6 on groq...) emit a <think> monologue before
    the JSON — including stray {braces} that must not be mistaken for it."""
    from memehog.core.indexer import _parse_response

    content = (
        '<think>\nDraft: {"ocr_text": "wrong draft"} ...no, let me redo\n'
        "</think>\n"
        '{"ocr_text": "BOBER", "description": "bóbr", '
        '"tags": ["kot"], "lang": "pl"}'
    )
    ocr, description, tags, lang = _parse_response(content)
    assert (ocr, description, tags, lang) == ("BOBER", "bóbr", ["kot"], "pl")


async def test_untouched_memes_jump_the_queue(settings, session_factory, search):
    """A meme no model has described yet is processed before backfilling
    items that already carry some other model's data; a later run then
    fills in the gaps until coverage is complete."""
    from memehog.db.models import VlmProfile, VlmText
    from sqlalchemy import select

    settings.vlm_rpm = 0
    settings.vlm_max_per_run = 1
    item_a = await ingest_png(session_factory, settings, search, name="a.png")
    async with session_factory() as session:
        session.add(VlmProfile(
            name="model-a", base_url="https://x.test/v1",
            model="vision-a", api_key="test-key",
        ))
        await session.commit()
    reply = {"ocr_text": "", "description": "opis"}
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1

    # a second model arrives, plus a brand-new meme nobody has described
    item_b = await ingest_png(
        session_factory, settings, search, name="b.png", color="blue"
    )
    async with session_factory() as session:
        session.add(VlmProfile(
            name="model-b", base_url="https://y.test/v1",
            model="vision-b", api_key="test-key",
        ))
        await session.commit()

    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 2
    async with session_factory() as session:
        rows = (await session.scalars(select(VlmText))).all()
        covered = {(r.item_id, r.profile_id) for r in rows}
    # both models spent their one slot on the untouched meme — model-b did
    # not burn it backfilling the item model-a already described
    assert covered == {(item_a.id, 1), (item_b.id, 1), (item_b.id, 2)}

    # the next quiet run completes the set
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1
    async with session_factory() as session:
        rows = (await session.scalars(select(VlmText))).all()
        assert {(r.item_id, r.profile_id) for r in rows} == {
            (item_a.id, 1), (item_a.id, 2), (item_b.id, 1), (item_b.id, 2),
        }


async def test_one_model_failing_does_not_block_the_other(
    settings, session_factory, search
):
    from memehog.db.models import VlmProfile, VlmText
    from sqlalchemy import select

    settings.vlm_rpm = 0
    item = await ingest_png(session_factory, settings, search)
    async with session_factory() as session:
        session.add(VlmProfile(
            name="broken", base_url="https://x.test/v1", model="vision-a"
        ))
        session.add(VlmProfile(
            name="healthy", base_url="https://y.test/v1", model="vision-b"
        ))
        await session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model == "vision-a":
            return httpx.Response(401, json={"error": "bad key"})
        content = json.dumps({"ocr_text": "", "description": "działam"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    indexed = await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    )
    assert indexed == 1  # the healthy model got through
    async with session_factory() as session:
        rows = (await session.scalars(select(VlmText))).all()
        assert [(r.item_id, r.profile_id) for r in rows] == [(item.id, 2)]


async def test_profile_toggle(client):
    await client.post(
        "/ui/vlm/profiles",
        data={"name": "p1", "base_url": "https://x.test/v1",
              "model": "m", "api_key": ""},
    )
    resp = await client.post("/ui/vlm/profiles/1/toggle")
    assert resp.status_code == 200
    assert "checked" not in resp.text
    resp = await client.post("/ui/vlm/profiles/1/toggle")
    assert "checked" in resp.text


async def test_profile_edit_flow(client):
    await client.post(
        "/ui/vlm/profiles",
        data={"name": "router", "base_url": "https://openrouter.test/v1",
              "model": "openrouter/free", "api_key": "k1"},
    )
    resp = await client.get("/ui/vlm/profiles/1/edit")
    assert resp.status_code == 200
    assert 'value="router"' in resp.text
    assert 'value="openrouter/free"' in resp.text
    assert "Edit AI model" in resp.text

    resp = await client.post(
        "/ui/vlm/profiles/1",
        data={"name": "qwen-free", "base_url": "https://openrouter.test/v1",
              "model": "qwen/qwen3-vl:free", "api_key": "k1"},
    )
    assert resp.status_code == 200
    assert "qwen-free" in resp.text
    assert "qwen/qwen3-vl:free" in resp.text
    assert "router" not in resp.text.replace("openrouter.test", "")


async def test_deleting_active_profile_disables_indexing(
    client, settings, session_factory, search
):
    await client.post(
        "/ui/vlm/profiles",
        data={"name": "p1", "base_url": "https://vlm.test/v1",
              "model": "test-vision", "api_key": "k"},
    )
    resp = await client.post("/ui/vlm/profiles/1/delete")
    assert resp.status_code == 200
    assert "No models saved yet" in resp.text

    await ingest_png(session_factory, settings, search)
    reply = {"ocr_text": "", "description": "x"}
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 0  # active profile gone, .env empty → indexer disabled


async def test_interval_setting_saved(client, settings, session_factory):
    resp = await client.post(
        "/ui/settings/vlm",
        data={"language": "Polish", "rpm": "10", "max_per_run": "200",
              "interval": "30", "index_spicy": "0", "auto_tag": "1"},
    )
    assert resp.status_code == 200

    from memehog.core import appsettings

    async with session_factory() as session:
        effective = await appsettings.effective_settings(session, settings)
    assert effective.vlm_interval_minutes == 30


async def test_vlm_test_endpoint_requires_config(client):
    resp = await client.post(
        "/ui/settings/vlm/test", data={"base_url": "", "model": ""}
    )
    assert resp.status_code == 200
    assert "Fill in the endpoint and model" in resp.text


async def test_run_now_endpoint(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    reply = {"ocr_text": "", "description": "sowa z monoklem"}

    queue = DownloadQueue(session_factory, settings, search)
    app = create_app(settings, session_factory, search, queue)
    app.state.vlm_transport = vlm_transport(reply)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/ui/settings/vlm/run")
        assert resp.status_code == 200
        await wait_for_run_end()
        status_resp = await client.get("/ui/settings/vlm/status")
        assert "Done: 1/1" in status_resp.text
        assert "sowa z monoklem" in status_resp.text

    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "indexed"


async def test_run_now_without_config_logs_hint(client):
    resp = await client.post("/ui/settings/vlm/run")
    assert resp.status_code == 200
    await wait_for_run_end()
    status_resp = await client.get("/ui/settings/vlm/status")
    assert "No active models" in status_resp.text


async def test_one_bad_reply_does_not_kill_the_batch(
    settings, session_factory, search
):
    """A parse failure (with rollback) must not break processing later items."""
    vlm_settings(settings)
    good = await ingest_png(session_factory, settings, search, name="a.png")
    # different pixels → different sha, so both get ingested; the newer one
    # is processed first (newest-first queue) and hits the broken reply
    bad = await ingest_png(
        session_factory, settings, search, name="b.png", color="blue"
    )

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        content = (
            "not json at all" if calls["n"] == 1
            else json.dumps({"ocr_text": "", "description": "niebieski kwadrat"})
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    indexed = await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    )
    assert indexed == 1
    async with session_factory() as session:
        assert (await items_svc.get_item(session, bad.id)).index_status == "pending"
        assert (await items_svc.get_item(session, good.id)).index_status == "indexed"


async def test_auto_tagging(client, settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        await items_svc.add_tag(session, search, fresh, "bobr")  # user tag

    reply = {
        "ocr_text": "",
        "description": "bóbr w kapeluszu",
        # "Bobr" dedupes with the user tag, "spicy" is reserved, and the
        # per-item AI cap (4) trims the tail.
        "tags": ["Bobr", "zwierzęta", "spicy", "kapelusz", "natura", "las", "woda"],
    }
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 1

    async with session_factory() as session:
        ai = await items_svc.ai_tag_names(session, item.id)
        assert ai == {"zwierzęta", "kapelusz", "natura", "las"}
        # AI tags work in the gallery tag filter and in FTS
        hits = await items_svc.list_items(session, search, q="", tag="kapelusz")
        assert [h.id for h in hits] == [item.id]
        hits = await items_svc.list_items(session, search, q="natura")
        assert [h.id for h in hits] == [item.id]

    resp = await client.get(f"/ui/items/{item.id}/detail")
    assert "tag ai" in resp.text  # AI tags are visually marked


async def test_item_info_shows_per_model_data(
    client, settings, session_factory, search
):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    reply = {
        "ocr_text": "BOBER KURWA",
        "description": "Zdziwiony bóbr patrzy w kamerę.",
        "tags": ["bóbr"],
    }
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1

    resp = await client.get(f"/ui/items/{item.id}/info")
    assert resp.status_code == 200
    assert "test-vision" in resp.text          # profile name (from env shim)
    assert "Zdziwiony bóbr" in resp.text       # description, separately…
    assert "BOBER KURWA" in resp.text          # …from the OCR text
    assert "bóbr" in resp.text
    assert "bi-robot" in resp.text             # AI tag marked as such
    assert f"Meme #{item.id}" in resp.text


async def test_grid_shows_index_status_dot(client, settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    grid = await client.get("/ui/items", params={"page": 1})
    assert 'index-dot pending' in grid.text

    reply = {"ocr_text": "", "description": "opis"}
    await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    grid = await client.get("/ui/items", params={"page": 1})
    assert 'index-dot indexed' in grid.text
    assert f'data-id="{item.id}"' in grid.text


async def test_profile_health_badge_and_error_log(
    client, settings, session_factory, search
):
    from sqlalchemy import select

    from memehog.db.models import VlmError

    vlm_settings(settings)
    await ingest_png(session_factory, settings, search)

    # connection-class failure (bad key) → red badge + log entry
    await run_indexing(
        session_factory, settings, search,
        transport=vlm_transport({"error": "bad key"}, status_code=403),
    )
    async with session_factory() as session:
        errors = list(await session.scalars(select(VlmError)))
        assert len(errors) == 1
        assert errors[0].kind == "connection"
        assert "403" in errors[0].message

    page = await client.get("/settings?tab=ai")
    assert "health-btn error" in page.text

    log_resp = await client.get("/ui/vlm/profiles/1/errors")
    assert log_resp.status_code == 200
    assert "403" in log_resp.text
    assert "Error log" in log_resp.text


async def test_junk_response_records_response_error(
    settings, session_factory, search
):
    from sqlalchemy import select

    from memehog.db.models import VlmError

    vlm_settings(settings)
    await ingest_png(session_factory, settings, search)
    await run_indexing(
        session_factory, settings, search,
        transport=vlm_transport("User Safety: safe"),  # moderation junk
    )
    async with session_factory() as session:
        errors = list(await session.scalars(select(VlmError)))
        assert len(errors) == 1
        assert errors[0].kind == "response"
        assert "ValueError" in errors[0].message


async def test_manual_reindex_from_info(settings, session_factory, search):
    from sqlalchemy import select

    from memehog.db.models import VlmText

    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        desc = "stary opis" if calls["n"] == 1 else "nowy opis po re-run"
        content = json.dumps({"ocr_text": "", "description": desc})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    transport = httpx.MockTransport(handler)
    assert await run_indexing(
        session_factory, settings, search, transport=transport
    ) == 1

    queue = DownloadQueue(session_factory, settings, search)
    app = create_app(settings, session_factory, search, queue)
    app.state.vlm_transport = transport
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(f"/ui/items/{item.id}/reindex")
        assert resp.status_code == 200
        assert "nowy opis po re-run" in resp.text
        assert ": ok" in resp.text

    async with session_factory() as session:
        rows = list(
            await session.scalars(
                select(VlmText).where(VlmText.item_id == item.id)
            )
        )
        assert len(rows) == 1  # replaced, not stacked
        assert rows[0].description == "nowy opis po re-run"


async def test_language_detection_and_filter(
    client, settings, session_factory, search
):
    vlm_settings(settings)
    item_pl = await ingest_png(session_factory, settings, search, name="pl.png")
    item_en = await ingest_png(
        session_factory, settings, search, name="en.png", color="blue"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # newest-first queue: the first call is for item_en, then item_pl
        lang = "en" if handler.calls == 0 else "pl"
        handler.calls += 1
        content = json.dumps(
            {"ocr_text": "", "description": f"mem {lang}", "lang": lang}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )
    handler.calls = 0

    assert await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    ) == 2

    async with session_factory() as session:
        assert (await items_svc.get_item(session, item_pl.id)).lang == "pl"
        assert (await items_svc.get_item(session, item_en.id)).lang == "en"
        only_pl = await items_svc.list_items(session, search, lang="pl")
        assert [i.id for i in only_pl] == [item_pl.id]

    # the gallery filter dropdown lists both detected languages (SVG flags —
    # emoji flags don't render on Windows)
    page = await client.get("/")
    assert "/static/vendor/flags/pl.svg" in page.text
    assert "polski" in page.text
    assert "/static/vendor/flags/gb.svg" in page.text
    grid = await client.get("/ui/items", params={"page": 1, "lang": "en"})
    assert f'data-id="{item_en.id}"' in grid.text
    assert f'data-id="{item_pl.id}"' not in grid.text


async def test_info_language_dropdown_and_edit(
    client, settings, session_factory, search
):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    reply = {"ocr_text": "", "description": "opis", "lang": "pl"}
    await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )

    info = await client.get(f"/ui/items/{item.id}/info")
    assert 'value="pl" selected' in info.text

    resp = await client.post(f"/ui/items/{item.id}/lang", data={"lang": "en"})
    assert resp.status_code == 200
    assert 'value="en" selected' in resp.text
    async with session_factory() as session:
        assert (await items_svc.get_item(session, item.id)).lang == "en"

    resp = await client.post(f"/ui/items/{item.id}/lang", data={"lang": ""})
    async with session_factory() as session:
        assert (await items_svc.get_item(session, item.id)).lang is None


async def test_tags_as_comma_string_are_tolerated(
    settings, session_factory, search
):
    """pixtral-style: tags returned as one comma-separated string."""
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    reply = {"ocr_text": "", "description": "opis",
             "tags": "kot, praca; humor"}
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1
    async with session_factory() as session:
        ai = await items_svc.ai_tag_names(session, item.id)
        assert ai == {"kot", "praca", "humor"}


async def test_auto_tagging_can_be_disabled(settings, session_factory, search):
    vlm_settings(settings)
    settings.vlm_auto_tag = False
    item = await ingest_png(session_factory, settings, search)

    reply = {"ocr_text": "", "description": "x", "tags": ["kot", "pies"]}
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 1
    async with session_factory() as session:
        assert await items_svc.ai_tag_names(session, item.id) == set()


async def test_timeout_is_retried_like_transient(
    settings, session_factory, search, monkeypatch
):
    from memehog.core import indexer as indexer_mod

    monkeypatch.setattr(indexer_mod, "TRANSIENT_BACKOFF", (0, 0, 0))
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("")  # empty message, like real httpx timeouts
        reply = json.dumps({"ocr_text": "", "description": "po timeoucie działa"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": reply}}]}
        )

    indexed = await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    )
    assert indexed == 1
    assert calls["n"] == 2
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "indexed"


async def test_api_error_leaves_items_pending(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    indexed = await run_indexing(
        session_factory, settings, search,
        transport=vlm_transport({"error": "bad key"}, status_code=403),
    )
    assert indexed == 0
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "pending"


async def test_transient_503_is_retried(
    settings, session_factory, search, monkeypatch
):
    from memehog.core import indexer as indexer_mod

    monkeypatch.setattr(indexer_mod, "TRANSIENT_BACKOFF", (0, 0, 0))
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "high demand"})
        reply = json.dumps({"ocr_text": "", "description": "przetrwał 503"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": reply}}]}
        )

    indexed = await run_indexing(
        session_factory, settings, search, transport=httpx.MockTransport(handler)
    )
    assert indexed == 1
    assert calls["n"] == 2
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "indexed"
