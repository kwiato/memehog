import json

import httpx
from sqlalchemy import select
from test_indexer import ingest_png, vlm_settings, vlm_transport

from memehog.core import items as items_svc
from memehog.core.indexer import run_indexing
from memehog.db.models import VlmText


async def _indexed_item(client, settings, session_factory, search):
    """One meme fully indexed (env-shim profile) with a tag."""
    vlm_settings(settings)
    item = await ingest_png(session_factory, settings, search)
    reply = {"ocr_text": "", "description": "opis", "tags": ["kot"]}
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1
    return item


async def test_memes_tab_shows_coverage_and_tags(
    client, settings, session_factory, search
):
    item = await _indexed_item(client, settings, session_factory, search)
    # a second, unindexed meme
    other = await ingest_png(
        session_factory, settings, search, name="b.png", color="blue"
    )

    page = await client.get("/settings?tab=memes")
    assert page.status_code == 200
    assert f'name="ids" value="{item.id}"' in page.text
    assert "ok-check" in page.text          # coverage tick for the indexed one
    assert "kot" in page.text               # its tag

    # "missing" filter shows only the unindexed meme
    page = await client.get("/settings?tab=memes&mfilter=missing")
    assert f'name="ids" value="{other.id}"' in page.text
    assert f'name="ids" value="{item.id}"' not in page.text

    # "notags" filter shows only the untagged meme
    page = await client.get("/settings?tab=memes&mfilter=notags")
    assert f'name="ids" value="{other.id}"' in page.text
    assert f'name="ids" value="{item.id}"' not in page.text


async def test_reindex_selected_requeues(
    client, settings, session_factory, search
):
    item = await _indexed_item(client, settings, session_factory, search)
    async with session_factory() as session:
        assert len(list(await session.scalars(select(VlmText)))) == 1

    resp = await client.post(
        "/ui/memes/reindex",
        data={"ids": [str(item.id)], "mpage": "1", "mfilter": ""},
    )
    assert resp.status_code == 200
    assert "queued" in resp.text

    async with session_factory() as session:
        # model outputs dropped -> back in every active model's queue
        assert list(await session.scalars(select(VlmText))) == []
        # search still works off the retained FTS copy in the meantime
        hits = await items_svc.list_items(session, search, q="opis")
        assert [h.id for h in hits] == [item.id]

    # the next run re-processes it
    reply = {"ocr_text": "", "description": "świeży opis", "tags": []}
    assert await run_indexing(
        session_factory, settings, search, transport=vlm_transport(reply)
    ) == 1
    async with session_factory() as session:
        rows = list(await session.scalars(select(VlmText)))
        assert len(rows) == 1
        assert rows[0].description == "świeży opis"
