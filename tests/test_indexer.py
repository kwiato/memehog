import json

import httpx
from conftest import write_png

from memehog.core import items as items_svc
from memehog.core.indexer import run_indexing
from memehog.core.library import ingest_file


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


async def ingest_png(session_factory, settings, search, name="meme.png", **kwargs):
    async with session_factory() as session:
        item, _ = await ingest_file(
            session, settings, search,
            write_png(settings.tmp_dir / name),
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


async def test_web_settings_override_env(
    client, settings, session_factory, search
):
    """VLM configured via the web UI (not .env) is picked up by the indexer."""
    item = await ingest_png(session_factory, settings, search)

    resp = await client.post(
        "/ui/settings/vlm",
        data={
            "base_url": "https://vlm.test/v1",
            "api_key": "test-key",
            "model": "test-vision",
            "language": "Polish",
            "rpm": "0",
            "max_per_run": "50",
            "index_spicy": "0",
        },
    )
    assert resp.status_code == 200
    # the re-rendered modal shows the saved values
    assert 'value="test-vision"' in resp.text
    assert 'value="Polish"' in resp.text

    # .env settings are empty — overrides alone must enable the indexer
    assert not settings.vlm_enabled
    reply = {"ocr_text": "", "description": "kot w kapeluszu"}
    indexed = await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    )
    assert indexed == 1
    async with session_factory() as session:
        hits = await items_svc.list_items(session, search, q="kapelusz")
        assert [h.id for h in hits] == [item.id]


async def test_vlm_test_endpoint_requires_config(client):
    resp = await client.post(
        "/ui/settings/vlm/test", data={"base_url": "", "model": ""}
    )
    assert resp.status_code == 200
    assert "Fill in the endpoint and model" in resp.text


async def test_api_error_leaves_items_pending(settings, session_factory, search):
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)

    indexed = await run_indexing(
        session_factory, settings, search,
        transport=vlm_transport({"error": "quota"}, status_code=429),
    )
    assert indexed == 0
    async with session_factory() as session:
        fresh = await items_svc.get_item(session, item.id)
        assert fresh.index_status == "pending"
