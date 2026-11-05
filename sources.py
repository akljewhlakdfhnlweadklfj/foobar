"""Fetching a source: one place for the fallback rules, per kind of source.

- rss: the feed itself, then Feedly's copy of it.
- youtube: the channel's feed (with view counts), then Feedly's copy plus the channel's videos page for
  view counts (at most every youtube.VIEWS_EVERY seconds per channel), then the page alone. See youtube.py.

Rules shared by every kind: a URL refused by the private-address guard is never routed around via
Feedly; a Feedly rate-limit is reported as such (headlines already stored are kept).

`via` says where the items came from: "direct", "feedly", "page" (YouTube's channel page) or "cache"
(seed_cache, see seedcache.py); `note` explains a fallback (not an error).
"""

from __future__ import annotations

import time

import feedly
import feeds
import youtube

KINDS = ("rss", "youtube")
VIA = ("direct", "feedly", "page", "cache")


def kind_of(url: str) -> str:
    return "youtube" if youtube.is_youtube(url) else "rss"


def _feedly(url: str, direct_err: feeds.FeedError, since: float | None = None) -> feeds.ParsedFeed | None:
    """Feedly's copy, None if it has nothing; raises FeedError if it's rate-limiting us. since: only
    what Feedly crawled after then (nothing new is then an empty feed, not a failure)."""
    try:
        title, entries = feedly.recent(url, since=since)    # doesn't wait out a rate-limit cooldown
    except feedly.RateLimited:
        raise feeds.FeedError(f"{direct_err}; Feedly fallback is rate-limited right now, "
                              "will retry at the next refresh (existing headlines are kept)") from None
    except Exception:  # noqa: BLE001 - Feedly unreachable or doesn't know the feed
        return None
    return feedly.to_parsed(entries, title, url) if entries or since else None


def fetch(url: str, views_checked_at: float = 0.0, feedly_since: float | None = None
          ) -> tuple[feeds.ParsedFeed, str, str | None]:
    """(parsed feed, via, note). views_checked_at: when this YouTube channel's view counts were last
    read from its page; parsed.views_checked_at is set when they're read now (stored by the caller).
    feedly_since: when the Feedly fallback last worked for this source (it then sends only newer
    entries); None to get its latest 100."""
    try:
        parsed = feeds.fetch_and_parse(url)
        if kind_of(url) == "youtube":
            parsed.views_checked_at = time.time()       # the channel feed carries view counts itself
        return parsed, "direct", None
    except feeds.FeedError as direct_err:
        if str(direct_err).startswith("Refusing to fetch"):
            raise                                       # the private-address guard: never route around it
        err = direct_err
    if kind_of(url) == "rss":
        parsed = _feedly(url, err, feedly_since)
        if parsed is None:
            raise err
        return parsed, "feedly", f"Direct fetch failed ({err}); using Feedly's copy"
    return _youtube_fallback(url, err, views_checked_at, feedly_since)


def _youtube_fallback(url: str, err: feeds.FeedError, views_checked_at: float, feedly_since: float | None):
    cid = youtube.channel_id(url)
    try:
        parsed = _feedly(url, err, feedly_since)
    except feeds.FeedError:
        parsed = None                                   # rate-limited: the channel page still works
    if parsed is not None and time.time() - views_checked_at < youtube.VIEWS_EVERY:
        return parsed, "feedly", f"YouTube's feed failed ({err}); using Feedly's copy"
    try:
        name, videos = youtube.channel_videos(cid)
    except Exception as e:  # noqa: BLE001
        if parsed is None:
            raise feeds.FeedError(f"{err}; the channel page failed too ({e})") from None
        return parsed, "feedly", f"YouTube's feed failed ({err}); using Feedly's copy (channel page failed: {e})"
    if parsed is None:
        page = feeds.ParsedFeed(name, videos)
        page.views_checked_at = time.time()
        return page, "page", f"YouTube's feed failed ({err}); using the channel page (no descriptions)"
    views = {youtube.video_id(v.link): v.views for v in videos}
    known = {e.guid for e in parsed.entries}
    for e in parsed.entries:
        e.views = views.get(youtube.video_id(e.link), e.views)
    parsed.entries += [v for v in videos if v.guid not in known]     # older videos: their counts update too
    parsed.views_checked_at = time.time()
    return parsed, "feedly", f"YouTube's feed failed ({err}); using Feedly's copy and the channel page for view counts"
