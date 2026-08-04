from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import AppSetting, VlmProfile

SCAN_CRON_KEY = "scan_cron"
NIGHTLY_JOB_ID = "nightly"
VLM_INTERVAL_JOB_ID = "vlm-interval"

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
    "vlm_auto_tag",
    "vlm_interval_minutes",
)

CRAWLER_FIELDS: tuple[str, ...] = (
    "crawler_sources",
    "crawler_daily_target",
    "crawler_hour",
)


def _parse_vlm(field: str, value: str):
    if field == "vlm_rpm":
        return float(value)
    if field in ("vlm_max_per_run", "vlm_interval_minutes",
                 "crawler_daily_target", "crawler_hour"):
        return int(value)
    if field in ("vlm_index_spicy", "vlm_auto_tag"):
        return value.strip().lower() in ("1", "true", "on", "yes")
    if field == "crawler_sources":
        return value  # multi-line, keep verbatim
    return value.strip()


async def effective_settings(session: AsyncSession, settings: Settings) -> Settings:
    """A copy of `settings` with web-UI overrides applied on top of .env.

    Model connections themselves live in `vlm_profiles` — this only covers
    the indexer knobs (language, rpm, limits) plus the legacy .env fallback
    fields used to bootstrap the first profile.
    """
    updates = {}
    for field in VLM_FIELDS + CRAWLER_FIELDS:
        row = await session.get(AppSetting, field)
        if row is None:
            continue
        try:
            updates[field] = _parse_vlm(field, row.value)
        except ValueError:
            pass
    return settings.model_copy(update=updates) if updates else settings


async def ensure_profile_from_env(session: AsyncSession, settings: Settings) -> None:
    """Bootstrap: turn a .env-only VLM config into the first saved profile,
    so headless installs get indexing without ever opening the settings UI."""
    if await session.scalar(select(VlmProfile.id).limit(1)) is not None:
        return
    effective = await effective_settings(session, settings)
    if not effective.vlm_enabled:
        return
    session.add(
        VlmProfile(
            name=effective.vlm_model,
            base_url=effective.vlm_base_url,
            api_key=effective.vlm_api_key,
            model=effective.vlm_model,
            active=True,
        )
    )
    await session.commit()


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
