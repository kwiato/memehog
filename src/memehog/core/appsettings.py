from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import AppSetting, VlmProfile

SCAN_CRON_KEY = "scan_cron"
NIGHTLY_JOB_ID = "nightly"

# app_settings key holding the id of the active VlmProfile (production model).
VLM_PROFILE_KEY = "vlm_profile_id"

# Settings fields that the web UI can override; stored one row per field in
# app_settings under the field name. A stored row wins over the .env value.
VLM_FIELDS: tuple[str, ...] = (
    "vlm_base_url",
    "vlm_api_key",
    "vlm_model",
    "vlm_language",
    "vlm_rpm",
    "vlm_max_per_run",
    "vlm_index_spicy",
)


def _parse_vlm(field: str, value: str):
    if field == "vlm_rpm":
        return float(value)
    if field == "vlm_max_per_run":
        return int(value)
    if field == "vlm_index_spicy":
        return value.strip().lower() in ("1", "true", "on", "yes")
    return value.strip()


async def active_vlm_profile(session: AsyncSession) -> VlmProfile | None:
    row = await session.get(AppSetting, VLM_PROFILE_KEY)
    if row is None or not row.value.strip().isdigit():
        return None
    return await session.get(VlmProfile, int(row.value))


async def effective_settings(session: AsyncSession, settings: Settings) -> Settings:
    """A copy of `settings` with web-UI overrides applied on top of .env.

    The selected VLM profile (if any) wins over both the .env values and the
    legacy per-field overrides.
    """
    updates = {}
    for field in VLM_FIELDS:
        row = await session.get(AppSetting, field)
        if row is None:
            continue
        try:
            updates[field] = _parse_vlm(field, row.value)
        except ValueError:
            pass
    profile = await active_vlm_profile(session)
    if profile is not None:
        updates["vlm_base_url"] = profile.base_url
        updates["vlm_api_key"] = profile.api_key
        updates["vlm_model"] = profile.model
    return settings.model_copy(update=updates) if updates else settings


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
