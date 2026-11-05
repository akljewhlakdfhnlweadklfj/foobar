"""Pre-fetched history shipped with the repo, so a new vertical starts with stories immediately.

seed_cache/<vertical>.jsonl.gz: gzipped JSON lines. Line 1 is metadata
    {"vertical": ..., "created": <unix>, "since": <unix>, "items": <n>}
and every other line is one item:
    {"url": <feed url>, "guid": ..., "title": ..., "link": ..., "published": <unix>, "description": ...}

Loaded by app.open_stores when a vertical's database has feeds but no headlines yet (a new
database, or one created before the cache existed). Items older than the retention window are
dropped on load, and the normal refresh then adds anything newer.
Build or refresh a cache with:  python3 tools/build_seed_cache.py <data dir> <vertical> [...]
"""

from __future__ import annotations

import gzip
import json
import time
from collections import defaultdict
from pathlib import Path

import feeds
from store import RETENTION_DAYS, Store

CACHE_DIR = Path(__file__).parent / "seed_cache"


def cache_path(vertical: str) -> Path:
    return CACHE_DIR / f"{vertical}.jsonl.gz"


def export(store: Store, vertical: str, out: Path | None = None) -> int:
    """Write every stored item of the vertical's feeds to its cache file. Returns the item count."""
    out = out or cache_path(vertical)
    out.parent.mkdir(parents=True, exist_ok=True)
    urls = {f["id"]: f["url"] for f in store.list_feeds()}
    with store._conn() as c:
        rows = c.execute("SELECT feed_id, guid, title, link, published, description FROM items "
                         "ORDER BY feed_id, published").fetchall()
    since = min((r["published"] for r in rows), default=time.time())
    tmp = out.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"vertical": vertical, "created": time.time(), "since": since, "items": len(rows)}) + "\n")
        for r in rows:
            f.write(json.dumps({"url": urls[r["feed_id"]], "guid": r["guid"], "title": r["title"], "link": r["link"],
                                "published": r["published"], "description": r["description"]},
                               ensure_ascii=False) + "\n")
    tmp.replace(out)
    return len(rows)


def load(store: Store, path: Path) -> tuple[int, int]:
    """Import a cache file into the store. Returns (items loaded, feeds touched).
    Only feeds present in the store are filled; unknown URLs are skipped."""
    by_url: dict[str, list[feeds.ParsedEntry]] = defaultdict(list)
    cutoff = time.time() - RETENTION_DAYS * 86400
    with gzip.open(path, "rt", encoding="utf-8") as f:
        meta = json.loads(f.readline())
        for line in f:
            r = json.loads(line)
            if r["published"] >= cutoff:
                by_url[r["url"]].append(feeds.ParsedEntry(r["guid"], r["title"], r["link"], r["published"],
                                                          r.get("description", "")))
    loaded = touched = 0
    for feed in store.list_feeds():
        entries = by_url.get(feed["url"])
        if not entries:
            continue
        store.save_fetch(feed["id"], feeds.ParsedFeed("", entries), None, "cache")
        # Tell resumable backfills this history is already here (as far back as the cache goes).
        store.mark_backfilled(feed["id"], max(meta.get("since", time.time()), cutoff))
        loaded += len(entries)
        touched += 1
    return loaded, touched


def store_is_empty(store: Store) -> bool:
    with store._conn() as c:
        return c.execute("SELECT 1 FROM items LIMIT 1").fetchone() is None
