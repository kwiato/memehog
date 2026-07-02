from conftest import auth, make_png


async def test_health_is_public(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_api_requires_token(client):
    assert (await client.get("/api/v1/items")).status_code == 401
    bad = await client.get(
        "/api/v1/items", headers={"Authorization": "Bearer wrong"}
    )
    assert bad.status_code == 401


async def test_upload_list_search_delete_roundtrip(client):
    resp = await client.post(
        "/api/v1/items",
        headers=auth(),
        files={"file": ("cat.png", make_png("red"), "image/png")},
        data={"caption": "sarcastic cat"},
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["created"] is True
    assert item["media_type"] == "image"
    item_id = item["id"]

    # duplicate upload → same item, created=False
    resp = await client.post(
        "/api/v1/items",
        headers=auth(),
        files={"file": ("cat2.png", make_png("red"), "image/png")},
    )
    assert resp.json()["created"] is False
    assert resp.json()["id"] == item_id

    # media + thumb served
    assert (await client.get(item["url"])).status_code == 200
    assert (await client.get(item["thumb_url"])).status_code == 200

    # search
    resp = await client.get("/api/v1/items", headers=auth(), params={"q": "sarcastic"})
    assert [i["id"] for i in resp.json()["items"]] == [item_id]

    # tags
    resp = await client.post(
        f"/api/v1/items/{item_id}/tags", headers=auth(), json={"name": "cats"}
    )
    assert resp.json()["tags"] == ["cats"]

    # delete
    assert (
        await client.delete(f"/api/v1/items/{item_id}", headers=auth())
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/items/{item_id}", headers=auth())
    ).status_code == 404


async def test_ui_pages_render(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Memehog" in resp.text

    resp = await client.get("/ui/items", params={"page": 1})
    assert resp.status_code == 200


async def test_job_submit_and_status(client):
    resp = await client.post(
        "/api/v1/jobs", headers=auth(), json={"url": "https://example.com/x.jpg"}
    )
    assert resp.status_code == 202
    job = resp.json()
    assert job["status"] == "pending"

    resp = await client.get(f"/api/v1/jobs/{job['id']}", headers=auth())
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://example.com/x.jpg"


async def test_ui_upload_reports_duplicates(client):
    def post(name, color):
        return client.post(
            "/ui/upload",
            files={"files": (name, make_png(color), "image/png")},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

    assert (await post("a.png", "red")).json() == {
        "added": 1, "duplicates": 0, "queued": 0,
    }
    # same bytes under a different name -> duplicate, not stored twice
    assert (await post("b.png", "red")).json() == {
        "added": 0, "duplicates": 1, "queued": 0,
    }
    grid = await client.get("/ui/items", params={"page": 1})
    assert grid.text.count('class="card"') == 1
