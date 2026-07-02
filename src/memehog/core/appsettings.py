from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AppSetting

SCAN_CRON_KEY = "scan_cron"
NIGHTLY_JOB_ID = "nightly"


async def get_setting(
    session: AsyncSession, key: str, default: str = ""
) -> str:
    row = await session.get(AppSetting, key)
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await session.commit()
