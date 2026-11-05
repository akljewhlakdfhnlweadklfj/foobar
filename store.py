"""SQLite storage: one self-contained database file per vertical.

Schema history (PRAGMA user_version): v1 feeds+items; v2 feeds.via; v3 feeds.name; v4 feeds.weight;
v5 feeds.enabled; v6 feeds.backfilled_since + items.description; v7 feeds.last_run + feeds.required +
phrases table; v8 settings table; v9 feeds.only (keyword filter); v10 items.image.
Older files are upgraded in place when opened.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import feeds
import matching

RETENTION_DAYS = 30
SCHEMA_VERSION = 12
MAX_WEIGHT = 10.0
MAX_BONUS = 100.0
DEFAULT_BONUS = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    added_at     REAL NOT NULL,
    last_fetched REAL,
    last_error   TEXT,
    item_count   INTEGER NOT NULL DEFAULT 0,
    via          TEXT,              -- 'direct' or 'feedly' (how the last successful fetch got its items)
    name         TEXT,              -- display name from the seed file; falls back to the feed's own title
    weight       REAL,              -- outlet prestige 0..10, seeds use 1..5; NULL = default
    enabled      INTEGER NOT NULL DEFAULT 1,  -- 0 = off by default in filter groups (still fetched)
    backfilled_since REAL,          -- oldest point Feedly history has been loaded back to
    last_run     TEXT,              -- id of the last scrape run that finished this feed (for resuming)
    required     INTEGER NOT NULL DEFAULT 0, -- unused since filter groups (seeded into them once)
    only         TEXT               -- comma-separated keywords: only use items mentioning one (NULL = all)
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    feed_id     INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid        TEXT NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL DEFAULT '',
    published   REAL NOT NULL,
    fetched_at  REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    image       TEXT NOT NULL DEFAULT '',
    views       INTEGER,                    -- view count (YouTube videos), when known
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS items_published ON items(published);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL            -- JSON
);
CREATE TABLE IF NOT EXISTS phrases (
    id       INTEGER PRIMARY KEY,
    phrase   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    bonus    REAL NOT NULL DEFAULT 5,
    added_at REAL NOT NULL
);
"""

# Columns added after v1, for upgrading older files in place.
_ADDED_COLUMNS = {
    "feeds": [("via", "TEXT"), ("name", "TEXT"), ("weight", "REAL"), ("enabled", "INTEGER NOT NULL DEFAULT 1"),
              ("backfilled_since", "REAL"), ("last_run", "TEXT"), ("required", "INTEGER NOT NULL DEFAULT 0"),
              ("only", "TEXT")],
    "items": [("description", "TEXT NOT NULL DEFAULT ''"), ("image", "TEXT NOT NULL DEFAULT ''"), ("views", "INTEGER")],
}


@dataclass
class SeedFeed:
    name: str | None = None
    weight: float | None = None
    only: str | None = None


def keyword_regex(only: str | None) -> re.Pattern | None:
    """Regex for a feed's keyword filter ("manga, light novel"): case-insensitive, each keyword
    matched at a word start (so "manga" also matches "mangaka"); any whitespace inside a phrase."""
    words = [w.strip() for w in (only or "").split(",") if w.strip()]
    if not words:
        return None
    alts = "|".join(r"\s+".join(re.escape(p) for p in w.split()) for w in words)
    return re.compile(rf"(?<!\w)(?:{alts})", re.I)


def read_feed_list(path: Path) -> dict[str, SeedFeed]:
    """{url: SeedFeed} from a seed file: one URL per line, then an optional comment
    "# Display name | w=4 | only=manga, manhwa | free-form notes". The first field is the name;
    "w=N" is the weight; "only=..." limits the feed to items mentioning one of those keywords."""
    feeds_ = {}
    for line in path.read_text().splitlines():
        url, _, comment = line.partition("#")
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        fields = [f.strip() for f in comment.split("|")]
        weight = next((float(f[2:]) for f in fields[1:] if re.fullmatch(r"w=\d+(\.\d+)?", f)), None)
        only = next((f[5:].strip() for f in fields[1:] if f.startswith("only=")), None)
        feeds_[url] = SeedFeed(fields[0] or None, weight, only or None)
    return feeds_


class Store:
    def __init__(self, path: Path):
        self.path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.version = 0  # bumped on every write; lets callers cache derived results
        self.catalog_version = 0  # bumped when the outlet list, names, weights or defaults change
        with self._conn() as c:
            prior = c.execute("PRAGMA user_version").fetchone()[0]
            self.is_new = prior == 0 and not c.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'feeds'").fetchone()
            c.executescript(SCHEMA)
            for table, cols in _ADDED_COLUMNS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols:
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            # Earlier versions stored placeholder descriptions some feeds send literally.
            c.execute("UPDATE items SET description = '' "
                      "WHERE lower(trim(description)) IN ('null', 'none', 'undefined', 'nan')")
            if 0 < prior < 11:  # v11 dropped the subreddit feeds (throttled, mostly discussion)
                c.execute("DELETE FROM items WHERE feed_id IN (SELECT id FROM feeds WHERE url LIKE '%reddit.com/%')")
                c.execute("DELETE FROM feeds WHERE url LIKE '%reddit.com/%'")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits (or rolls back) and is always closed."""
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn() as c:
            yield c
        self.version += 1

    @contextmanager
    def _catalog_write(self) -> Iterator[sqlite3.Connection]:
        with self._write() as c:
            yield c
        self.catalog_version += 1

    # --- feeds ---------------------------------------------------------------------------------

    def list_feeds(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM feeds ORDER BY COALESCE(name, NULLIF(title, ''), url) COLLATE NOCASE")]

    def get_feed(self, feed_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            return dict(r) if r else None

    def add_feed(self, url: str, name: str | None = None, weight: float | None = None,
                 only: str | None = None) -> int:
        with self._catalog_write() as c:
            return c.execute("INSERT INTO feeds (url, added_at, name, weight, only) VALUES (?, ?, ?, ?, ?)",
                             (url, time.time(), name, weight, only)).lastrowid

    def add_feeds(self, feeds_: dict[str, SeedFeed]) -> int:
        added = 0
        for url, seed in feeds_.items():
            try:
                self.add_feed(url, seed.name, seed.weight, seed.only)
                added += 1
            except sqlite3.IntegrityError:
                pass
        return added

    def sync_seed(self, feeds_: dict[str, SeedFeed]) -> None:
        """Apply seed-file names to existing feeds, and seed-file weights where none is set yet
        (a weight changed in the UI is kept)."""
        with self._catalog_write() as c:
            c.executemany("UPDATE feeds SET name = ? WHERE url = ?",
                          [(s.name, u) for u, s in feeds_.items() if s.name])
            c.executemany("UPDATE feeds SET weight = ? WHERE url = ? AND weight IS NULL",
                          [(s.weight, u) for u, s in feeds_.items() if s.weight is not None])
            c.executemany("UPDATE feeds SET only = ? WHERE url = ? AND only IS NULL",
                          [(s.only, u) for u, s in feeds_.items() if s.only])

    def update_feed(self, feed_id: int, weight: float | None = None, enabled: bool | None = None,
                    only: str | None = None) -> bool:
        """Catalog defaults: weight, enabled (on by default in groups) and the keyword filter
        (only="" clears it; None leaves it unchanged)."""
        sets, args = [], []
        if only is not None:
            sets.append("only = ?"); args.append(only.strip() or None)
        if weight is not None:
            sets.append("weight = ?"); args.append(weight)
        if enabled is not None:
            sets.append("enabled = ?"); args.append(int(enabled))
        if not sets:
            return self.get_feed(feed_id) is not None
        with self._catalog_write() as c:
            return c.execute(f"UPDATE feeds SET {', '.join(sets)} WHERE id = ?", (*args, feed_id)).rowcount > 0

    def set_all_enabled(self, enabled: bool) -> int:
        with self._catalog_write() as c:
            return c.execute("UPDATE feeds SET enabled = ?", (int(enabled),)).rowcount

    def delete_feed(self, feed_id: int) -> bool:
        with self._catalog_write() as c:
            return c.execute("DELETE FROM feeds WHERE id = ?", (feed_id,)).rowcount > 0

    def mark_backfilled(self, feed_id: int, since: float) -> None:
        with self._write() as c:
            c.execute("UPDATE feeds SET backfilled_since = MIN(COALESCE(backfilled_since, ?), ?) WHERE id = ?",
                      (since, since, feed_id))

    def mark_run(self, feed_id: int, run_id: str) -> None:
        with self._write() as c:
            c.execute("UPDATE feeds SET last_run = ? WHERE id = ?", (run_id, feed_id))

    # --- items ---------------------------------------------------------------------------------

    def save_fetch(self, feed_id: int, parsed: feeds.ParsedFeed | None, error: str | None, via: str = "direct") -> None:
        now = time.time()
        with self._write() as c:
            c.execute("DELETE FROM items WHERE published < ?", (now - RETENTION_DAYS * 86400,))
            if parsed is None:
                c.execute("UPDATE feeds SET last_fetched = ?, last_error = ? WHERE id = ?", (now, error, feed_id))
                return
            c.executemany(
                """INSERT INTO items (feed_id, guid, title, link, published, fetched_at, description, image, views)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (feed_id, guid) DO UPDATE SET title = excluded.title, link = excluded.link,
                       description = CASE WHEN excluded.description != '' THEN excluded.description
                                          ELSE items.description END,
                       image = CASE WHEN excluded.image != '' THEN excluded.image ELSE items.image END,
                       views = COALESCE(excluded.views, items.views)""",
                [(feed_id, e.guid, e.title, e.link, min(e.published, now), now, e.description, e.image, e.views)
                 for e in parsed.entries if e.title and e.published >= now - RETENTION_DAYS * 86400],
            )
            c.execute(
                """UPDATE feeds SET last_fetched = ?, last_error = ?, via = ?,
                   title = CASE WHEN ? != '' THEN ? ELSE title END,
                   item_count = (SELECT COUNT(*) FROM items WHERE feed_id = ?)
                   WHERE id = ?""",
                (now, error, via, parsed.title, parsed.title, feed_id, feed_id),
            )

    def items_since(self, since: float, include_disabled: bool = False) -> list[matching.Item]:
        """Items published at or after `since`, from switched-on feeds (or all feeds), with each feed's
        keyword filter applied."""
        enabled_sql = "" if include_disabled else " AND f.enabled = 1"
        with self._conn() as c:
            filters = {r["id"]: keyword_regex(r["only"]) for r in
                       c.execute("SELECT id, only FROM feeds WHERE only IS NOT NULL")}
            rows = c.execute(
                """SELECT i.id, i.feed_id, i.title, i.link, i.published, i.description, i.image, i.views,
                          COALESCE(f.name, NULLIF(f.title, ''), f.url) AS feed_title,
                          COALESCE(f.weight, ?) AS weight
                   FROM items i JOIN feeds f ON f.id = i.feed_id
                   WHERE i.published >= ?""" + enabled_sql,
                (matching.DEFAULT_WEIGHT, since),
            ).fetchall()
        items = []
        for r in rows:
            rx = filters.get(r["feed_id"])
            if rx is None or rx.search(r["title"]) or rx.search(r["description"]):
                items.append(matching.Item(**dict(r)))
        return items

    # --- settings (only match strictness is used now; older versions kept story filters here) ---------------------------------------------

    def get_settings(self) -> dict:
        with self._conn() as c:
            return {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}

    def set_settings(self, values: dict) -> None:
        with self._write() as c:
            c.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                          [(k, json.dumps(v)) for k, v in values.items()])

    def reset_settings(self) -> None:
        with self._write() as c:
            c.execute("DELETE FROM settings")
