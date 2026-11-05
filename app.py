"""News aggregator: shows only stories reported by at least N of your feeds.

Stories are split into verticals (Games, Movies & TV, Books, General News, Consumer Tech, Anime, Manga). Each vertical has
its own seed list (feeds/<vertical>.txt) and its own self-contained SQLite file
(data/<vertical>.db), so a vertical can be moved by copying one file.

Run:  python3 app.py [--port 8000] [--interval 15] [--backfill DAYS]
Then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import threading
from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import ai as ai_mod
import digest as digests_mod
import groups as groups_mod
import feeds
import matching
import seedcache
import sources
import youtube
from scraper import Scraper
from store import DEFAULT_BONUS, MAX_BONUS, MAX_WEIGHT, RETENTION_DAYS, Store, read_feed_list

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
FEEDS_DIR = ROOT / "feeds"
MAX_BODY = 64 * 1024


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
    Vertical("tech", "Consumer Tech", 4),
    Vertical("anime", "Anime", 3),
    Vertical("manga", "Manga", 2),
]
VERTICALS_BY_KEY = {v.key: v for v in VERTICALS}


# --- per-vertical match strictness ----------------------------------------------------------

# The only story setting a vertical keeps (store.settings): how similar headlines must be to be grouped.
# Everything about which stories show is per filter group (groups.py).
THRESHOLD_RANGE = (0.02, 1.0)


def threshold(store: Store) -> float:
    val = store.get_settings().get("threshold")
    lo, hi = THRESHOLD_RANGE
    if isinstance(val, (int, float)) and not isinstance(val, bool) and lo <= val <= hi:
        return float(val)
    return matching.DEFAULT_THRESHOLD


def earlier_settings(v: Vertical, store: Store) -> dict:
    """A vertical's story settings saved by earlier versions, as filter-group settings (for seeding the
    first groups; values that don't fit are skipped)."""
    out = {**groups_mod.DEFAULT_SETTINGS, "min_feeds": v.default_min_feeds}
    for k, val in store.get_settings().items():
        if k in groups_mod.SETTING_SPECS:
            try:
                out[k] = groups_mod.coerce_setting(k, val)
            except ValueError:
                pass
    return out


# --- HTTP ------------------------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
DEFAULT_ALLOWED_SUFFIXES = (".app.github.dev", ".githubpreview.dev")


@dataclass
class Security:
    """Protection for requests that change things (POST/PATCH/DELETE).

    - Without a valid API token: the body must be JSON (blocks cross-site "simple" form/text posts),
      a browser Origin header must match the Host, and the Host must be allowed (blocks DNS
      rebinding). Allowed hosts: localhost, Codespaces forwarding domains, and --allow-host values
      ("*" allows any host).
    - With "Authorization: Bearer <token>" matching --api-token: allowed from anywhere, for
      external orchestrators.
    """
    api_token: str | None = None
    allowed_hosts: tuple[str, ...] = ()

    def host_allowed(self, host: str) -> bool:
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        name = name.lower()
        if "*" in self.allowed_hosts:
            return True
        return (name in DEFAULT_ALLOWED_HOSTS or name in self.allowed_hosts
                or any(name.endswith(s) for s in DEFAULT_ALLOWED_SUFFIXES))

    def token_ok(self, header: str | None) -> bool:
        if not self.api_token or not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.api_token.encode())


def make_digests(stores: dict[str, Store], out_dir: Path | None = None, log=None, workers: int = 0) -> digests_mod.Digests:
    """Shared digests for every vertical (see digest.py). With workers > 0, grouping runs in that many
    worker processes, so the server keeps answering at full speed while it runs."""
    pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) if workers else None
    broken: list[bool] = []

    def builder(key: str, window: int) -> dict:
        store = stores[key]
        args = (store.path, key, VERTICALS_BY_KEY[key].label, window, threshold(store))
        if pool is not None and not broken:
            try:
                return pool.submit(digests_mod.build_from_file, *args).result()
            except BrokenProcessPool:
                broken.append(True)               # workers can't start here: build in this process instead
                (log or print)("digest workers unavailable; building in the server process")
        return digests_mod.build_from_file(*args)

    def stamp(key: str):
        return stores[key].items_version, threshold(stores[key])
    return digests_mod.Digests(out_dir, builder, list(stores), log=log, stamp=stamp)


def seed_groups(group_store: groups_mod.GroupStore, stores: dict[str, Store]) -> int:
    """One filter group per vertical, the first time the vertical exists (on a first start, or when a
    vertical is added later), from that vertical's earlier story settings. Returns how many were made."""
    made = 0
    for v in VERTICALS:
        if v.key not in stores or group_store.was_seeded(v.key):
            continue
        if group_store.upgraded and not stores[v.key].is_new:
            group_store.mark_seeded(v.key)      # existed before groups were tracked: its group was deleted on purpose
            continue
        settings = earlier_settings(v, stores[v.key])
        # Outlets marked "required" in the earlier design become this group's own setting.
        required = {str(f["id"]): {"req": True} for f in stores[v.key].list_feeds() if f.get("required")}
        group_store.create(groups_mod.clean({"name": v.label, "verticals": [v.key], "settings": settings,
                                             "outlets": {v.key: required} if required else {}}, None, list(stores)))
        group_store.mark_seeded(v.key)
        made += 1
    return made


def make_handler(stores: dict[str, Store], scraper: Scraper, security: Security | None = None,
                 digests: digests_mod.Digests | None = None, group_store: groups_mod.GroupStore | None = None,
                 summarizer: ai_mod.Summarizer | None = None):
    security = security or Security()
    digests = digests or make_digests(stores)
    if group_store is None:
        group_store = groups_mod.GroupStore(Path(tempfile.mkdtemp()) / "groups.db")
        seed_groups(group_store, stores)
    summarizer = summarizer or ai_mod.Summarizer(None, enabled=False)
    views = groups_mod.Views(group_store, digests, stores, summarizer)
    seen_hosts: set[str] = set()
    page_cache: dict = {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsAggregator/3.0"
        timeout = 30  # seconds a client may take to send its request

        def log_message(self, fmt, *args):  # quieter logs
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            raw_len = self.headers.get("Content-Length")
            if not raw_len:
                return {}
            try:
                n = int(raw_len)
            except ValueError:
                raise HTTPError(400, "Invalid Content-Length")
            if n < 0:
                raise HTTPError(400, "Invalid Content-Length")
            if n > MAX_BODY:
                raise HTTPError(413, f"Request body larger than {MAX_BODY // 1024} KB")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPError(400, "Body must be JSON")
            if not isinstance(data, dict):
                raise HTTPError(400, "Body must be a JSON object")
            return data

        def _note_host(self):
            host = self.headers.get("Host", "")
            if host not in seen_hosts and len(seen_hosts) < 50:
                seen_hosts.add(host)
                print(f"[http] first request for Host: {host!r}", file=sys.stderr, flush=True)

        def _guard_write(self):
            """Raise HTTPError unless this state-changing request is allowed."""
            if security.token_ok(self.headers.get("Authorization")):
                return
            host = self.headers.get("Host", "")
            if not security.host_allowed(host):
                raise HTTPError(403, f"Changes aren't allowed through host {host!r}. Start the app with "
                                     f"--allow-host {host.rsplit(':', 1)[0]} (or use an API token).")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != host.lower():
                raise HTTPError(403, "Cross-site request refused")
            # Browsers can send a cross-site POST without asking first (a "simple" request), but only
            # with a form or text content type; requiring JSON blocks that. PATCH/DELETE always need a
            # CORS preflight, which this server never grants, so they don't need the check.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.command == "POST" and ctype != "application/json":
                raise HTTPError(415, "Send changes as Content-Type: application/json")

        def _send_digest(self, entry: dict, max_age: int = 60):
            """A shared digest or group view: same bytes for everyone, so it's cacheable (ETag, gzip, max-age)."""
            if self.headers.get("If-None-Match") == entry["etag"]:
                self.send_response(304)
                self.send_header("ETag", entry["etag"])
                self.end_headers()
                return
            gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
            body = entry["gz"] if gz else gzip.decompress(entry["gz"])      # only non-browser clients
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("ETag", entry["etag"])
            self.send_header("Cache-Control", f"public, max-age={max_age}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_page(self):
            """The page, gzipped and cached until the file changes."""
            f = STATIC_DIR / "index.html"
            mtime = f.stat().st_mtime
            if page_cache.get("mtime") != mtime:
                body = f.read_bytes()
                page_cache.update(mtime=mtime, json=body, gz=gzip.compress(body, 9),
                                  etag='"%s"' % hashlib.sha1(body).hexdigest()[:16])
            if self.headers.get("If-None-Match") == page_cache["etag"]:
                self.send_response(304)
                self.send_header("ETag", page_cache["etag"])
                self.end_headers()
                return
            gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
            body = page_cache["gz"] if gz else page_cache["json"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("ETag", page_cache["etag"])
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _vertical_key(self) -> str:
            return urlparse(self.path).path.strip("/").split("/")[1]

        def _route(self) -> tuple[list[str], dict, Store | None]:
            """/api/<vertical>/<rest...> -> (rest parts, query, store)."""
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = u.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "api" and parts[1] in stores:
                return parts[2:], q, stores[parts[1]]
            return parts, q, None

        def _handle(self, method: str):
            self._note_host()
            try:
                if method != "GET":
                    self._guard_write()
                getattr(self, "_" + method)()
            except HTTPError as e:
                self._json({"error": str(e)}, e.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - answer instead of dropping the connection
                print(f"[http] {method} {self.path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                self._json({"error": "Internal error"}, 500)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        # --- GET ---

        def _GET(self):
            rest, q, store = self._route()
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send_page()
            elif path == "/api/verticals":
                self._json({
                    "verticals": [{"key": v.key, "label": v.label} for v in VERTICALS if v.key in stores],
                    "default_weight": matching.DEFAULT_WEIGHT, "max_weight": MAX_WEIGHT,
                    "default_bonus": DEFAULT_BONUS, "max_bonus": MAX_BONUS, "retention_days": RETENTION_DAYS,
                })
            elif path == "/api/groups":
                self._json({"groups": group_store.list(), "ai": summarizer.status()})
            elif len(rest) == 4 and rest[:2] == ["api", "groups"] and rest[2].isdigit() and rest[3] == "stories":
                entry = views.get(int(rest[2]))
                if entry is None:
                    raise HTTPError(404, "No such group")
                self._send_digest(entry, max_age=30)
            elif len(rest) == 4 and rest[:2] == ["api", "groups"] and rest[2].isdigit() and rest[3] == "extras":
                entry = views.extras(int(rest[2]))        # stories below the group's minimums (watched phrases)
                if entry is None:
                    raise HTTPError(404, "No such group")
                self._send_digest(entry, max_age=30)
            elif path == "/api/scrape/status":
                self._json(scraper.status())
            elif store and rest == ["digest"]:
                try:
                    hours = float(q.get("hours", digests_mod.WINDOWS[0]))
                except ValueError:
                    raise HTTPError(400, "hours must be a number")
                self._send_digest(digests.get(self._vertical_key(), hours))
            elif store and rest == ["feeds"]:
                self._json({"feeds": store.list_feeds()})
            elif store and rest == ["settings"]:
                key = self._vertical_key()
                self._json({"threshold": threshold(store), "rebuilding": digests.rebuilding(key),
                            "grouped_at": digests.data(key, digests_mod.WINDOWS[0])[1]["generated_at"],
                            "grouped_threshold": digests.data(key, digests_mod.WINDOWS[0])[1]["threshold"]})
            else:
                raise HTTPError(404, "Not found")

        # --- POST ---

        def _POST(self):
            rest, _, store = self._route()
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/scrape/start":
                kind = body.get("kind", "refresh")
                days = body.get("days")
                verticals = body.get("verticals")
                if verticals is not None and (not isinstance(verticals, list)
                                              or not all(isinstance(v, str) for v in verticals)):
                    raise HTTPError(400, "verticals must be a list of vertical keys")
                if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float))):
                    raise HTTPError(400, "days must be a number")
                ok, res = scraper.start(kind, verticals, days, resume=body.get("resume", True) is not False)
                if not ok:
                    raise HTTPError(409 if "already running" in res else 400, res)
                return self._json(res, 202)
            if path == "/api/scrape/stop":
                ok, res = scraper.stop()
                return self._json(res if ok else {**scraper.status(), "message": res}, 202 if ok else 200)
            if path == "/api/scrape/schedule":
                auto, interval = body.get("auto"), body.get("interval_minutes")
                if auto is not None and not isinstance(auto, bool):
                    raise HTTPError(400, "auto must be true or false")
                if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float))
                                             or not 1 <= interval <= 24 * 60):
                    raise HTTPError(400, "interval_minutes must be between 1 and 1440")
                return self._json(scraper.set_schedule(auto, interval))
            if path == "/api/groups":
                try:
                    cfg = groups_mod.clean(body, None, list(stores))
                except ValueError as e:
                    raise HTTPError(400, str(e))
                return self._json({"group": group_store.get(group_store.create(cfg))}, 201)
            if store and rest == ["feeds"]:
                if not isinstance(body.get("url", ""), str):
                    raise HTTPError(400, "url must be text")
                url = body.get("url", "").strip()
                try:
                    url = youtube.feed_url_for(url) or url          # a YouTube channel link or @handle
                except feeds.FeedError as e:
                    raise HTTPError(400, str(e))
                if not url.lower().startswith(("http://", "https://")):
                    raise HTTPError(400, "URL must start with http:// or https:// (or be a YouTube @handle)")
                try:
                    parsed, via, note = sources.fetch(url)  # validate before saving
                except feeds.FeedError as e:
                    raise HTTPError(400, str(e))
                try:
                    feed_id = store.add_feed(url)
                except sqlite3.IntegrityError:
                    raise HTTPError(409, "That feed is already added")
                group_store.forget_outlet(self._vertical_key(), feed_id)   # an id can be reused after a delete
                store.save_fetch(feed_id, parsed, None, via, note)
                digests.request_rebuild([self._vertical_key()])             # new headlines to group
                return self._json({"feed": store.get_feed(feed_id)}, 201)
            raise HTTPError(404, "Not found")

        @staticmethod
        def _number(value, name: str, lo: float, hi: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise HTTPError(400, f"{name} must be a number")
            if not lo <= value <= hi:
                raise HTTPError(400, f"{name} must be between {lo:g} and {hi:g}")
            return float(value)

        # --- PATCH ---

        def _PATCH(self):
            """/api/groups/<id> {...}                          - change a filter group (see groups.clean).
            /api/<v>/settings {"threshold": 0.02..1}          - match strictness ({"reset": true} = default).
            /api/<v>/feeds/<id> {"weight": 0..10, "enabled": bool, "only": "manga, light novel"}
                                                              - catalog defaults and keyword filter ("" clears)."""
            rest, _, store = self._route()
            body = self._body()
            if len(rest) == 3 and rest[:2] == ["api", "groups"] and rest[2].isdigit():
                try:
                    group = group_store.change(int(rest[2]), body, list(stores))
                except ValueError as e:
                    raise HTTPError(400, str(e))
                if group is None:
                    raise HTTPError(404, "No such group")
                return self._json({"group": group})
            if store and rest == ["settings"]:
                before = threshold(store)
                if body.get("reset") is True:
                    store.reset_settings()
                elif set(body) == {"threshold"}:
                    store.set_settings({"threshold": self._number(body["threshold"], "threshold", *THRESHOLD_RANGE)})
                else:
                    raise HTTPError(400, 'send {"threshold": number} or {"reset": true}')
                changed = threshold(store) != before
                if changed:
                    digests.request_rebuild([self._vertical_key()])         # stories are grouped differently
                return self._json({"threshold": threshold(store), "rebuilding": changed})
            if store and len(rest) == 2 and rest[0] == "feeds" and rest[1].isdigit():
                enabled, only = body.get("enabled"), body.get("only")
                if only is not None and (not isinstance(only, str) or len(only) > 500):
                    raise HTTPError(400, "only must be text: comma-separated keywords (empty to clear)")
                if enabled is not None and not isinstance(enabled, bool):
                    raise HTTPError(400, "enabled must be true or false")
                weight = self._number(body["weight"], "weight", 0, MAX_WEIGHT) if "weight" in body else None
                if weight is None and enabled is None and only is None:
                    raise HTTPError(400, "send weight, enabled and/or only")
                if store.update_feed(int(rest[1]), weight=weight, enabled=enabled, only=only):
                    if only is not None:                                    # changes which headlines are grouped
                        digests.request_rebuild([self._vertical_key()])
                    return self._json({"feed": store.get_feed(int(rest[1]))})
            raise HTTPError(404, "Not found")

        # --- DELETE ---

        def _DELETE(self):
            rest, _, store = self._route()
            if len(rest) == 3 and rest[:2] == ["api", "groups"] and rest[2].isdigit():
                try:
                    if group_store.delete(int(rest[2])):
                        return self._json({"ok": True})
                except ValueError as e:
                    raise HTTPError(400, str(e))
                raise HTTPError(404, "No such group")
            if store and len(rest) == 2 and rest[1].isdigit():
                if rest[0] == "feeds" and store.delete_feed(int(rest[1])):
                    group_store.forget_outlet(self._vertical_key(), int(rest[1]))
                    digests.request_rebuild([self._vertical_key()])
                    return self._json({"ok": True})
            raise HTTPError(404, "Not found")

    return Handler


class Server(ThreadingHTTPServer):
    request_queue_size = 1024     # the default (5) drops connections when many viewers poll at once
    daemon_threads = True


# --- setup -----------------------------------------------------------------------------------

def open_stores(data_dir: Path, seed: bool = True) -> dict[str, Store]:
    """One Store per vertical. A brand-new database is seeded from feeds/<key>.txt; after that,
    seed files only update display names (and weights of feeds that have none). A vertical with no
    headlines yet loads seed_cache/<key>.jsonl.gz if there is one (see seedcache.py)."""
    stores = {}
    for v in VERTICALS:
        store = Store(data_dir / f"{v.key}.db")
        seed_file = FEEDS_DIR / f"{v.key}.txt"
        if seed and seed_file.exists():
            seed_feeds = read_feed_list(seed_file)
            if store.is_new:
                n = store.add_feeds(seed_feeds)
                print(f"Seeded {v.label}: {n} feeds from {seed_file.relative_to(ROOT)}")
            store.sync_seed(seed_feeds)
            cache = seedcache.cache_path(v.key)
            if cache.exists() and seedcache.store_is_empty(store):
                n, touched = seedcache.load(store, cache)
                print(f"Loaded {v.label}: {n} cached headlines for {touched} feeds from {cache.relative_to(ROOT)}")
        stores[v.key] = store
    return stores


def backfill(stores: dict[str, Store], days: float, scraper: Scraper | None = None) -> dict:
    """Run a backfill to completion (or until stopped/rate-limited), printing progress."""
    scraper = scraper or Scraper(stores, auto=False, log=print)
    print(f"Backfilling {days:g} days for {sum(len(s.list_feeds()) for s in stores.values())} feeds from Feedly...")
    ok, res = scraper.start("backfill", list(stores), days, trigger="cli", wait=True)
    if not ok:
        print(f"Backfill not started: {res}")
        return {}
    run = scraper.status()["run"]
    print(f"Backfill {run['status']}: {run['done']}/{run['total']} feeds, {run['failed']} failed.")
    if run["status"] != "completed":
        print(f"{run.get('error') or 'Stopped early.'} Run --backfill {days:g} again to resume; "
              "finished feeds are skipped and nothing is lost.")
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=str(ROOT / "data"), help="directory holding one <vertical>.db per vertical")
    ap.add_argument("--interval", type=float, default=15, help="minutes between automatic refreshes")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't refresh on a schedule (an external orchestrator calls /api/scrape/start)")
    ap.add_argument("--no-resume", action="store_true", help="don't resume an interrupted scrape on startup")
    ap.add_argument("--backfill", type=float, metavar="DAYS",
                    help=f"load the last DAYS (max {RETENTION_DAYS}) of history for every feed from Feedly first")
    ap.add_argument("--import", dest="import_file", metavar="FILE", help="add feed URLs from FILE to --vertical")
    ap.add_argument("--vertical", choices=list(VERTICALS_BY_KEY), default="games", help="vertical for --import")
    ap.add_argument("--no-serve", action="store_true", help="exit after seeding/--import/--backfill")
    ap.add_argument("--api-token", default=os.environ.get("NEWS_API_TOKEN"),
                    help="token that lets other machines call the API (Authorization: Bearer <token>); "
                         "default: $NEWS_API_TOKEN")
    ap.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                    help="extra hostname allowed to make changes through the UI/API ('*' = any)")
    ap.add_argument("--ai-model", default=os.environ.get("NEWS_AI_MODEL", ai_mod.DEFAULT_MODEL),
                    help=f"Claude model for AI headlines in groups that turn them on (default {ai_mod.DEFAULT_MODEL}; "
                         "needs ANTHROPIC_API_KEY)")
    ap.add_argument("--no-ai", action="store_true", help="never call the Claude API, even for groups with AI summary on")
    ap.add_argument("--allow-private-feeds", action="store_true",
                    help="allow feed URLs on localhost/private networks (off by default to prevent SSRF)")
    args = ap.parse_args()

    feeds.ALLOW_PRIVATE = args.allow_private_feeds
    data_dir = Path(args.data)
    for old in {Path.cwd() / "news.db", ROOT / "news.db"}:
        if old.exists():
            print(f"Note: {old} is a database from the first version and is no longer used. To keep feeds you "
                  f"added there, re-add them in the UI or with --import.", file=sys.stderr)
    stores = open_stores(data_dir)
    if args.import_file:
        n = stores[args.vertical].add_feeds(read_feed_list(Path(args.import_file)))
        print(f"Imported {n} new feed(s) into {args.vertical}")

    digests = make_digests(stores, data_dir / "digests", log=print, workers=max(1, min(3, (os.cpu_count() or 2) - 1)))
    scraper = Scraper(stores, data_dir / "scrape_state.json", interval_minutes=args.interval,
                      auto=not args.no_auto and args.interval > 0, log=print, on_finish=digests.request_rebuild)
    if args.backfill:
        backfill(stores, args.backfill, scraper)
    if args.no_serve:
        return

    if not args.no_resume:
        scraper.resume_interrupted()
    threading.Thread(target=scraper.loop, daemon=True, name="scheduler").start()
    digests.request_rebuild()   # fresh digests from whatever is stored now
    security = Security(api_token=args.api_token, allowed_hosts=tuple(h.lower() for h in args.allow_host)
                        + ((args.host.lower(),) if args.host not in ("0.0.0.0", "::") else ()))
    group_store = groups_mod.GroupStore(data_dir / "groups.db")
    if n := seed_groups(group_store, stores):
        print(f"Created {n} filter group(s), one per new vertical (edit them under Settings › Groups)")
    summarizer = ai_mod.Summarizer(data_dir / "ai.db", model=args.ai_model, log=print, enabled=not args.no_ai)
    st_ai = summarizer.status()
    print(f"AI headlines: {'available (' + st_ai['model'] + ')' if st_ai['available'] else 'off: ' + st_ai['reason']}")
    srv = Server((args.host, args.port), make_handler(stores, scraper, security, digests, group_store, summarizer))
    print(f"News aggregator running at http://{args.host}:{args.port}  (data: {args.data})")
    st = scraper.status()
    print(f"Scraping: {'every %g min' % st['interval_minutes'] if st['auto'] else 'manual (POST /api/scrape/start)'}"
          f"{'; API token enabled' if args.api_token else ''}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
