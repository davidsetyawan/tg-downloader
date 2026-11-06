"""ffmpeg helper: shrink a video under Telegram's upload limit."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger("mediautil")


async def _duration(path: Path) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return None


async def shrink_video(path: Path, limit_bytes: int) -> Path | None:
    """Re-encode to fit limit_bytes (single pass, <=720p). None if not possible."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return None
    dur = await _duration(path)
    if not dur or dur <= 0:
        return None
    total_kbps = int(limit_bytes * 8 * 0.90 / 1000 / dur)
    video_kbps = total_kbps - 96
    if video_kbps < 100:
        return None
    out = path.with_name(path.stem + ".shrunk.mp4")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
        "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0 or not out.is_file():
        log.warning("ffmpeg failed: %s", err.decode(errors="replace")[-500:])
        return None
    if out.stat().st_size > limit_bytes:
        return None
    return out
