from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import TelegramClient


async def list_clients(session: AsyncSession) -> list[TelegramClient]:
    return list(
        (
            await session.scalars(
                select(TelegramClient).order_by(
                    TelegramClient.status.desc(), TelegramClient.created_at
                )
            )
        ).all()
    )


async def get_client(
    session: AsyncSession, telegram_id: int
) -> TelegramClient | None:
    return await session.scalar(
        select(TelegramClient).where(TelegramClient.telegram_id == telegram_id)
    )


async def is_allowed(
    session: AsyncSession, settings: Settings, telegram_id: int
) -> bool:
    if telegram_id in settings.allowed_ids:
        return True
    client = await get_client(session, telegram_id)
    return client is not None and client.status == "approved"


async def add_client(
    session: AsyncSession,
    telegram_id: int,
    *,
    username: str | None = None,
    note: str | None = None,
    status: str = "approved",
) -> TelegramClient:
    client = await get_client(session, telegram_id)
    if client is None:
        client = TelegramClient(
            telegram_id=telegram_id, username=username, note=note, status=status
        )
        session.add(client)
    else:
        client.username = username or client.username
        client.note = note or client.note
        if status == "approved":
            client.status = "approved"
    await session.commit()
    return client


async def request_access(
    session: AsyncSession, telegram_id: int, username: str | None
) -> tuple[TelegramClient, bool]:
    """Create a pending request. Returns (client, created)."""
    client = await get_client(session, telegram_id)
    if client is not None:
        return client, False
    client = TelegramClient(
        telegram_id=telegram_id, username=username, status="pending"
    )
    session.add(client)
    await session.commit()
    return client, True


async def approve_client(
    session: AsyncSession, telegram_id: int
) -> TelegramClient | None:
    client = await get_client(session, telegram_id)
    if client is not None:
        client.status = "approved"
        await session.commit()
    return client


async def remove_client(session: AsyncSession, telegram_id: int) -> None:
    await session.execute(
        sa_delete(TelegramClient).where(TelegramClient.telegram_id == telegram_id)
    )
    await session.commit()
