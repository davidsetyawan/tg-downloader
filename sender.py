"""Reply formatting + Telegram sending."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from dataclasses import dataclass

from pathlib import Path

from telegram import InputFile, InputMediaDocument, InputMediaPhoto, InputMediaVideo
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter

from downloader import Job, Media
from mediautil import shrink_video

log = logging.getLogger("sender")

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
PHOTO_LIMIT = 10 * 1024 * 1024  # send_photo hard limit; bigger goes as document
GROUP_SIZE = 10
TIMEOUTS = dict(read_timeout=180, write_timeout=180, connect_timeout=30, pool_timeout=30)


# ---------- chat action ("sending video/photo..." status) ----------

class ChatActionKeeper:
    """Keeps a Telegram chat action alive (it expires after ~5s) while a job runs."""

    def __init__(self, bot, chat_id: int, action: str = ChatAction.UPLOAD_VIDEO):
        self.bot, self.chat_id, self.action = bot, chat_id, action
        self._task: asyncio.Task | None = None

    def set(self, action: str) -> None:
        self.action = action

    async def _loop(self) -> None:
        while True:
            try:
                await self.bot.send_chat_action(chat_id=self.chat_id, action=self.action)
            except Exception as e:  # never let the status break the job
                log.debug("chat action failed: %s", e)
            await asyncio.sleep(4)

    async def __aenter__(self) -> "ChatActionKeeper":
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


def _action_for(m: Media) -> str:
    return {
        "video": ChatAction.UPLOAD_VIDEO,
        "animation": ChatAction.UPLOAD_VIDEO,
        "photo": ChatAction.UPLOAD_PHOTO,
    }.get(m.kind, ChatAction.UPLOAD_DOCUMENT)


# ---------- captions ----------

def esc(s: str) -> str:
    return html.escape(s, quote=False)


def profile_link_html(username: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(username)}</a>'


def build_post_caption(caption: str, username: str, profile_url: str, post_url: str) -> tuple[str, str | None]:
    """-> (caption_html <= 1024 chars, full_caption_html or None if it was truncated)."""
    footer = f"by: {profile_link_html(username, profile_url)}\n{esc(post_url)}"
    caption = caption.strip()
    if not caption:
        return footer, None
    budget = CAPTION_LIMIT - len(footer) - 2
    if len(esc(caption)) <= budget:
        return f"{esc(caption)}\n\n{footer}", None
    cut = caption[: max(budget, 0)]
    while cut and len(esc(cut)) > budget - 1:
        cut = cut[:-8]
    return f"{esc(cut).rstrip()}…\n\n{footer}", esc(caption[: MESSAGE_LIMIT - 200])


# ---------- sending ----------

@dataclass
class SendStats:
    sent: int = 0
    skipped: int = 0


async def _retry(fn, **kw):
    for _ in range(3):
        try:
            return await fn(**kw)
        except RetryAfter as e:
            ra = e.retry_after
            secs = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
            log.warning("flood control, sleeping %.0fs", secs)
            await asyncio.sleep(secs + 1)
    return await fn(**kw)


def _dims(m: Media) -> dict:
    w, h = m.meta.get("width"), m.meta.get("height")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return {"width": w, "height": h}
    return {}


async def prepare(items: list[Media], limit_bytes: int) -> tuple[list[Media], int]:
    """Drop/shrink files that exceed upload limits. -> (ready, skipped_count)."""
    ready: list[Media] = []
    skipped = 0
    for m in items:
        size = m.path.stat().st_size
        if size > limit_bytes:
            out = await shrink_video(m.path, limit_bytes) if m.kind in ("video", "animation") else None
            if out is None:
                skipped += 1
                continue
            m = Media(out, m.kind, m.post_key, m.meta)
        elif m.kind == "photo" and size > PHOTO_LIMIT:
            m = Media(m.path, "document", m.post_key, m.meta)
        ready.append(m)
    return ready, skipped


def _chunks(group: list[Media]) -> list[list[Media]]:
    """Albums of 10 in order; the remainder is the last one (11 -> 10+1, 12 -> 10+2)."""
    return [group[i:i + GROUP_SIZE] for i in range(0, len(group), GROUP_SIZE)]


def _segments(items: list[Media]) -> list[list[Media]]:
    """Everything goes into albums. Photos/videos/GIFs share one album (Telegram albums can't hold
    animations, so GIFs ride along as videos); documents can only be albumed with documents.
    Only a lone file is sent on its own."""
    if len(items) == 1:
        return [items]
    visual = [m for m in items if m.kind != "document"]
    docs = [m for m in items if m.kind == "document"]
    return _chunks(visual) + _chunks(docs)


async def _send_single(bot, chat_id: int, m: Media, caption: str | None) -> None:
    kw: dict = dict(chat_id=chat_id, parse_mode=ParseMode.HTML, **TIMEOUTS)
    if caption:
        kw["caption"] = caption
    try:
        if m.kind == "video":
            await _retry(bot.send_video, video=m.path, supports_streaming=True, **_dims(m), **kw)
        elif m.kind == "animation":
            await _retry(bot.send_animation, animation=m.path, **_dims(m), **kw)
        elif m.kind == "photo":
            await _retry(bot.send_photo, photo=m.path, **kw)
        else:
            await _retry(bot.send_document, document=m.path, **kw)
    except BadRequest as e:
        if m.kind == "document":
            raise
        log.warning("%s rejected as %s (%s), resending as document", m.path.name, m.kind, e)
        await _retry(bot.send_document, document=m.path, **kw)


def _input_file(path: Path) -> InputFile:
    """InputMedia* hard-codes local_mode=True, so a Path/str would become a file:// URI that only a
    local Bot API server understands ("unsupported url protocol" on the cloud API). Upload the bytes."""
    return InputFile(path.read_bytes(), filename=path.name, attach=True)


def _input_media(m: Media, caption: str | None, as_document: bool = False):
    kw: dict = {}
    if caption:
        kw.update(caption=caption, parse_mode=ParseMode.HTML)
    f = _input_file(m.path)
    if as_document or m.kind == "document":
        return InputMediaDocument(media=f, **kw)
    if m.kind in ("video", "animation"):
        return InputMediaVideo(media=f, supports_streaming=True, **_dims(m), **kw)
    return InputMediaPhoto(media=f, **kw)


async def _send_album(bot, chat_id: int, seg: list[Media], caption: str | None, as_document: bool = False) -> None:
    media = [_input_media(m, caption if i == 0 else None, as_document) for i, m in enumerate(seg)]
    await _retry(bot.send_media_group, chat_id=chat_id, media=media, **TIMEOUTS)


async def _send_segment(bot, chat_id: int, seg: list[Media], caption: str | None) -> None:
    if len(seg) == 1:
        await _send_single(bot, chat_id, seg[0], caption)
        return
    try:
        await _send_album(bot, chat_id, seg, caption)
        return
    except BadRequest as e:
        log.warning("album rejected (%s)", e)
    if any(m.kind != "document" for m in seg):
        # one bad file (odd dimensions/codec) must not split the album: resend all as documents
        try:
            await _send_album(bot, chat_id, seg, caption, as_document=True)
            return
        except BadRequest as e:
            log.warning("document album rejected too (%s), sending one by one", e)
    for i, m in enumerate(seg):
        await _send_single(bot, chat_id, m, caption if i == 0 else None)


async def send_items(
    bot,
    chat_id: int,
    items: list[Media],
    caption: str | None,
    caption_on: str,  # "first" (post) or "last" (profile)
    keeper: ChatActionKeeper,
    job: Job,
    limit_bytes: int,
) -> SendStats:
    ready, skipped = await prepare(items, limit_bytes)
    stats = SendStats(skipped=skipped)
    segs = _segments(ready)
    cap_idx = 0 if caption_on == "first" else len(segs) - 1
    for i, seg in enumerate(segs):
        if job.cancelled:
            break
        keeper.set(_action_for(seg[0]))
        await _send_segment(bot, chat_id, seg, caption if i == cap_idx else None)
        stats.sent += len(seg)
    return stats
