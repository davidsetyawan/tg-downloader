from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_ids: tuple[int, ...]
    download_timeout: int
    profile_idle_timeout: int
    max_concurrent: int
    request_delay: float
    max_upload_bytes: int
    local_api_url: str | None
    gallery_dl_bin: Path
    gallery_dl_conf: Path
    cookies_dir: Path
    tmp_dir: Path
    log_dir: Path

    @property
    def admin_id(self) -> int | None:
        return self.allowed_ids[0] if self.allowed_ids else None


def _ids(raw: str) -> tuple[int, ...]:
    return tuple(int(x) for x in raw.replace(" ", "").split(",") if x)


def load_settings(require_token: bool = True) -> Settings:
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "").strip()
    if require_token and not token:
        sys.exit("BOT_TOKEN is not set (see .env.example)")
    allowed = _ids(os.getenv("ALLOWED_USER_IDS", ""))
    if require_token and not allowed:
        sys.exit("ALLOWED_USER_IDS is empty: the bot would answer nobody")

    # gallery-dl from the same venv as the running interpreter, never the global one
    default_bin = Path(sys.executable).parent / "gallery-dl"
    return Settings(
        bot_token=token,
        allowed_ids=allowed,
        download_timeout=int(os.getenv("DOWNLOAD_TIMEOUT", "300")),
        profile_idle_timeout=int(os.getenv("PROFILE_IDLE_TIMEOUT", "300")),
        max_concurrent=int(os.getenv("MAX_CONCURRENT", "1")),
        request_delay=float(os.getenv("REQUEST_DELAY", "3")),
        max_upload_bytes=int(float(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024),
        local_api_url=(os.getenv("LOCAL_API_URL") or "").rstrip("/") or None,
        gallery_dl_bin=Path(os.getenv("GALLERY_DL_BIN", str(default_bin))),
        gallery_dl_conf=BASE_DIR / "gallery-dl.conf",
        cookies_dir=BASE_DIR / "cookies",
        tmp_dir=BASE_DIR / "tmp",
        log_dir=BASE_DIR / "logs",
    )
