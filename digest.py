"""Shared, per-vertical story digests: headlines grouped into stories, the expensive step.

Grouping doesn't depend on who is looking or on any filter group, so it runs once per vertical after
each scrape (and after a change that affects grouping: a feed added or removed, a keyword filter,
match strictness), for each window. Filter groups (groups.py) are then applied to these digests.
Digests are kept in memory and written to data/digests/ (JSON plus a gzipped copy), so they survive
restarts and could be served as static files.

Digest format (keys kept short):
{
  "vertical": "games", "label": "Games", "generated_at": <unix>, "window_hours": 168, "threshold": 0.12,
  "stories": [{"id", "k": [{"t": term, "w": weight, "f": [forms]}],          # key terms, best first
               "s": [{"f": outlet id, "t": title, "l": link, "p": published, "d": description, "i": image}]}]
}
One headline per outlet per story (the one matching.pick_headline would pick). Stories from a single
outlet are left out to keep files small.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import keyterms
import matching
from store import Store

WINDOWS = (48, 168, 720)          # hours: 2 days, 7 days, 30 days. Browsers pick the smallest that covers theirs.
DESCRIPTION_CHARS = 240
MIN_OUTLETS = 2


def window_for(hours: float) -> int:
    """The smallest digest window that covers `hours`."""
    return next((w for w in WINDOWS if hours <= w), WINDOWS[-1])


def _per_outlet(items: list[matching.Item]) -> list[matching.Item]:
    """One item per outlet: the headline matching.pick_headline would choose among its items."""
    by_feed: dict[int, list[matching.Item]] = {}
    for it in items:
        by_feed.setdefault(it.feed_id, []).append(it)
    return sorted((matching.pick_headline(its) for its in by_feed.values()), key=lambda i: i.published)


def build_digest(key: str, label: str, store: Store, hours: int, threshold: float) -> dict:
    """Group the last `hours` of headlines from every outlet in the catalog (switched on by default or
    not, since each filter group chooses its own) and describe the stories."""
    now = time.time()
    items = store.items_since(now - hours * 3600, include_disabled=True)
    stories = [s for s in matching.cluster(items, threshold=threshold) if s.coverage >= MIN_OUTLETS]

    entries, term_input, bodies = [], [], []
    for s in stories:
        chosen = _per_outlet(s.items)
        entries.append({
            "id": hashlib.blake2b(min(i.link or i.title for i in s.items).encode(), digest_size=6).hexdigest(),
            "s": [{"f": i.feed_id, "t": i.title, "l": i.link, "p": round(i.published),
                   "d": i.description[:DESCRIPTION_CHARS], "i": i.image} for i in chosen],
        })
        # Key terms use every headline in the story and the default weights for scale.
        term_input.append({"title": min((i.title for i in s.items), key=len), "score": s.score,
                           "sources": [{"title": i.title} for i in s.items]})
        bodies.append(list({i.description for i in s.items if i.description}))
    keyterms.story_terms(term_input, bodies=bodies)
    for e, t in zip(entries, term_input):
        e["k"] = [{"t": k["term"], "w": k["weight"], "f": k["forms"]} for k in t.get("keywords", [])]
    return {"vertical": key, "label": label, "generated_at": round(now), "window_hours": hours,
            "threshold": threshold, "stories": entries}


class Digests:
    """Builds digests in the background after scrapes or catalog changes, and serves the latest."""

    def __init__(self, out_dir: Path | None, builder: Callable[[str, int], dict], verticals: list[str],
                 log: Callable[[str], None] | None = None):
        self.out_dir = out_dir
        self.builder = builder
        self.verticals = verticals
        self.log = log or (lambda m: None)
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, int], dict] = {}     # (vertical, window) -> {"json", "gz", "etag", "at"}
        self._pending: set[str] = set()
        self._build_locks: dict[str, threading.Lock] = {}
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            self._load_from_disk()

    def _load_from_disk(self) -> None:
        for path in self.out_dir.glob("*.json.gz"):
            m = re.fullmatch(r"(\w+)-(\d+)\.json\.gz", path.name)
            if m and m.group(1) in self.verticals:
                try:
                    gz = path.read_bytes()
                    body = gzip.decompress(gz)
                    json.loads(body)
                except (OSError, EOFError, ValueError):   # damaged (e.g. a crash mid-write): rebuild it instead
                    self.log(f"digest {path.name} is damaged; it will be rebuilt")
                    path.unlink(missing_ok=True)
                    continue
                self._cache[(m.group(1), int(m.group(2)))] = {
                    "gz": gz, "json": body, "etag": self._etag(body), "at": path.stat().st_mtime}

    @staticmethod
    def _etag(body: bytes) -> str:
        return '"%s"' % hashlib.sha1(body).hexdigest()[:16]

    def _store(self, key: str, window: int, digest: dict) -> None:
        body = json.dumps(digest, separators=(",", ":"), ensure_ascii=False).encode()
        gz = gzip.compress(body, 6)
        entry = {"json": body, "gz": gz, "etag": self._etag(body), "at": time.time()}
        with self._lock:
            self._cache[(key, window)] = entry
        if self.out_dir:  # also written to disk: survives restarts, and can be served by a CDN as static files
            for suffix, data in ((".json", body), (".json.gz", gz)):
                path = self.out_dir / f"{key}-{window}{suffix}"
                fd, tmp = tempfile.mkstemp(dir=self.out_dir, prefix=path.name + ".", suffix=".tmp")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(data)
                    os.replace(tmp, path)
                except BaseException:
                    Path(tmp).unlink(missing_ok=True)
                    raise

    def _key_lock(self, key: str) -> threading.Lock:
        with self._lock:
            return self._build_locks.setdefault(key, threading.Lock())

    def build_now(self, key: str, windows: tuple[int, ...] = WINDOWS) -> None:
        with self._key_lock(key):              # one build per vertical at a time (background or on demand)
            self._build(key, windows)

    def _build(self, key: str, windows: tuple[int, ...]) -> None:
        for w in windows:
            t0 = time.time()
            digest = self.builder(key, w)
            self._store(key, w, digest)
            self.log(f"digest {key}-{w}h: {len(digest['stories'])} stories, built in {time.time() - t0:.1f}s")

    def get(self, key: str, hours: float) -> dict:
        """The cached digest covering `hours` (built on the spot the first time)."""
        w = window_for(hours)
        with self._lock:
            entry = self._cache.get((key, w))
        if entry is None:
            with self._key_lock(key):
                with self._lock:
                    entry = self._cache.get((key, w))
                if entry is None:                 # not built by whoever held the lock
                    self._build(key, (w,))
                    with self._lock:
                        entry = self._cache[(key, w)]
        return entry

    def data(self, key: str, hours: float) -> tuple[str, dict]:
        """(etag, parsed digest) covering `hours`; the parsed copy is kept with the cached bytes."""
        entry = self.get(key, hours)
        if "data" not in entry:
            entry["data"] = json.loads(entry["json"])
        return entry["etag"], entry["data"]

    def request_rebuild(self, verticals: list[str] | None = None) -> None:
        """Queue a background rebuild (coalesced: many requests -> one rebuild)."""
        with self._lock:
            self._pending.update(verticals or self.verticals)
        self._wake.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="digests")
            self._thread.start()

    def _loop(self) -> None:
        while True:
            self._wake.wait(30)
            self._wake.clear()
            time.sleep(1)  # let bursts of changes (several outlets edited) settle into one rebuild
            with self._lock:
                todo, self._pending = sorted(self._pending), set()
            for key in todo:
                try:
                    self.build_now(key)
                except Exception:  # noqa: BLE001 - keep serving the previous digest
                    self.log(f"digest {key} failed:\n" + traceback.format_exc())
