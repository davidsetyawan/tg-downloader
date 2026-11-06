"""URL classification. No I/O, no dependency on config, easy to unit test."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class Platform(str, Enum):
    TWITTER = "twitter"
    INSTAGRAM = "instagram"


class Kind(str, Enum):
    POST = "post"            # tweet, IG post, reel, single story item
    PROFILE = "profile"
    STORIES = "stories"      # all active stories of a user
    HIGHLIGHT = "highlight"


@dataclass(frozen=True)
class ParsedLink:
    platform: Platform
    kind: Kind
    url: str                    # what gets passed to gallery-dl
    username: str | None        # None when unknown (e.g. /i/web/status/ID, highlights)


_URL_RE = re.compile(r"https?://[^\s<>]+", re.I)

_TW_USER = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_IG_USER = re.compile(r"^[A-Za-z0-9._]{1,30}$")

_TW_RESERVED = {
    "home", "explore", "i", "search", "settings", "notifications", "messages",
    "intent", "share", "hashtag", "compose", "login", "signup", "tos", "privacy",
    "about", "download", "jobs", "lists", "bookmarks", "communities", "premium",
}
_IG_RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "tags",
    "about", "developer", "legal", "web", "share", "challenge", "emails", "locations",
}


def profile_url(platform: Platform, username: str) -> str:
    if platform is Platform.TWITTER:
        return f"https://x.com/{username}"
    return f"https://www.instagram.com/{username}/"


def _twitter(segs: list[str]) -> ParsedLink | None:
    if not segs:
        return None
    # /i/web/status/<id>
    if segs[:3] == ["i", "web", "status"] and len(segs) >= 4 and segs[3].isdigit():
        return ParsedLink(Platform.TWITTER, Kind.POST, f"https://x.com/i/web/status/{segs[3]}", None)
    # /<user>/status/<id>[/photo/1 ...]
    if len(segs) >= 3 and segs[1] in ("status", "statuses") and segs[2].isdigit():
        if _TW_USER.match(segs[0]):
            return ParsedLink(
                Platform.TWITTER, Kind.POST, f"https://x.com/{segs[0]}/status/{segs[2]}", segs[0]
            )
        return None
    # /<user>[/media|/with_replies]
    user = segs[0]
    if (
        _TW_USER.match(user)
        and user.lower() not in _TW_RESERVED
        and (len(segs) == 1 or segs[1] in ("media", "with_replies"))
    ):
        return ParsedLink(Platform.TWITTER, Kind.PROFILE, f"https://x.com/{user}/media", user)
    return None


def _instagram(segs: list[str]) -> ParsedLink | None:
    if not segs:
        return None
    ig = "https://www.instagram.com"
    head = segs[0]

    if head in ("p", "reel", "reels", "tv") and len(segs) >= 2 and segs[1] != "audio":
        path = "reel" if head == "reels" else head
        return ParsedLink(Platform.INSTAGRAM, Kind.POST, f"{ig}/{path}/{segs[1]}/", None)

    if len(segs) >= 3 and segs[1] in ("p", "reel", "tv") and _IG_USER.match(head):
        return ParsedLink(Platform.INSTAGRAM, Kind.POST, f"{ig}/{segs[1]}/{segs[2]}/", head)

    if head == "share" and len(segs) >= 3:  # share links, resolved by gallery-dl
        return ParsedLink(Platform.INSTAGRAM, Kind.POST, f"{ig}/share/{segs[1]}/{segs[2]}/", None)

    if head == "stories":
        if len(segs) >= 3 and segs[1] == "highlights" and segs[2].isdigit():
            return ParsedLink(
                Platform.INSTAGRAM, Kind.HIGHLIGHT, f"{ig}/stories/highlights/{segs[2]}/", None
            )
        if len(segs) >= 2 and _IG_USER.match(segs[1]) and segs[1] != "highlights":
            if len(segs) >= 3 and segs[2].isdigit():  # one specific story item
                return ParsedLink(
                    Platform.INSTAGRAM, Kind.POST, f"{ig}/stories/{segs[1]}/{segs[2]}/", segs[1]
                )
            return ParsedLink(Platform.INSTAGRAM, Kind.STORIES, f"{ig}/stories/{segs[1]}/", segs[1])
        return None

    if (
        _IG_USER.match(head)
        and head.lower() not in _IG_RESERVED
        and (len(segs) == 1 or (len(segs) == 2 and segs[1] == "reels"))
    ):
        suffix = "reels/" if len(segs) == 2 else ""
        return ParsedLink(Platform.INSTAGRAM, Kind.PROFILE, f"{ig}/{head}/{suffix}", head)
    return None


def parse_url(raw: str) -> ParsedLink | None:
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "mobile.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    segs = [s for s in parts.path.split("/") if s]
    if host in ("x.com", "twitter.com"):
        return _twitter(segs)
    if host == "instagram.com":
        return _instagram(segs)
    return None


def parse_message(text: str) -> ParsedLink | None:
    """First supported link in the message."""
    m = _URL_RE.search(text or "")
    if not m:
        return None
    raw = m.group(0).rstrip(".,;:!?)]}>'\"")
    return parse_url(raw)


def has_url(text: str) -> bool:
    return bool(_URL_RE.search(text or ""))
