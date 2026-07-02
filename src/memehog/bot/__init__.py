from __future__ import annotations

import logging
import uuid
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Settings
from ..core import clients as clients_svc
from ..core import items as items_svc
from ..core import submissions as subs_svc
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

GUEST_HINT = (
    "👋 You're not on the guest list, but you can still contribute!\n"
    "Send me a meme (photo, video or GIF — as a file, links don't work "
    "for guests) and the owner will vote on it. If it gets in, you'll "
    "receive a random meme from the library as a thank-you 🐗\n\n"
    "Want full access? Send /register."
)

GUEST_REPLIES = {
    "too_many_pending": (
        "⏳ You already have a few memes waiting for a vote. "
        "Let the owner catch up first!"
    ),
    "daily_limit": "🛑 Daily submission limit reached — try again tomorrow.",
    "duplicate": "♻️ We already have that one (or it's already in the queue). Try another!",
}


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


def _media_file(message: Message) -> tuple[str, str] | None:
    """(file_id, filename) of the message's media, or None if unsupported."""
    if message.photo:
        return message.photo[-1].file_id, "photo.jpg"
    if message.video:
        return message.video.file_id, message.video.file_name or "video.mp4"
    if message.animation:
        return message.animation.file_id, message.animation.file_name or "animation.mp4"
    doc = message.document
    if doc is not None and (doc.mime_type or "").startswith(("image/", "video/")):
        return doc.file_id, doc.file_name or "file.bin"
    return None


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

    if user is None:
        return None

    text = (event.text or "").strip().lower()
    if text.startswith("/register"):
        await _handle_register(event, data)
        return None
    # Guests can submit media files for moderation — but never links
    # (a stranger must not be able to make this box download things).
    if event.photo or event.video or event.animation or event.document:
        await _handle_guest_media(event, data)
        return None
    log.info("Guest message from user %s — sent the hint", user.id)
    await event.answer(GUEST_HINT)
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


# --- guest submissions (moderated by owner votes) ----------------------------


async def _handle_guest_media(message: Message, data: dict[str, Any]) -> None:
    settings: Settings = data["settings"]
    session_factory = data["session_factory"]
    bot: Bot = data["bot"]
    user = message.from_user
    assert user is not None

    media = _media_file(message)
    if media is None:
        await message.reply("🤷 I only accept images and videos.")
        return
    file_id, name = media

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = settings.tmp_dir / f"sub-{uuid.uuid4().hex[:8]}-{name}"
    try:
        await bot.download(file_id, destination=tmp_path)
    except Exception as exc:  # noqa: BLE001 - e.g. >20 MB bot API limit
        log.warning("Guest submission download failed: %s", exc)
        await message.reply("❌ Couldn't download the file from Telegram (20 MB max).")
        return

    async with session_factory() as session:
        submission, reason = await subs_svc.create_submission(
            session, settings, tmp_path,
            submitter_id=user.id,
            submitter_name=user.username or user.full_name,
            caption=message.caption,
        )
    if submission is None:
        await message.reply(GUEST_REPLIES[reason])
        return

    if not settings.allowed_ids:
        await message.reply(
            "🤷 There is no owner configured (ALLOWED_TELEGRAM_IDS is empty), "
            "so nobody can vote on your meme."
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍", callback_data=f"sub:ok:{submission.id}"),
                InlineKeyboardButton(text="👎", callback_data=f"sub:no:{submission.id}"),
            ]
        ]
    )
    who = f"@{user.username}" if user.username else user.full_name
    header = f"🗳 Meme submission #{submission.id} from {who}"
    if message.caption:
        header += f":\n{message.caption}"
    refs: list[tuple[int, int]] = []
    for admin_id in settings.allowed_ids:
        try:
            copied = await bot.copy_message(
                chat_id=admin_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=header,
                reply_markup=keyboard,
            )
            refs.append((admin_id, copied.message_id))
        except Exception as exc:  # noqa: BLE001 - admin may not have started the bot
            log.warning("Couldn't send submission %s to admin %s: %s",
                        submission.id, admin_id, exc)
    if refs:
        async with session_factory() as session:
            sub = await subs_svc.get_submission(session, submission.id)
            if sub is not None:
                subs_svc.set_vote_msgs(sub, refs)
                await session.commit()
    await message.reply(
        "📨 Thanks! Your meme is waiting for the owner's vote — "
        "if it gets in, you'll receive a random meme back 🐗"
    )


async def _send_item(
    bot: Bot, chat_id: int, settings: Settings, item: Item, caption: str
) -> None:
    file = FSInputFile(settings.library_dir / item.filename)
    if item.media_type == "video":
        await bot.send_video(chat_id, file, caption=caption)
    elif item.media_type == "animation":
        await bot.send_animation(chat_id, file, caption=caption)
    else:
        await bot.send_photo(chat_id, file, caption=caption)


@router.callback_query(F.data.startswith("sub:"))
async def on_submission_vote(
    callback: CallbackQuery,
    settings: Settings,
    session_factory,
    search: SearchBackend,
    bot: Bot,
    **_: Any,
) -> None:
    if callback.from_user.id not in settings.allowed_ids:
        await callback.answer("Only the owner can vote.", show_alert=True)
        return
    _, action, raw_id = (callback.data or "").split(":")

    async with session_factory() as session:
        submission = await subs_svc.get_submission(session, int(raw_id))
        if submission is None or submission.status != "pending":
            await callback.answer("Already decided.")
            return
        who = submission.submitter_name or str(submission.submitter_id)
        reward = None
        if action == "ok":
            item = await subs_svc.approve_submission(session, settings, search, submission)
            verdict = f"👍 Accepted submission #{submission.id} from {who}."
            # Random thank-you meme — never spicy, never the one they just sent.
            reward = await items_svc.random_item(
                session, spicy=False, exclude_id=item.id if item else None
            )
        else:
            await subs_svc.reject_submission(session, settings, submission)
            verdict = f"👎 Rejected submission #{submission.id} from {who}."
        refs = subs_svc.get_vote_msgs(submission)
        submitter_id = submission.submitter_id

    for chat_id, msg_id in refs:
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id, caption=verdict, reply_markup=None
            )
        except Exception:  # noqa: BLE001 - message may have been deleted
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=msg_id, reply_markup=None
                )
            except Exception:  # noqa: BLE001
                pass

    try:
        if action == "ok":
            await bot.send_message(
                submitter_id, "🎉 Your meme made it into the library! Here's one for you:"
            )
            if reward is not None:
                await _send_item(
                    bot, submitter_id, settings, reward, "Random pick from the stash 🐗"
                )
        else:
            await bot.send_message(
                submitter_id,
                "🚫 The owner passed on your meme this time. Feel free to try another!",
            )
    except Exception as exc:  # noqa: BLE001 - user may have blocked the bot
        log.warning("Couldn't notify submitter %s: %s", submitter_id, exc)
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
    media = _media_file(message)
    if media is None:
        await message.reply("🤷 I only accept images and videos.")
        return
    file_id, name = media

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
