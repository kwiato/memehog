import asyncio
import json

import httpx
import pytest
from sqlalchemy import select
from test_indexer import ingest_png

from memehog.core import appsettings
from memehog.core.bench import BENCH_CONFIGS_KEY, BENCH_STATUS, run_benchmark
from memehog.core.queue import DownloadQueue
from memehog.db.models import VlmSample
from memehog.web import create_app


@pytest.fixture(autouse=True)
def reset_bench_status():
    BENCH_STATUS.running = False
    BENCH_STATUS.total = BENCH_STATUS.processed = BENCH_STATUS.indexed = 0
    BENCH_STATUS.log.clear()
    yield


def multi_model_transport() -> httpx.MockTransport:
    """Replies with a description that names the model that was asked."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = json.dumps(
            {"ocr_text": "", "description": f"opis od {payload['model']}"}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}]}
        )

    return httpx.MockTransport(handler)


async def wait_for_bench_end(timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if not BENCH_STATUS.running and BENCH_STATUS.log:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("benchmark did not finish in time")


CONFIGS = [
    {"label": "model-a", "base_url": "https://x.test/v1",
     "model": "vision-a", "api_key": ""},
    {"label": "model-b", "base_url": "https://y.test/v1",
     "model": "vision-b", "api_key": ""},
]


async def test_benchmark_stores_side_by_side(settings, session_factory, search):
    settings.vlm_rpm = 0
    await ingest_png(session_factory, settings, search, name="a.png")
    await ingest_png(session_factory, settings, search, name="b.png", color="blue")
    async with session_factory() as session:
        await appsettings.set_setting(
            session, BENCH_CONFIGS_KEY, json.dumps(CONFIGS)
        )

    done = await run_benchmark(
        session_factory, settings, sample_size=10,
        transport=multi_model_transport(),
    )
    assert done == 4  # 2 models x 2 memes

    async with session_factory() as session:
        samples = (await session.scalars(select(VlmSample))).all()
    assert len(samples) == 4
    assert {s.model_label for s in samples} == {"model-a", "model-b"}
    assert all(s.error is None for s in samples)
    assert any("vision-a" in s.description for s in samples)
    assert any("vision-b" in s.description for s in samples)


async def test_benchmark_without_configs_notes_it(settings, session_factory, search):
    settings.vlm_rpm = 0
    done = await run_benchmark(session_factory, settings, sample_size=5)
    assert done == 0
    assert any("No benchmark models" in line for line in BENCH_STATUS.log)


async def test_bench_ui_flow(settings, session_factory, search):
    settings.vlm_rpm = 0
    await ingest_png(session_factory, settings, search, name="a.png")

    queue = DownloadQueue(session_factory, settings, search)
    app = create_app(settings, session_factory, search, queue)
    app.state.vlm_transport = multi_model_transport()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/ui/settings/vlm/bench",
            data={
                "sample_size": "5",
                "bench_label": ["A", "B"],
                "bench_url": ["https://x.test/v1", "https://y.test/v1"],
                "bench_model": ["vision-a", "vision-b"],
                "bench_key": ["", ""],
            },
        )
        assert resp.status_code == 200
        await wait_for_bench_end()

        results = await client.get("/ui/vlm/bench")
        assert results.status_code == 200
        assert "opis od vision-a" in results.text
        assert "opis od vision-b" in results.text
        # summary table lists both labels
        assert "<strong>A</strong>" in results.text
        assert "<strong>B</strong>" in results.text
