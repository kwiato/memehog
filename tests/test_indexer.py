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
    # first saved profile becomes active automatically
    assert "test-profile" in resp.text
    assert "active" in resp.text

    resp = await client.post(
        "/ui/settings/vlm",
        data={
            "profile_id": "1",
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
    assert "Not configured" in status_resp.text


async def test_one_bad_reply_does_not_kill_the_batch(
    settings, session_factory, search
):
    """A parse failure (with rollback) must not break processing later items."""
    vlm_settings(settings)
    bad = await ingest_png(session_factory, settings, search, name="a.png")
    # different pixels → different sha, so both get ingested
    good = await ingest_png(
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
