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

import json
import time
import urllib.parse
import urllib.request
from typing import Iterator

API = "https://cloud.feedly.com/v3"
USER_AGENT = "Mozilla/5.0 (compatible; NewsAggregator/1.0)"


def _get(path: str, params: dict, timeout: float = 30) -> dict:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:  # rate limited: back off
                time.sleep(5 * 2**attempt)
                continue
            raise
    return {}


def feed_info(feed_url: str) -> dict | None:
    """Feedly's metadata for a feed (subscribers, lastUpdated...), or None if unknown to Feedly."""
    res = _get("search/feeds", {"query": feed_url, "count": 5}).get("results", [])
    want = "feed/" + feed_url
    for r in res:
        if r.get("feedId", "").rstrip("/") == want.rstrip("/"):
            return r
    return None


def history(feed_url: str, since: float = 0, until: float | None = None, max_pages: int = 500) -> Iterator[dict]:
    """Yield Feedly entries for a feed, oldest first, published at or after `since` (unix seconds)."""
    stream = "feed/" + feed_url
    newer_ms = int(since * 1000)
    seen: set[str] = set()
    for _ in range(max_pages):
        items = _get("streams/contents", {"streamId": stream, "count": 1000,
                                          "ranked": "oldest", "newerThan": newer_ms}).get("items", [])
        fresh = [i for i in items if i["id"] not in seen]
        if not fresh:
            return
        for i in sorted(fresh, key=lambda i: i.get("published", 0)):
            seen.add(i["id"])
            if until is not None and i.get("published", 0) / 1000 > until:
                return
            yield i
        # newerThan filters on crawl time; advance to the newest crawl time we've seen.
        nxt = max(max(i.get("crawled", 0), i.get("published", 0)) for i in items)
        if nxt <= newer_ms:
            return
        newer_ms = nxt
        time.sleep(0.5)  # be polite


def recent(feed_url: str, count: int = 100) -> tuple[str, list[dict]]:
    """(feed title, newest entries) - a fallback for when the feed itself can't be fetched."""
    d = _get("streams/contents", {"streamId": "feed/" + feed_url, "count": count, "ranked": "newest"})
    return d.get("title", ""), d.get("items", [])


def entry_fields(e: dict) -> tuple[str, str, str, float]:
    """(guid, title, link, published_unix) for a Feedly entry."""
    link = (e.get("alternate") or [{}])[0].get("href") or e.get("canonicalUrl") or e.get("originId", "")
    return e.get("originId") or e["id"], (e.get("title") or "").strip(), link, e.get("published", 0) / 1000


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
