"""News aggregator: shows only stories reported by at least N of your feeds.

Stories are split into verticals (Games, Movies & TV, Books, General News). Each vertical has
its own seed list (feeds/<vertical>.txt) and its own self-contained SQLite file
(data/<vertical>.db), so a vertical can be moved by copying one file.

Run:  python3 app.py [--port 8000] [--interval 15] [--backfill DAYS]
Then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feedly
import feeds
import matching

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
FEEDS_DIR = ROOT / "feeds"
RETENTION_DAYS = 14


@dataclass(frozen=True)
class Vertical:
    key: str
    label: str
    default_min_feeds: int


# Order = tab order. default_min_feeds: Overworld (games) never shows a story with < 4 sources.
VERTICALS = [
    Vertical("games", "Games", 4),
    Vertical("movies", "Movies & TV", 3),
    Vertical("books", "Books", 2),
    Vertical("news", "General News", 4),
]
VERTICALS_BY_KEY = {v.key: v for v in VERTICALS}

SCHEMA_VERSION = 4
SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL DEFAULT '',
    added_at     REAL NOT NULL,
    last_fetched REAL,
    last_error   TEXT,
    item_count   INTEGER NOT NULL DEFAULT 0,
    via          TEXT,         -- 'direct' or 'feedly' (how the last successful fetch got its items)
    name         TEXT,         -- display name from the seed file; falls back to the feed's own title
    weight       REAL          -- outlet prestige 1..5 (seed file "w=N", editable in the UI); NULL = default
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


@dataclass
class SeedFeed:
    name: str | None = None
    weight: float | None = None


def read_feed_list(path: Path) -> dict[str, SeedFeed]:
    """{url: SeedFeed} from a seed file: one URL per line, then an optional comment
    "# Display name | w=4 | free-form notes". The first field is the name; "w=N" is the weight."""
    feeds_ = {}
    for line in path.read_text().splitlines():
        url, _, comment = line.partition("#")
        url = url.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        fields = [f.strip() for f in comment.split("|")]
        weight = next((float(f[2:]) for f in fields[1:] if re.fullmatch(r"w=\d+(\.\d+)?", f)), None)
        feeds_[url] = SeedFeed(fields[0] or None, weight)
    return feeds_


class Store:
    def __init__(self, path: Path):
        self.path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(feeds)")}
            for col, typ in (("via", "TEXT"), ("name", "TEXT"), ("weight", "REAL")):  # added in v2 / v3 / v4
                if col not in cols:
                    c.execute(f"ALTER TABLE feeds ADD COLUMN {col} {typ}")
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    def list_feeds(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM feeds ORDER BY COALESCE(name, NULLIF(title, ''), url) COLLATE NOCASE")]

    def get_feed(self, feed_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            return dict(r) if r else None

    def add_feed(self, url: str, name: str | None = None, weight: float | None = None) -> int:
        with self._lock, self._conn() as c:
            return c.execute("INSERT INTO feeds (url, added_at, name, weight) VALUES (?, ?, ?, ?)",
                             (url, time.time(), name, weight)).lastrowid

    def add_feeds(self, feeds_: dict[str, SeedFeed]) -> int:
        added = 0
        for url, seed in feeds_.items():
            try:
                self.add_feed(url, seed.name, seed.weight)
                added += 1
            except sqlite3.IntegrityError:
                pass
        return added

    def sync_seed(self, feeds_: dict[str, SeedFeed]) -> None:
        """Apply seed-file names to existing feeds, and seed-file weights where none is set yet
        (a weight changed in the UI is kept)."""
        with self._lock, self._conn() as c:
            c.executemany("UPDATE feeds SET name = ? WHERE url = ?",
                          [(s.name, u) for u, s in feeds_.items() if s.name])
            c.executemany("UPDATE feeds SET weight = ? WHERE url = ? AND weight IS NULL",
                          [(s.weight, u) for u, s in feeds_.items() if s.weight is not None])

    def set_weight(self, feed_id: int, weight: float) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("UPDATE feeds SET weight = ? WHERE id = ?", (weight, feed_id)).rowcount > 0

    def delete_feed(self, feed_id: int) -> bool:
        with self._lock, self._conn() as c:
            return c.execute("DELETE FROM feeds WHERE id = ?", (feed_id,)).rowcount > 0

    def save_fetch(self, feed_id: int, parsed: feeds.ParsedFeed | None, error: str | None, via: str = "direct") -> None:
        now = time.time()
        with self._lock, self._conn() as c:
            if parsed is not None:
                c.executemany(
                    """INSERT INTO items (feed_id, guid, title, link, published, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT (feed_id, guid) DO UPDATE SET title = excluded.title, link = excluded.link""",
                    [(feed_id, e.guid, e.title, e.link, e.published, now) for e in parsed.entries if e.title],
                )
                c.execute(
                    """UPDATE feeds SET last_fetched = ?, last_error = ?, via = ?,
                       title = CASE WHEN ? != '' THEN ? ELSE title END,
                       item_count = (SELECT COUNT(*) FROM items WHERE feed_id = ?)
                       WHERE id = ?""",
                    (now, error, via, parsed.title, parsed.title, feed_id, feed_id),
                )
            else:
                c.execute("UPDATE feeds SET last_fetched = ?, last_error = ? WHERE id = ?", (now, error, feed_id))
            c.execute("DELETE FROM items WHERE published < ?", (now - RETENTION_DAYS * 86400,))

    def items_since(self, since: float) -> list[matching.Item]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT i.id, i.feed_id, i.title, i.link, i.published,
                          COALESCE(f.name, NULLIF(f.title, ''), f.url) AS feed_title,
                          COALESCE(f.weight, ?) AS weight
                   FROM items i JOIN feeds f ON f.id = i.feed_id
                   WHERE i.published >= ?""",
                (matching.DEFAULT_WEIGHT, since),
            ).fetchall()
        return [matching.Item(**dict(r)) for r in rows]


def _from_feedly(entries: list[dict], title: str = "") -> feeds.ParsedFeed:
    parsed = []
    for e in entries:
        guid, t, link, pub = feedly.entry_fields(e)
        if t:
            parsed.append(feeds.ParsedEntry(guid, feeds.clean_text(t), link, pub or time.time()))
    return feeds.ParsedFeed(title=title, entries=parsed)


def fetch_with_fallback(url: str) -> tuple[feeds.ParsedFeed, str, str | None]:
    """(parsed feed, via, note). Tries the feed directly, then Feedly's copy of it."""
    try:
        return feeds.fetch_and_parse(url), "direct", None
    except feeds.FeedError as direct_err:
        try:
            title, entries = feedly.recent(url)
        except Exception:  # noqa: BLE001
            raise direct_err
        if not entries:
            raise direct_err
        return _from_feedly(entries, title), "feedly", f"Direct fetch failed ({direct_err}); using Feedly's copy"


class Fetcher:
    def __init__(self, stores: dict[str, Store], interval_min: float):
        self.stores = stores
        self.interval = interval_min * 60
        self._wake = threading.Event()
        self._busy = threading.Lock()
        self.last_run: float | None = None

    @staticmethod
    def refresh_feed(store: Store, feed: dict) -> None:
        try:
            parsed, via, note = fetch_with_fallback(feed["url"])
            store.save_fetch(feed["id"], parsed, note, via)
        except feeds.FeedError as e:
            store.save_fetch(feed["id"], None, str(e))
        except Exception as e:  # noqa: BLE001
            store.save_fetch(feed["id"], None, f"Unexpected error: {e}")

    def refresh_all(self) -> None:
        if not self._busy.acquire(blocking=False):
            return  # a refresh is already running
        try:
            jobs = [(s, f) for s in self.stores.values() for f in s.list_feeds()]
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda job: self.refresh_feed(*job), jobs))
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


def backfill(stores: dict[str, Store], days: float) -> None:
    """Load the last `days` of history for every feed from Feedly's archive."""
    since = time.time() - min(days, RETENTION_DAYS) * 86400

    def one(job):
        key, store, feed = job
        try:
            entries = list(feedly.history(feed["url"], since=since))
        except Exception as e:  # noqa: BLE001
            return f"  {key}: {feed['url']}: Feedly error: {e}"
        if entries:
            store.save_fetch(feed["id"], _from_feedly(entries), feed.get("last_error"), feed.get("via") or "feedly")
        return f"  {key}: {len(entries):4d} items  {feed['title'] or feed['url']}"

    jobs = [(k, s, f) for k, s in stores.items() for f in s.list_feeds()]
    print(f"Backfilling {days:g} days for {len(jobs)} feeds from Feedly...")
    with ThreadPoolExecutor(max_workers=4) as pool:
        for line in pool.map(one, jobs):
            print(line)


def build_stories(store: Store, min_feeds: int, hours: float, threshold: float, min_score: float = 0) -> dict:
    items = store.items_since(time.time() - hours * 3600)
    stories = [s for s in matching.cluster(items, threshold=threshold)
               if s.coverage >= min_feeds and s.score >= min_score]
    stories.sort(key=lambda s: (-s.score, -s.coverage, -s.latest))
    out = []
    for s in stories:
        # One entry per feed (a feed may have several near-duplicate items; keep the newest).
        per_feed: dict[int, matching.Item] = {}
        for it in sorted(s.items, key=lambda i: i.published):
            per_feed[it.feed_id] = it
        lead = matching.lead_item(s)
        per_feed[lead.feed_id] = lead  # show the lead outlet's lead headline, not its newest item
        out.append(
            {
                "title": lead.title,
                "lead": {"feed": lead.feed_title, "link": lead.link, "weight": lead.weight},
                "coverage": s.coverage,
                "score": s.score,
                "first_seen": s.earliest,
                "latest": s.latest,
                "sources": [
                    {"feed": it.feed_title, "title": it.title, "link": it.link, "published": it.published,
                     "weight": it.weight}
                    for it in sorted(per_feed.values(), key=lambda i: (-i.weight, i.published))
                ],
            }
        )
    return {"stories": out, "items_considered": len(items)}


def make_handler(stores: dict[str, Store], fetcher: Fetcher):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsAggregator/2.0"

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

        def _route(self) -> tuple[list[str], dict, Store | None]:
            """/api/<vertical>/<rest...> -> (rest parts, query, store)."""
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = u.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "api" and parts[1] in stores:
                return parts[2:], q, stores[parts[1]]
            return parts, q, None

        def do_GET(self):
            rest, q, store = self._route()
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/verticals":
                self._json({
                    "verticals": [{"key": v.key, "label": v.label, "default_min_feeds": v.default_min_feeds}
                                  for v in VERTICALS if v.key in stores],
                    "default_threshold": matching.DEFAULT_THRESHOLD,
                    "default_weight": matching.DEFAULT_WEIGHT,
                })
            elif store and rest == ["feeds"]:
                self._json({"feeds": store.list_feeds(), "refreshing": fetcher.running, "last_run": fetcher.last_run})
            elif store and rest == ["stories"]:
                try:
                    min_feeds = max(1, int(q.get("min_feeds", 2)))
                    hours = min(24 * RETENTION_DAYS, max(1.0, float(q.get("hours", 48))))
                    threshold = min(1.0, max(0.02, float(q.get("threshold", matching.DEFAULT_THRESHOLD))))
                    min_score = max(0.0, float(q.get("min_score", 0) or 0))
                except ValueError:
                    return self._json({"error": "Invalid parameters"}, 400)
                self._json(build_stories(store, min_feeds, hours, threshold, min_score))
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            rest, _, store = self._route()
            if store and rest == ["feeds"]:
                url = str(self._body().get("url", "")).strip()
                if not url.lower().startswith(("http://", "https://")):
                    return self._json({"error": "URL must start with http:// or https://"}, 400)
                # Validate before saving so typos are reported immediately.
                try:
                    parsed, via, note = fetch_with_fallback(url)
                except feeds.FeedError as e:
                    return self._json({"error": str(e)}, 400)
                try:
                    feed_id = store.add_feed(url)
                except sqlite3.IntegrityError:
                    return self._json({"error": "That feed is already added"}, 409)
                store.save_fetch(feed_id, parsed, note, via)
                self._json({"feed": store.get_feed(feed_id)}, 201)
            elif urlparse(self.path).path == "/api/refresh":
                fetcher.trigger()
                self._json({"ok": True}, 202)
            else:
                self._json({"error": "Not found"}, 404)

        def do_PATCH(self):
            rest, _, store = self._route()
            if store and len(rest) == 2 and rest[0] == "feeds" and rest[1].isdigit():
                try:
                    weight = float(self._body().get("weight"))
                except (TypeError, ValueError):
                    return self._json({"error": "weight must be a number"}, 400)
                if not 0 < weight <= 10:
                    return self._json({"error": "weight must be between 0 and 10"}, 400)
                if store.set_weight(int(rest[1]), weight):
                    return self._json({"feed": store.get_feed(int(rest[1]))})
            self._json({"error": "Not found"}, 404)

        def do_DELETE(self):
            rest, _, store = self._route()
            if store and len(rest) == 2 and rest[0] == "feeds" and rest[1].isdigit():
                if store.delete_feed(int(rest[1])):
                    return self._json({"ok": True})
            self._json({"error": "Not found"}, 404)

    return Handler


def open_stores(data_dir: Path, seed: bool = True) -> dict[str, Store]:
    """One Store per vertical. A brand-new (empty) vertical is seeded from feeds/<key>.txt."""
    stores = {}
    for v in VERTICALS:
        store = Store(data_dir / f"{v.key}.db")
        seed_file = FEEDS_DIR / f"{v.key}.txt"
        if seed and seed_file.exists():
            seed_feeds = read_feed_list(seed_file)
            if not store.list_feeds():
                n = store.add_feeds(seed_feeds)
                print(f"Seeded {v.label}: {n} feeds from {seed_file.relative_to(ROOT)}")
            store.sync_seed(seed_feeds)
        stores[v.key] = store
    return stores


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=str(ROOT / "data"), help="directory holding one <vertical>.db per vertical")
    ap.add_argument("--interval", type=float, default=15, help="minutes between automatic refreshes")
    ap.add_argument("--backfill", type=float, metavar="DAYS",
                    help=f"load the last DAYS (max {RETENTION_DAYS}) of history for every feed from Feedly first")
    ap.add_argument("--import", dest="import_file", metavar="FILE", help="add feed URLs from FILE to --vertical")
    ap.add_argument("--vertical", choices=list(VERTICALS_BY_KEY), default="games", help="vertical for --import")
    ap.add_argument("--no-serve", action="store_true", help="exit after seeding/--import/--backfill")
    args = ap.parse_args()

    stores = open_stores(Path(args.data))
    if args.import_file:
        n = stores[args.vertical].add_feeds(read_feed_list(Path(args.import_file)))
        print(f"Imported {n} new feed(s) into {args.vertical}")
    if args.backfill:
        backfill(stores, args.backfill)
    if args.no_serve:
        return

    fetcher = Fetcher(stores, args.interval)
    threading.Thread(target=fetcher.loop, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(stores, fetcher))
    print(f"News aggregator running at http://{args.host}:{args.port}  (data: {args.data})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
