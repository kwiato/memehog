from memehog import __version__


async def test_about_shows_version_and_build(client):
    resp = await client.get("/ui/about")
    assert resp.status_code == 200
    assert __version__ in resp.text
    assert "<code>dev</code>" in resp.text


async def test_settings_footer_shows_build(client):
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert f"Memehog v{__version__}" in resp.text
    assert "<code>dev</code>" in resp.text


async def test_settings_ai_tab_renders(client):
    resp = await client.get("/settings?tab=ai")
    assert resp.status_code == 200
    assert "Saved models" in resp.text
    assert "Run benchmark" in resp.text
