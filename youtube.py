"""YouTube channels as a source: a channel is an outlet, its videos are items (video title = headline,
video description = description, thumbnail = image, plus view counts).

What a channel's feed (https://www.youtube.com/feeds/videos.xml?channel_id=UC...) gives, per video
(its 15 latest): yt:videoId, title, link, author (channel), published / updated, and a media:group with
media:title, media:description (the full description), media:thumbnail (480x360), media:content (the
player), and media:community with media:starRating (likes count) and media:statistics (views).

YouTube refuses that feed from some servers (HTTP 404 from cloud machines); sources.py then falls back
to Feedly's copy plus the channel's videos page (its ~30 latest videos with view counts like "8.1M" and
ages like "1d ago"), or the page alone.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

import feeds

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_CHANNEL_ID = re.compile(r"(UC[\w-]{22})")
_FEED_RE = re.compile(r"youtube\.com/feeds/videos\.xml\?channel_id=(UC[\w-]{22})")
VIEWS_EVERY = 3 * 3600   # seconds between reading a channel page for view counts (when its feed is refused)


def is_youtube(url: str) -> bool:
    return bool(_FEED_RE.search(url or ""))


def channel_id(feed_url: str) -> str:
    return _FEED_RE.search(feed_url).group(1)


def video_id(link: str) -> str:
    m = re.search(r"(?:v=|shorts/|youtu\.be/|yt:video:)([\w-]{11})", link or "")
    return m.group(1) if m else ""


def _page(url: str) -> str:
    return feeds.fetch(url, accept="text/html").decode("utf-8", "replace")


def feed_url_for(text: str) -> str | None:
    """The channel feed URL for a channel link (…/channel/UC…, …/@handle), a handle (@name) or a feed
    URL; None if it isn't a YouTube channel. Looks the handle up on YouTube when needed."""
    text = text.strip()
    if is_youtube(text):
        return FEED_URL.format(_FEED_RE.search(text).group(1))
    if text.startswith("@"):
        text = "https://www.youtube.com/" + text
    try:
        parts = urlsplit(text if "//" in text else "https://" + text)
        host = (parts.hostname or "").lower()
    except ValueError:                       # e.g. "https://[bad"
        return None
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        return None
    m = re.match(r"/channel/(UC[\w-]{22})", parts.path)
    if m:
        return FEED_URL.format(m.group(1))
    if parts.path.startswith(("/@", "/c/", "/user/")):
        html = _page("https://www.youtube.com" + re.sub(r"/(videos|featured|streams|shorts)/?$", "", parts.path))
        m = re.search(r'"externalId":"(UC[\w-]{22})"', html) or re.search(r'channel_id=(UC[\w-]{22})', html)
        if m:
            return FEED_URL.format(m.group(1))
        raise feeds.FeedError("Couldn't find that YouTube channel")
    return None


def parse_count(text: str) -> int | None:
    """'8.1M' -> 8100000, '12K views' -> 12000, '935' -> 935."""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", text or "", re.I)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()])


_AGO = re.compile(r"(\d+)\s*(second|minute|hour|day|week|month|year|s|m|h|d|w|mo|y)s?\s*ago", re.I)
_UNIT = {"second": 1, "s": 1, "minute": 60, "m": 60, "hour": 3600, "h": 3600, "day": 86400, "d": 86400,
         "week": 604800, "w": 604800, "month": 2592000, "mo": 2592000, "year": 31536000, "y": 31536000}


def channel_videos(channel_id: str) -> tuple[str, list[feeds.ParsedEntry]]:
    """(channel name, its latest videos) from the channel's videos page, with view counts."""
    html = _page(f"https://www.youtube.com/channel/{channel_id}/videos")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        raise feeds.FeedError("YouTube page without video data")
    data = json.loads(m.group(1))
    name = (data.get("metadata", {}).get("channelMetadataRenderer", {}) or {}).get("title", "")
    found: dict[str, feeds.ParsedEntry] = {}
    now = time.time()

    def walk(o):
        if isinstance(o, dict):
            lv = o.get("lockupViewModel")
            if lv and lv.get("contentId") and lv["contentId"] not in found:
                md = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
                title = (md.get("title") or {}).get("content", "")
                parts = [p.get("text", {}).get("content", "") for row in
                         md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                         for p in row.get("metadataParts", [])]
                views = next((parse_count(p) for p in parts if re.match(r"^[\d.,]+\s*[KMB]?(\s*views?)?$", p, re.I)), None)
                age = next((a for a in (_AGO.search(p) for p in parts) if a), None)
                pub = now - int(age.group(1)) * _UNIT[age.group(2).lower()] if age else now
                vid = lv["contentId"]
                if title:
                    found[vid] = feeds.ParsedEntry(f"yt:video:{vid}", title, f"https://www.youtube.com/watch?v={vid}",
                                                   pub, "", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", views, approx=True)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return name, list(found.values())
