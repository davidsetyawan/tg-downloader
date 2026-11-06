from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler

from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from cleanup import delete, stale_dirs
from config import Settings, load_settings
from dataclasses import dataclass

from downloader import DownloadError, Downloader, ErrorKind, post_info
from linkparser import Kind, ParsedLink, has_url, parse_message, profile_url
from sender import ChatActionKeeper, build_post_caption, profile_link_html, send_items

log = logging.getLogger("bot")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

HELP = (
    "Send a Twitter/X or Instagram link:\n"
    "- post / reel / single story: media + caption + author + link\n"
    "- profile / stories / highlight: all media of that account, no item limit.\n"
    "  Sent in albums of 10 as they download. Username link on the first album.\n\n"
    "/cancel stops your running downloads.\n/id shows your Telegram ID."
)

ERROR_TEXT = {
    ErrorKind.AUTH: "Login/cookies for {platform} are missing or expired. The admin has been notified.",
    ErrorKind.NOT_FOUND: "Not found. The post/profile was deleted or the link is wrong.",
    ErrorKind.PRIVATE: "That account is private or the content is restricted.",
    ErrorKind.RATE_LIMIT: "{platform} is rate limiting the bot. Try again in a few minutes.",
    ErrorKind.TIMEOUT: "Timed out while downloading.",
    ErrorKind.EMPTY: "No media found in that link.",
    ErrorKind.NO_STORIES: "No active stories right now.",
    ErrorKind.CANCELLED: "Cancelled.",
    ErrorKind.OTHER: "Download failed. Details are in the log.",
}


# ---------- handlers ----------

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"Your Telegram ID: {update.effective_user.id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP, link_preview_options=NO_PREVIEW)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = context.bot_data["downloader"].cancel(update.effective_chat.id)
    await update.effective_message.reply_text(f"Cancelling {n} job(s)." if n else "Nothing running.")


async def on_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        f"Not authorized. Ask the admin to add your ID: {update.effective_user.id}"
    )


async def _alert_admin(context: ContextTypes.DEFAULT_TYPE, link: ParsedLink, detail: str) -> None:
    s: Settings = context.bot_data["settings"]
    last = context.bot_data.get("last_auth_alert", 0.0)
    if s.admin_id is None or time.time() - last < 3600:
        return
    context.bot_data["last_auth_alert"] = time.time()
    text = f"{link.platform.value} auth problem, refresh cookies/{link.platform.value}.txt\n\n{detail[-400:]}"
    try:
        await context.bot.send_message(s.admin_id, text)
    except Exception:
        log.exception("could not alert admin")


async def deliver(bot, chat_id: int, link: ParsedLink, result, keeper, job, s: Settings) -> None:
    """Single post: everything is already on disk."""
    username, caption, post_url = post_info(result.items[0].meta, link)
    profile = profile_url(link.platform, username)
    cap_html, overflow = build_post_caption(caption, username, profile, post_url)

    stats = await send_items(bot, chat_id, result.items, cap_html, "first", keeper, job, s.max_upload_bytes)

    if overflow and stats.sent and not job.cancelled:
        await bot.send_message(chat_id, overflow, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
    notes = _notes(stats.skipped, result.warning, s)
    if notes and not job.cancelled:
        await bot.send_message(chat_id, f"{notes}\n{post_url}", link_preview_options=NO_PREVIEW)


@dataclass
class _StreamState:
    captioned: bool = False
    skipped: int = 0


async def deliver_stream(bot, chat_id: int, link: ParsedLink, keeper, job, s: Settings, dl: Downloader) -> None:
    """Profile / stories / highlight: send every 10 files as they are downloaded.
    The username link goes on the first album only."""
    state = _StreamState()

    async def on_batch(batch) -> None:
        cap = None
        if not state.captioned:
            username, _, _ = post_info(batch[0].meta, link)
            cap = profile_link_html(username, profile_url(link.platform, username))
        stats = await send_items(bot, chat_id, batch, cap, "first", keeper, job, s.max_upload_bytes)
        state.skipped += stats.skipped
        if stats.sent:
            state.captioned = True

    result = await dl.stream(link, job, on_batch)
    notes = _notes(state.skipped, result.warning, s)
    if notes and not job.cancelled:
        await bot.send_message(chat_id, notes, link_preview_options=NO_PREVIEW)


def _notes(skipped: int, warning: str | None, s: Settings) -> str:
    notes = []
    if skipped:
        notes.append(f"{skipped} file(s) over the {s.max_upload_bytes // (1024 * 1024)} MB upload limit were skipped.")
    if warning:
        notes.append(warning)
    return " ".join(notes)


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    text = msg.text or ""
    link = parse_message(text)
    if link is None:
        if has_url(text):
            await msg.reply_text("Unsupported link. Send a Twitter/X or Instagram post, reel, profile or story link.")
        return

    s: Settings = context.bot_data["settings"]
    dl: Downloader = context.bot_data["downloader"]
    chat_id = msg.chat_id
    job = dl.new_job(chat_id)
    try:
        async with ChatActionKeeper(context.bot, chat_id) as keeper:
            if link.kind is Kind.POST:
                async with dl.download(link, job) as result:
                    await deliver(context.bot, chat_id, link, result, keeper, job, s)
            else:
                await deliver_stream(context.bot, chat_id, link, keeper, job, s, dl)
        if job.cancelled:
            await msg.reply_text(ERROR_TEXT[ErrorKind.CANCELLED])
    except DownloadError as e:
        if e.kind is ErrorKind.AUTH:
            await _alert_admin(context, link, e.detail)
        if e.kind in (ErrorKind.OTHER, ErrorKind.AUTH):
            log.error("download failed for %s: %s", link.url, e.detail)
        await msg.reply_text(ERROR_TEXT[e.kind].format(platform=link.platform.value.capitalize()))
    except Exception:
        log.exception("unhandled error for %s", link.url)
        await msg.reply_text("Something broke. Details are in the log.")
    finally:
        dl.finish(job)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("update error", exc_info=context.error)


# ---------- setup ----------

def setup_logging(s: Settings) -> None:
    s.log_dir.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_h = RotatingFileHandler(s.log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(fmt)
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_h, stream_h])
    logging.getLogger("httpx").setLevel(logging.WARNING)  # would leak the bot token in URLs


async def post_init(app: Application) -> None:
    s: Settings = app.bot_data["settings"]
    old = stale_dirs(s.tmp_dir, 60)
    if old:
        log.info("removing %d stale tmp dir(s) from a previous run", len(old))
        delete(old)


def main() -> None:
    s = load_settings()
    setup_logging(s)
    s.tmp_dir.mkdir(exist_ok=True)
    if not s.gallery_dl_bin.exists():
        raise SystemExit(f"gallery-dl not found at {s.gallery_dl_bin} (pip install -r requirements.txt in the venv)")

    builder = (
        Application.builder()
        .token(s.bot_token)
        .concurrent_updates(True)  # so /cancel works while a job is running
        .read_timeout(30)
        .write_timeout(180)
        .post_init(post_init)
    )
    if s.local_api_url:
        builder = builder.base_url(f"{s.local_api_url}/bot").base_file_url(f"{s.local_api_url}/file/bot").local_mode(True)
    app = builder.build()
    app.bot_data["settings"] = s
    app.bot_data["downloader"] = Downloader(s)

    allowed = filters.User(user_id=list(s.allowed_ids))
    app.add_handler(CommandHandler("id", cmd_id))  # open to everyone, used to get IDs for the allowlist
    app.add_handler(CommandHandler(["start", "help"], cmd_help, filters=allowed))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(
        MessageHandler(~allowed & filters.ChatType.PRIVATE & ~filters.Regex(r"^/id(@\w+)?\b"), on_denied),
        group=1,
    )
    app.add_error_handler(on_error)

    log.info("starting, %d allowed user(s), gallery-dl: %s", len(s.allowed_ids), s.gallery_dl_bin)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
