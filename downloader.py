"""gallery-dl wrapper: async subprocess, per-request temp dir, one-at-a-time lock."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from config import Settings
from linkparser import Kind, ParsedLink, Platform

log = logging.getLogger("downloader")

FILENAME_FMT = "{category}_{tweet_id|post_id|media_id|id|filename}_{num}.{extension}"

BATCH_SIZE = 10  # one Telegram album

VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp"}


class ErrorKind(str, Enum):
    AUTH = "auth"
    NOT_FOUND = "not_found"
    PRIVATE = "private"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    EMPTY = "empty"
    NO_STORIES = "no_stories"
    CANCELLED = "cancelled"
    OTHER = "other"


class DownloadError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = ""):
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail


@dataclass(eq=False)
class Job:
    chat_id: int
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False


@dataclass
class Media:
    path: Path
    kind: str                 # video | photo | animation | document
    post_key: str
    meta: dict = field(default_factory=dict)


@dataclass
class Result:
    items: list[Media]
    warning: str | None = None


@dataclass
class StreamResult:
    total: int
    warning: str | None = None


BatchCallback = Callable[[list[Media]], Awaitable[None]]


def classify_error(stderr: str) -> ErrorKind:
    lines = [l for l in stderr.splitlines() if "error" in l.lower()]
    s = ("\n".join(lines) or stderr).lower()
    if any(x in s for x in ("ratelimit", "rate limit", "429", "too many requests")):
        return ErrorKind.RATE_LIMIT
    if any(x in s for x in ("notfounderror", "404", "does not exist", "not found")):
        return ErrorKind.NOT_FOUND
    if "authrequired" in s:
        return ErrorKind.AUTH
    if any(x in s for x in ("authorizationerror", "private", "403", "forbidden")):
        return ErrorKind.PRIVATE
    if any(x in s for x in ("login", "cookie", "401", "authentication")):
        return ErrorKind.AUTH
    return ErrorKind.OTHER


def _media_kind(path: Path, meta: dict) -> str:
    ext = path.suffix.lower()
    if meta.get("type") == "animated_gif" or ext == ".gif":
        return "animation"
    if ext in VIDEO_EXT:
        return "video"
    if ext in PHOTO_EXT:
        return "photo"
    return "document"


def _post_key(meta: dict, path: Path) -> str:
    for k in ("tweet_id", "post_id", "post_shortcode", "media_id", "id"):
        if meta.get(k):
            return str(meta[k])
    return path.stem


_TCO_TAIL = re.compile(r"\s*https://t\.co/\w+\s*$")


def post_info(meta: dict, link: ParsedLink) -> tuple[str, str, str]:
    """-> (username, caption, post_url) for the reply footer."""
    if link.platform is Platform.TWITTER:
        author = meta.get("author") or meta.get("user") or {}
        username = author.get("name") or link.username or "unknown"
        caption = _TCO_TAIL.sub("", meta.get("content") or "").strip()
        tid = meta.get("tweet_id")
        url = f"https://x.com/{username}/status/{tid}" if tid else link.url
    else:
        username = meta.get("username") or link.username or "unknown"
        caption = (meta.get("description") or "").strip()
        url = meta.get("post_url") or link.url
    return username, caption, url


class Downloader:
    POLL = 1.0  # seconds between tmp dir scans while streaming

    def __init__(self, settings: Settings):
        self.s = settings
        self._sem = asyncio.Semaphore(settings.max_concurrent)
        self._active: dict[int, set[Job]] = defaultdict(set)
        self._last_end = 0.0

    # ---- job registry (for /cancel) ----
    def new_job(self, chat_id: int) -> Job:
        job = Job(chat_id)
        self._active[chat_id].add(job)
        return job

    def finish(self, job: Job) -> None:
        self._active[job.chat_id].discard(job)

    def cancel(self, chat_id: int) -> int:
        jobs = list(self._active[chat_id])
        for job in jobs:
            job.cancelled = True
            if job.proc and job.proc.returncode is None:
                job.proc.kill()
        return len(jobs)

    # ---- main entry ----
    @asynccontextmanager
    async def download(self, link: ParsedLink, job: Job):
        workdir = self.s.tmp_dir / uuid.uuid4().hex
        workdir.mkdir(parents=True)
        try:
            async with self._sem:
                wait = self.s.request_delay - (time.monotonic() - self._last_end)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    result = await self._run(link, job, workdir)
                finally:
                    self._last_end = time.monotonic()
            yield result
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _build_cmd(self, link: ParsedLink, workdir: Path) -> list[str]:
        cmd = [
            str(self.s.gallery_dl_bin),
            "--config", str(self.s.gallery_dl_conf),
            "-D", str(workdir),
            "-f", FILENAME_FMT,
            "--write-metadata",
        ]
        cookies = self.s.cookies_dir / f"{link.platform.value}.txt"
        if cookies.is_file():
            cmd += ["--cookies", str(cookies)]
        cmd.append(link.url)
        return cmd

    async def _run(self, link: ParsedLink, job: Job, workdir: Path) -> Result:
        cmd = self._build_cmd(link, workdir)
        log.info("gallery-dl: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        job.proc = proc
        timed_out = False
        stderr = ""
        try:
            _, err = await asyncio.wait_for(proc.communicate(), self.s.download_timeout)
            stderr = err.decode(errors="replace")
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        finally:
            job.proc = None

        if job.cancelled:
            raise DownloadError(ErrorKind.CANCELLED)

        items = self._collect(workdir)
        if stderr.strip():
            log.warning("gallery-dl stderr (rc=%s):\n%s", proc.returncode, stderr.strip()[-2000:])

        if items:
            warning = None
            if timed_out:
                warning = f"Timed out after {self.s.download_timeout}s, partial result."
            elif proc.returncode:
                warning = "gallery-dl reported errors, result may be incomplete."
            return Result(items, warning)

        if timed_out:
            raise DownloadError(ErrorKind.TIMEOUT)
        if proc.returncode:
            raise DownloadError(classify_error(stderr), stderr.strip()[-500:])
        if link.kind is Kind.STORIES:
            raise DownloadError(ErrorKind.NO_STORIES)
        raise DownloadError(ErrorKind.EMPTY)

    @staticmethod
    def _load(p: Path) -> Media:
        meta: dict = {}
        mp = p.with_name(p.name + ".json")
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("bad metadata file %s", mp)
        return Media(p, _media_kind(p, meta), _post_key(meta, p), meta)

    @classmethod
    def _collect(cls, workdir: Path) -> list[Media]:
        files = [p for p in workdir.iterdir() if p.is_file() and p.suffix != ".json"]
        files.sort(key=lambda p: (p.stat().st_ctime_ns, p.name))  # download order
        return [cls._load(p) for p in files]

    # ---- streaming (profiles, stories, highlights) ----
    @classmethod
    def _ready(cls, workdir: Path, sent: set[str], final: bool) -> list[Media]:
        """Completed, not yet sent files in download order. gallery-dl writes name.part and
        renames when done, so a plain name means complete; the .json sidecar comes right after."""
        now = time.time_ns()
        found = []
        for p in workdir.iterdir():
            n = p.name
            if not p.is_file() or n in sent or n.endswith((".json", ".part", ".shrunk.mp4")):
                continue
            st = p.stat()
            if not final and not p.with_name(n + ".json").is_file() and now - st.st_ctime_ns < 3_000_000_000:
                continue  # sidecar not written yet
            found.append((st.st_ctime_ns, n, p))
        found.sort()
        return [cls._load(p) for _, _, p in found]

    @staticmethod
    def _discard(m: Media) -> None:
        for f in (m.path, m.path.with_name(m.path.name + ".json"), m.path.with_name(m.path.stem + ".shrunk.mp4")):
            f.unlink(missing_ok=True)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        if proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.send_signal(sig)

    async def stream(self, link: ParsedLink, job: Job, on_batch: BatchCallback,
                     batch_size: int = BATCH_SIZE) -> StreamResult:
        """Run gallery-dl and hand over every `batch_size` finished files to on_batch as they appear.
        Files are deleted after on_batch returns; gallery-dl is paused (SIGSTOP) while sending,
        so disk use stays around one batch. Idle timeout instead of a total timeout."""
        workdir = self.s.tmp_dir / uuid.uuid4().hex
        workdir.mkdir(parents=True)
        try:
            async with self._sem:
                wait = self.s.request_delay - (time.monotonic() - self._last_end)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    return await self._stream(link, job, workdir, on_batch, batch_size)
                finally:
                    self._last_end = time.monotonic()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _stream(self, link, job, workdir, on_batch, batch_size) -> StreamResult:
        cmd = self._build_cmd(link, workdir)
        log.info("gallery-dl (stream): %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        job.proc = proc
        comm = asyncio.create_task(proc.communicate())  # drains pipes so gallery-dl never blocks
        sent: set[str] = set()
        total = 0
        timed_out = False
        last_progress = time.monotonic()
        try:
            while True:
                finished = comm.done()
                ready = self._ready(workdir, sent, final=finished)
                while ready and (len(ready) >= batch_size or finished) and not job.cancelled:
                    batch, ready = ready[:batch_size], ready[batch_size:]
                    self._signal(proc, signal.SIGSTOP)
                    try:
                        await on_batch(batch)
                    finally:
                        self._signal(proc, signal.SIGCONT)
                    for m in batch:
                        sent.add(m.path.name)
                        self._discard(m)
                    total += len(batch)
                    last_progress = time.monotonic()
                if finished or job.cancelled:
                    break
                if not timed_out and time.monotonic() - last_progress > self.s.profile_idle_timeout:
                    timed_out = True
                    proc.kill()
                await asyncio.sleep(self.POLL)
        except BaseException:
            proc.kill()
            comm.cancel()
            raise
        finally:
            job.proc = None
            if proc.returncode is None:
                proc.kill()

        _, err = await comm
        stderr = err.decode(errors="replace")
        if stderr.strip():
            log.warning("gallery-dl stderr (rc=%s):\n%s", proc.returncode, stderr.strip()[-2000:])
        if job.cancelled:
            raise DownloadError(ErrorKind.CANCELLED)
        if total:
            warning = None
            if timed_out:
                warning = f"No new file for {self.s.profile_idle_timeout}s, stopped early."
            elif proc.returncode:
                warning = "gallery-dl reported errors, result may be incomplete."
            return StreamResult(total, warning)
        if timed_out:
            raise DownloadError(ErrorKind.TIMEOUT)
        if proc.returncode:
            raise DownloadError(classify_error(stderr), stderr.strip()[-500:])
        if link.kind is Kind.STORIES:
            raise DownloadError(ErrorKind.NO_STORIES)
        raise DownloadError(ErrorKind.EMPTY)
