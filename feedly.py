"""Read feed history from Feedly's public cloud API (no account needed).

Feedly keeps every entry of every feed any of its users subscribes to, so it works as an
archive for feeds that only expose their latest few dozen items. Unauthenticated requests
return at most 50 entries per call, so we page forward with ranked=oldest + newerThan.

Usage as a library:
    for entry in history("https://www.overworld.vg/feed.xml", since=time.time() - 14 * 86400): ...
CLI:
    python3 feedly.py FEED_URL [--days 30] > entries.jsonl
"""

from __future__ import annotations

import email.utils
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

API = "https://cloud.feedly.com/v3"
USER_AGENT = "Mozilla/5.0 (compatible; NewsAggregator/1.0)"

# Throttling. Feedly's free API rate-limits by IP and doesn't publish its limits, so we pace every
# request (shared across threads) and, after a 429, pause *all* Feedly traffic for a cooldown that
# doubles on each consecutive 429 (or follows Retry-After when Feedly sends one).
MIN_INTERVAL = 0.5           # seconds between any two Feedly requests
COOLDOWN_START = 30.0        # first pause after a 429
COOLDOWN_MAX = 300.0         # longest pause
MAX_WAIT = 600.0             # a single call gives up (RateLimited) after waiting this long in total


class RateLimited(Exception):
    """Feedly is throttling us; try again later."""

    def __init__(self, retry_in: float):
        super().__init__(f"Feedly is rate-limiting requests; try again in about {max(1, round(retry_in))}s")
        self.retry_in = retry_in


_lock = threading.Lock()
_next_slot = 0.0        # earliest time the next request may start (pacing)
_blocked_until = 0.0    # cooldown after a 429
_cooldown = 0.0         # current cooldown length (doubles on consecutive 429s)
on_rate_limit = None    # optional callback(pause_seconds), e.g. to print progress during a backfill


def cooling_down() -> float:
    """Seconds left in the current 429 cooldown (0 if none)."""
    return max(0.0, _blocked_until - time.monotonic())


def _reserve_slot(wait: bool, deadline: float) -> None:
    """Block until this thread may send a request, or raise RateLimited."""
    global _next_slot
    while True:
        with _lock:
            now = time.monotonic()
            start = max(now, _next_slot, _blocked_until)
            if start <= now:
                _next_slot = now + MIN_INTERVAL
                return
            blocked = _blocked_until - now
        if blocked > 0 and not wait:
            raise RateLimited(blocked)          # in a 429 cooldown and the caller won't wait
        if wait and start > deadline:
            raise RateLimited(start - now)
        # Otherwise it's just pacing (a fraction of a second per request ahead): wait our turn.
        time.sleep(min(start - now, 5.0))


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    value = e.headers.get("Retry-After") if e.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def _get(path: str, params: dict, timeout: float = 30, wait: bool = True) -> dict:
    """GET a Feedly API path. wait=False: don't sit out a cooldown, raise RateLimited instead."""
    global _blocked_until, _cooldown
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    deadline = time.monotonic() + (MAX_WAIT if wait else 0)
    while True:
        _reserve_slot(wait, deadline)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            with _lock:
                _cooldown = 0.0  # a success resets the backoff
            return data
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            with _lock:
                _cooldown = min(COOLDOWN_MAX, max(COOLDOWN_START, _cooldown * 2))
                pause = _retry_after(e) or _cooldown
                _blocked_until = max(_blocked_until, time.monotonic() + pause)
            if on_rate_limit:
                on_rate_limit(pause)
            if not wait:
                raise RateLimited(pause) from e


def feed_info(feed_url: str) -> dict | None:
    """Feedly's metadata for a feed (subscribers, lastUpdated...), or None if unknown to Feedly."""
    res = _get("search/feeds", {"query": feed_url, "count": 5}).get("results", [])
    want = "feed/" + feed_url
    for r in res:
        if r.get("feedId", "").rstrip("/") == want.rstrip("/"):
            return r
    return None


def history(feed_url: str, since: float = 0, until: float | None = None, max_pages: int = 500) -> Iterator[dict]:
    """Yield Feedly entries for a feed published in [since, until] (unix seconds), in crawl order.

    Unauthenticated calls return at most 50 entries and no continuation token, so we page with
    ranked=oldest (sorted by crawl time) and newerThan=<crawl time>. newerThan is strict, so the
    cursor is the last page's newest crawl time minus 1 ms: entries sharing that timestamp are
    fetched again and dropped by the `seen` set instead of being skipped. The cursor never uses
    `published`, which can be later than the crawl time.
    """
    stream = "feed/" + feed_url
    newer_ms = max(0, int(since * 1000) - 1)
    seen: set[str] = set()
    for _ in range(max_pages):
        items = _get("streams/contents", {"streamId": stream, "count": 1000,
                                          "ranked": "oldest", "newerThan": newer_ms}).get("items", [])
        fresh = [i for i in items if i["id"] not in seen]
        if not fresh:
            return
        for i in fresh:
            seen.add(i["id"])
            pub = i.get("published", i.get("crawled", 0)) / 1000
            if pub >= since and (until is None or pub <= until):
                yield i
        newest_crawl = max(i.get("crawled", 0) for i in items)
        if until is not None and min(i.get("crawled", 0) for i in items) / 1000 > until:
            return
        nxt = newest_crawl - 1
        if nxt <= newer_ms:
            return  # more than a page of entries share one crawl time; nothing further to page to
        newer_ms = nxt


def recent(feed_url: str, count: int = 100, wait: bool = False) -> tuple[str, list[dict]]:
    """(feed title, newest entries) - a fallback for when the feed itself can't be fetched.
    By default doesn't wait out a rate-limit cooldown (raises RateLimited instead)."""
    d = _get("streams/contents", {"streamId": "feed/" + feed_url, "count": count, "ranked": "newest"}, wait=wait)
    return d.get("title", ""), d.get("items", [])


def entry_image(e: dict) -> str:
    """Feedly's picture for an entry: its "visual", a thumbnail, or an image enclosure."""
    url = (e.get("visual") or {}).get("url") or ""
    if url and url != "none":
        return url
    for t in e.get("thumbnail") or []:
        if t.get("url"):
            return t["url"]
    for enc in e.get("enclosure") or []:
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            return enc["href"]
    return ""


def entry_fields(e: dict) -> tuple[str, str, str, float, str, str]:
    """(guid, title, link, published_unix, raw description HTML, image URL) for a Feedly entry."""
    link = (e.get("alternate") or [{}])[0].get("href") or e.get("canonicalUrl") or e.get("originId", "")
    desc = (e.get("summary") or e.get("content") or {}).get("content", "")
    return (e.get("originId") or e["id"], (e.get("title") or "").strip(), link, e.get("published", 0) / 1000,
            desc, entry_image(e))


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser()
    ap.add_argument("feed_url")
    ap.add_argument("--days", type=float, default=0, help="only the last N days (0 = everything Feedly has)")
    a = ap.parse_args()
    since = time.time() - a.days * 86400 if a.days else 0
    n = 0
    for e in history(a.feed_url, since=since):
        sys.stdout.write(json.dumps(e) + "\n")
        n += 1
    print(f"{n} entries", file=sys.stderr)
