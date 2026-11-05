"""News aggregator: shows only stories reported by at least N of your feeds.

Run:  python3 app.py [--port 8000] [--db news.db] [--interval 15]
Then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feeds
import matching

STATIC_DIR = Path(__file__).parent / "static"
RETENTION_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    added_at     REAL NOT NULL,
    last_fetched REAL,
    last_error   TEXT,
    item_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    id         INTEGER PRIMARY KEY,
    feed_id    INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    guid       TEXT NOT NULL,
    title      TEXT NOT NULL,
    link       TEXT NOT NULL DEFAULT '',
    published  REAL NOT NULL,
    fetched_at REAL NOT NULL,
    UNIQUE (feed_id, guid)
);
CREATE INDEX IF NOT EXISTS items_published ON items(published);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def list_feeds(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM feeds ORDER BY title COLLATE NOCASE, url")]

    def get_feed(self, feed_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            return dict(r) if r else None

    def add_feed(self, url: str) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT INTO feeds (url, added_at) VALUES (?, ?)", (url, time.time()))
            return cur.lastrowid

    def delete_feed(self, feed_id: int) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM feeds WHERE id = ?", (feed_id,)).rowcount > 0

    def save_fetch(self, feed_id: int, parsed: feeds.ParsedFeed | None, error: str | None) -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            if parsed is not None:
                c.executemany(
                    """INSERT INTO items (feed_id, guid, title, link, published, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (feed_id, guid) DO UPDATE SET title = excluded.title, link = excluded.link""",
                    [(feed_id, e.guid, e.title, e.link, e.published, now) for e in parsed.entries],
                )
                c.execute(
                    """UPDATE feeds SET last_fetched = ?, last_error = NULL,
                       title = CASE WHEN ? != '' THEN ? ELSE title END,
                       item_count = (SELECT COUNT(*) FROM items WHERE feed_id = ?)
                       WHERE id = ?""",
                    (now, parsed.title, parsed.title, feed_id, feed_id),
                )
            else:
                c.execute("UPDATE feeds SET last_fetched = ?, last_error = ? WHERE id = ?", (now, error, feed_id))
            c.execute("DELETE FROM items WHERE published < ?", (now - RETENTION_DAYS * 86400,))

    def items_since(self, since: float) -> list[matching.Item]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT i.id, i.feed_id, i.title, i.link, i.published,
                          COALESCE(NULLIF(f.title, ''), f.url) AS feed_title
                   FROM items i JOIN feeds f ON f.id = i.feed_id
                   WHERE i.published >= ?""",
                (since,),
            ).fetchall()
        return [matching.Item(**dict(r)) for r in rows]


class Fetcher:
    def __init__(self, store: Store, interval_min: float):
        self.store = store
        self.interval = interval_min * 60
        self._wake = threading.Event()
        self._busy = threading.Lock()
        self.last_run: float | None = None

    def refresh_feed(self, feed: dict) -> None:
        try:
            parsed = feeds.fetch_and_parse(feed["url"])
            self.store.save_fetch(feed["id"], parsed, None)
        except feeds.FeedError as e:
            self.store.save_fetch(feed["id"], None, str(e))
        except Exception as e:  # noqa: BLE001
            self.store.save_fetch(feed["id"], None, f"Unexpected error: {e}")

    def refresh_all(self) -> None:
        if not self._busy.acquire(blocking=False):
            return  # a refresh is already running
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(self.refresh_feed, self.store.list_feeds()))
            self.last_run = time.time()
        finally:
            self._busy.release()

    @property
    def running(self) -> bool:
        return self._busy.locked()

    def trigger(self) -> None:
        self._wake.set()

    def loop(self) -> None:
        while True:
            self.refresh_all()
            self._wake.wait(self.interval)
            self._wake.clear()


def build_stories(store: Store, min_feeds: int, hours: float, threshold: float) -> dict:
    items = store.items_since(time.time() - hours * 3600)
    stories = [s for s in matching.cluster(items, threshold=threshold) if s.coverage >= min_feeds]
    stories.sort(key=lambda s: (-s.coverage, -s.latest))
    out = []
    for s in stories:
        # One entry per feed (a feed may have several near-duplicate items; keep the newest).
        per_feed: dict[int, matching.Item] = {}
        for it in sorted(s.items, key=lambda i: i.published):
            per_feed[it.feed_id] = it
        out.append(
            {
                "title": matching.representative_title(s),
                "coverage": s.coverage,
                "first_seen": s.earliest,
                "latest": s.latest,
                "sources": [
                    {"feed": it.feed_title, "title": it.title, "link": it.link, "published": it.published}
                    for it in sorted(per_feed.values(), key=lambda i: i.published)
                ],
            }
        )
    return {"stories": out, "items_considered": len(items)}


def make_handler(store: Store, fetcher: Fetcher):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsAggregator/1.0"

        def log_message(self, fmt, *args):  # quieter logs
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(min(n, 65536)))
            except json.JSONDecodeError:
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/feeds":
                self._json({"feeds": store.list_feeds(), "refreshing": fetcher.running, "last_run": fetcher.last_run})
            elif u.path == "/api/stories":
                try:
                    min_feeds = max(1, int(q.get("min_feeds", 2)))
                    hours = min(24 * 14, max(1.0, float(q.get("hours", 48))))
                    threshold = min(1.0, max(0.05, float(q.get("threshold", 0.4))))
                except ValueError:
                    return self._json({"error": "Invalid parameters"}, 400)
                self._json(build_stories(store, min_feeds, hours, threshold))
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/api/feeds":
                url = str(self._body().get("url", "")).strip()
                if not url.lower().startswith(("http://", "https://")):
                    return self._json({"error": "URL must start with http:// or https://"}, 400)
                # Validate before saving so typos are reported immediately.
                try:
                    parsed = feeds.fetch_and_parse(url)
                except feeds.FeedError as e:
                    return self._json({"error": str(e)}, 400)
                try:
                    feed_id = store.add_feed(url)
                except sqlite3.IntegrityError:
                    return self._json({"error": "That feed is already added"}, 409)
                store.save_fetch(feed_id, parsed, None)
                self._json({"feed": store.get_feed(feed_id)}, 201)
            elif u.path == "/api/refresh":
                fetcher.trigger()
                self._json({"ok": True}, 202)
            else:
                self._json({"error": "Not found"}, 404)

        def do_DELETE(self):
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "feeds"] and parts[2].isdigit():
                if store.delete_feed(int(parts[2])):
                    return self._json({"ok": True})
                return self._json({"error": "Not found"}, 404)
            self._json({"error": "Not found"}, 404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="news.db")
    ap.add_argument("--interval", type=float, default=15, help="minutes between automatic refreshes")
    ap.add_argument("--import", dest="import_file", metavar="FILE",
                    help="add feed URLs from a text file (one per line, # comments allowed) before starting")
    ap.add_argument("--import-only", action="store_true", help="exit after --import instead of starting the server")
    args = ap.parse_args()

    store = Store(args.db)
    if args.import_file:
        added = 0
        for line in Path(args.import_file).read_text().splitlines():
            url = line.split("#", 1)[0].strip()
            if url.lower().startswith(("http://", "https://")):
                try:
                    store.add_feed(url)
                    added += 1
                except sqlite3.IntegrityError:
                    pass
        print(f"Imported {added} new feed(s) from {args.import_file}")
    if args.import_only:
        return

    fetcher = Fetcher(store, args.interval)
    threading.Thread(target=fetcher.loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), make_handler(store, fetcher))
    print(f"News aggregator running at http://{args.host}:{args.port}  (db: {args.db})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
