"""YouTube channels as a source: a channel is an outlet, its videos are items (video title = headline,
video description = description, thumbnail = image, plus view counts).

What a channel's feed (https://www.youtube.com/feeds/videos.xml?channel_id=UC...) gives, per video
(its 15 latest): yt:videoId, title, link, author (channel), published / updated, and a media:group with
media:title, media:description (the full description), media:thumbnail (480x360), media:content (the
player), and media:community with media:starRating (likes count) and media:statistics (views).

YouTube refuses that feed from some servers (HTTP 404 from cloud machines). Then:
1. Feedly's copy of the feed: titles, descriptions, thumbnails, exact times, but no view counts;
2. the channel's videos page: its ~30 latest videos with view counts ("8.1M") and approximate ages
   ("1d ago"). Used for view counts (at most every VIEWS_EVERY seconds per channel) and, when Feedly
   doesn't have the channel either, for the videos themselves (without descriptions).
"""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urlsplit

import feeds
import feedly

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
_CHANNEL_ID = re.compile(r"(UC[\w-]{22})")
_FEED_RE = re.compile(r"youtube\.com/feeds/videos\.xml\?channel_id=(UC[\w-]{22})")
VIEWS_EVERY = 3 * 3600
_views_checked: dict[str, float] = {}
_views_lock = threading.Lock()


def is_youtube(url: str) -> bool:
    return bool(_FEED_RE.search(url or ""))


def is_video(link: str) -> bool:
    return bool(re.search(r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w-]{11}", link or ""))


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
    parts = urlsplit(text if "//" in text else "https://" + text)
    host = (parts.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        return None
    m = re.match(r"/channel/(UC[\w-]{22})", parts.path)
    if m:
        return FEED_URL.format(m.group(1))
    if parts.path.startswith(("/@", "/c/", "/user/")):
        html = _page(f"https://www.youtube.com{parts.path.split('/videos')[0]}")
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
                                                   pub, "", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", views)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return name, list(found.values())


def fetch(url: str, from_feedly) -> tuple[feeds.ParsedFeed, str, str | None]:
    """(parsed feed, via, note) for a channel feed: the feed itself, or Feedly plus the channel page.
    from_feedly converts Feedly entries (scraper._from_feedly)."""
    cid = _FEED_RE.search(url).group(1)
    try:
        parsed = feeds.fetch_and_parse(url)
        with _views_lock:
            _views_checked[cid] = time.time()      # the feed has view counts itself
        return parsed, "direct", None
    except feeds.FeedError as direct_err:
        if str(direct_err).startswith("Refusing to fetch"):
            raise
        err = direct_err
    parsed, via, note = None, "feedly", f"YouTube's feed failed ({err}); using Feedly's copy"
    try:
        title, entries = feedly.recent(url)
        if entries:
            parsed = from_feedly(entries, title, url)
    except Exception:  # noqa: BLE001 - try the channel page instead
        pass
    with _views_lock:
        due = parsed is None or time.time() - _views_checked.get(cid, 0) >= VIEWS_EVERY
        if due:
            _views_checked[cid] = time.time()
    if due:
        try:
            name, videos = channel_videos(cid)
        except Exception as e:  # noqa: BLE001
            if parsed is None:
                raise feeds.FeedError(f"{err}; the channel page failed too ({e})") from None
            return parsed, via, note
        if parsed is None:
            return feeds.ParsedFeed(name, videos), "youtube page", f"YouTube's feed failed ({err}); using the channel page"
        views = {video_id(v.link): v.views for v in videos}
        known = {e.guid for e in parsed.entries}
        for e in parsed.entries:
            e.views = views.get(video_id(e.link), e.views)
        parsed.entries += [v for v in videos if v.guid not in known]   # older ones: updates their view counts
        note += " and the channel page for view counts"
    return parsed, via, note
