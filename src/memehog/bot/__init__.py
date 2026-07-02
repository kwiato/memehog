from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Settings
from ..core import clients as clients_svc
from ..core import items as items_svc
from ..core.library import ingest_file
from ..core.queue import DownloadQueue
from ..db.models import Item, Job
from ..search.base import SearchBackend

log = logging.getLogger(__name__)

router = Router()

HELP_TEXT = (
    "Send me:\n"
    "• an Instagram / TikTok link\n"
    "• a direct link to an image or video\n"
    "• a photo, video or GIF straight from your gallery\n\n"
    "…and I'll save it to your meme library.\n\n"
    "Commands:\n"
    "/stats — library size\n"
    "/help — this message"
)

NOT_AUTHORIZED = (
    "⛔ You're not authorized to use this bot.\n"
    "Send /register to ask the owner for access."
)


def extract_urls(message: Message) -> list[str]:
    text = message.text or message.caption or ""
    urls: list[str] = []
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == "url":
            urls.append(entity.extract_from(text))
        elif entity.type == "text_link" and entity.url:
            urls.append(entity.url)
    return urls


def _sender(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "unknown"
    return user.username or user.full_name or str(user.id)


@router.message.outer_middleware()
async def whitelist_middleware(
    handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
    event: Message,
    data: dict[str, Any],
) -> Any:
    settings: Settings = data["settings"]
    session_factory = data["session_factory"]
    user = event.from_user
    if user is not None:
        async with session_factory() as session:
            allowed = await clients_svc.is_allowed(session, settings, user.id)
        if allowed:
            data["is_admin"] = user.id in settings.allowed_ids
            return await handler(event, data)

    text = (event.text or "").strip().lower()
    if user is not None and text.startswith("/register"):
        await _handle_register(event, data)
        return None
    log.info("Rejected message from non-whitelisted user %s", user.id if user else "?")
    await event.answer(NOT_AUTHORIZED)
    return None


async def _handle_register(message: Message, data: dict[str, Any]) -> None:
    settings: Settings = data["settings"]
    session_factory = data["session_factory"]
    bot: Bot = data["bot"]
    user = message.from_user
    assert user is not None

    async with session_factory() as session:
        client, created = await clients_svc.request_access(
            session, user.id, user.username or user.full_name
        )
    if not created:
        await message.answer("⏳ Your request is already waiting for the owner's decision.")
        return

    if not settings.allowed_ids:
        await message.answer(
            "🤷 There is no owner configured (ALLOWED_TELEGRAM_IDS is empty), "
            "so nobody can approve you."
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"reg:ok:{user.id}"),
                InlineKeyboardButton(text="❌ Reject", callback_data=f"reg:no:{user.id}"),
            ]
        ]
    )
    who = f"@{user.username}" if user.username else user.full_name
    for admin_id in settings.allowed_ids:
        try:
            await bot.send_message(
                admin_id,
                f"🔑 Access request: {who} (id {user.id}) wants to use Memehog.",
                reply_markup=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 - admin may not have started the bot
            log.warning("Couldn't notify admin %s: %s", admin_id, exc)
    await message.answer("📨 Request sent! You'll get a message when the owner decides.")


@router.callback_query(F.data.startswith("reg:"))
async def on_register_decision(
    callback: CallbackQuery,
    settings: Settings,
    session_factory,
    bot: Bot,
    **_: Any,
) -> None:
    if callback.from_user.id not in settings.allowed_ids:
        await callback.answer("Only the owner can decide.", show_alert=True)
        return
    _, action, raw_id = (callback.data or "").split(":")
    telegram_id = int(raw_id)

    async with session_factory() as session:
        client = await clients_svc.get_client(session, telegram_id)
        who = (client.username if client else None) or str(telegram_id)
        if action == "ok":
            await clients_svc.approve_client(session, telegram_id)
            verdict = f"✅ Approved {who}."
            user_note = "🎉 You're in! Send me a meme link to get started. /help"
        else:
            await clients_svc.remove_client(session, telegram_id)
            verdict = f"❌ Rejected {who}."
            user_note = "🚫 The owner rejected your access request."

    if callback.message is not None:
        try:
            await callback.message.edit_text(f"{callback.message.text}\n\n{verdict}")
        except Exception:  # noqa: BLE001
            pass
    try:
        await bot.send_message(telegram_id, user_note)
    except Exception as exc:  # noqa: BLE001
        log.warning("Couldn't notify user %s: %s", telegram_id, exc)
    await callback.answer(verdict)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer("🐗 *Memehog* at your service!\n\n" + HELP_TEXT, parse_mode="Markdown")


@router.message(Command("stats"))
async def cmd_stats(message: Message, session_factory, **_: Any) -> None:
    async with session_factory() as session:
        count = await items_svc.count_items(session)
    await message.answer(f"📚 Library contains {count} item(s).")


@router.message(F.photo | F.video | F.animation | F.document)
async def handle_media(
    message: Message,
    bot: Bot,
    settings: Settings,
    session_factory,
    search: SearchBackend,
    **_: Any,
) -> None:
    if message.photo:
        file_id, name = message.photo[-1].file_id, "photo.jpg"
    elif message.video:
        file_id = message.video.file_id
        name = message.video.file_name or "video.mp4"
    elif message.animation:
        file_id = message.animation.file_id
        name = message.animation.file_name or "animation.mp4"
    else:
        doc = message.document
        assert doc is not None
        if not (doc.mime_type or "").startswith(("image/", "video/")):
            await message.reply("🤷 I only accept images and videos.")
            return
        file_id, name = doc.file_id, doc.file_name or "file.bin"

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.tmp_dir / f"tg-{uuid.uuid4().hex[:8]}-{name}"
    try:
        await bot.download(file_id, destination=tmp_path)
    except Exception as exc:  # noqa: BLE001 - e.g. >20 MB bot API limit
        log.warning("Telegram download failed: %s", exc)
        await message.reply(f"❌ Couldn't download the file from Telegram: {exc}")
        return

    async with session_factory() as session:
        item, created = await ingest_file(
            session,
            settings,
            search,
            tmp_path,
            origin="telegram",
            caption=message.caption,
            uploader=_sender(message),
        )
    if created:
        await message.reply(f"✅ Saved to the library (#{item.id}).")
    else:
        await message.reply(f"♻️ Already in the library (#{item.id}).")


@router.message(F.text | F.caption)
async def handle_links(message: Message, queue: DownloadQueue, **_: Any) -> None:
    urls = extract_urls(message)
    if not urls:
        await message.reply("🤔 Send me a link or a media file. /help for details.")
        return

    for url in urls:
        status = await message.reply(f"⏳ Downloading…\n{url}")

        async def on_done(job: Job, saved: list[Item], _status: Message = status) -> None:
            if job.status == "done":
                text = f"✅ Saved {len(saved)} file(s) to the library."
            elif job.status == "duplicate":
                text = "♻️ Already in the library."
            else:
                text = f"❌ Download failed:\n{(job.error or 'unknown error')[:300]}"
            try:
                await _status.edit_text(text)
            except Exception:  # noqa: BLE001 - message may have been deleted
                log.debug("Could not edit status message for job %s", job.id)

        await queue.submit(url, origin="telegram", requested_by=_sender(message), callback=on_done)


async def run_bot(
    settings: Settings,
    session_factory,
    search: SearchBackend,
    queue: DownloadQueue,
) -> None:
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher(
        settings=settings,
        session_factory=session_factory,
        search=search,
        queue=queue,
    )
    dp.include_router(router)
    log.info("Starting Telegram bot (long polling)")
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        await bot.session.close()
