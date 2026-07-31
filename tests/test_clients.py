from memehog.core import clients as clients_svc


async def test_owner_ids_from_env_are_allowed(settings, session_factory):
    settings.allowed_telegram_ids = "111,222"
    async with session_factory() as session:
        assert await clients_svc.is_allowed(session, settings, 111)
        assert not await clients_svc.is_allowed(session, settings, 999)


async def test_register_approve_flow(settings, session_factory):
    async with session_factory() as session:
        client, created = await clients_svc.request_access(session, 999, "newguy")
        assert created and client.status == "pending"
        # pending ≠ allowed
        assert not await clients_svc.is_allowed(session, settings, 999)

        # duplicate request doesn't create a second row
        _, created2 = await clients_svc.request_access(session, 999, "newguy")
        assert not created2

        await clients_svc.approve_client(session, 999)
        assert await clients_svc.is_allowed(session, settings, 999)

        await clients_svc.remove_client(session, 999)
        assert not await clients_svc.is_allowed(session, settings, 999)


async def test_manual_add_is_approved_immediately(settings, session_factory):
    async with session_factory() as session:
        await clients_svc.add_client(session, 555, note="brother")
        assert await clients_svc.is_allowed(session, settings, 555)


async def test_clients_api_roundtrip(client):
    from conftest import auth

    resp = await client.post(
        "/api/v1/clients", headers=auth(), json={"telegram_id": 777, "note": "test"}
    )
    assert resp.status_code == 201

    resp = await client.get("/api/v1/clients", headers=auth())
    ids = [c["telegram_id"] for c in resp.json()["clients"]]
    assert 777 in ids

    assert (
        await client.delete("/api/v1/clients/777", headers=auth())
    ).status_code == 204


async def test_settings_page_and_client_actions(client):
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Telegram clients" in resp.text
    assert "Runs daily at" in resp.text

    resp = await client.post(
        "/ui/clients", data={"telegram_id": "888", "note": "cousin"}
    )
    assert resp.status_code == 200
    assert "888" in resp.text and "approved" in resp.text

    resp = await client.post("/ui/clients/888/delete")
    assert "888" not in resp.text
