"""Filter groups: named, shared story views (the tabs of the Stories page).

A group picks one or more verticals and the filters that used to be per viewer: the time window,
"reported by at least" (a count, or a percentage of the outlets that can count), the weight an outlet
needs to count, a minimum score, which outlets are switched on, their weights and which ones a story
must include. The server applies them once per group to the shared, already-grouped digests (see
digest.py) and serves the result as one cached JSON file, so many viewers cost about as much as one.
Only watched phrases and "last seen" stay in each viewer's browser.

Group config:
{"name": "Games", "verticals": ["games"], "ai_summary": false, "position": 0,
 "settings": {"hours": 48, "min_feeds_mode": "count", "min_feeds": 4, "min_feeds_percent": 10,
              "min_weight": 0, "min_score": 0, "require_all": false},
 "outlets": {"games": {"12": {"on": false}, "7": {"req": true, "w": 4.5}}}}   # overrides of the catalog

View (GET /api/groups/<id>/stories), keys kept short:
{"group": {...config, "id"}, "generated_at", "min_outlets": {vertical: N}, "countable": {vertical: n},
 "ai": {"on", "available", "reason", "pending"},
 "stories": [{"id", "v": vertical, "t": headline, "a": AI headline (when on and written), "l": link,
              "f": lead outlet, "d": description, "i": image, "k": [{"t", "w", "f"}] key terms,
              "sc": score, "cov": outlets that count, "n": outlets, "p": first, "u": latest,
              "x": 1 when it misses the group's minimums (shown only for a watched phrase, if the viewer asks),
              "h": [item keys, for "last seen"],
              "s": [{"o": outlet, "t", "l", "p", "w", "d", "c": counts}]}]}
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import digest as digest_mod
import matching

MAX_WEIGHT = 10.0
DEFAULT_SETTINGS = {"hours": 48.0, "min_feeds_mode": "count", "min_feeds": 3, "min_feeds_percent": 10.0,
                    "min_weight": 0.0, "min_score": 0.0, "require_all": False}
SETTING_SPECS = {   # name: (type, min, max)
    "hours": (float, 1, 24 * 30),
    "min_feeds_mode": (("count", "percent"), None, None),
    "min_feeds": (int, 1, 100),
    "min_feeds_percent": (float, 1, 100),
    "min_weight": (float, 0, MAX_WEIGHT),
    "min_score": (float, 0, 10_000),
    "require_all": (bool, None, None),
}
MAX_NAME = 60
def item_key(link: str, title: str) -> str:
    """Short, stable id for one outlet's article (for "last seen" and AI summary caching)."""
    return hashlib.blake2b((link or title).encode(), digest_size=5).hexdigest()


# --- validation ------------------------------------------------------------------------------

def coerce_setting(name: str, value):
    typ, lo, hi = SETTING_SPECS[name]
    if isinstance(typ, tuple):
        if value in typ:
            return value
        raise ValueError(f"{name} must be one of: {', '.join(typ)}")
    if typ is bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"{name} must be true or false")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{name} must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return int(round(value)) if typ is int else float(value)


def clean(body: dict, current: dict | None, verticals: list[str]) -> dict:
    """A validated group config: `current` with the changes in `body` applied (a new group when None).
    Raises ValueError with a message for the API."""
    cfg = json.loads(json.dumps(current)) if current else {
        "name": "", "verticals": [], "ai_summary": False, "position": None, "settings": dict(DEFAULT_SETTINGS), "outlets": {}}
    unknown = set(body) - {"name", "verticals", "ai_summary", "position", "settings", "outlets"}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    if "name" in body:
        name = " ".join(str(body["name"]).split()) if isinstance(body["name"], str) else ""
        if not 1 <= len(name) <= MAX_NAME:
            raise ValueError(f"name must be 1 to {MAX_NAME} characters")
        cfg["name"] = name
    if "verticals" in body:
        vs = body["verticals"]
        if not isinstance(vs, list) or not vs or not all(isinstance(v, str) and v in verticals for v in vs):
            raise ValueError(f"verticals must be a non-empty list of: {', '.join(verticals)}")
        cfg["verticals"] = list(dict.fromkeys(vs))
    if "ai_summary" in body:
        if not isinstance(body["ai_summary"], bool):
            raise ValueError("ai_summary must be true or false")
        cfg["ai_summary"] = body["ai_summary"]
    if "position" in body:
        p = body["position"]
        if isinstance(p, bool) or not isinstance(p, int) or not 0 <= p <= 10_000:
            raise ValueError("position must be a whole number from 0 to 10000")
        cfg["position"] = p
    if "settings" in body:
        if not isinstance(body["settings"], dict):
            raise ValueError("settings must be an object")
        for k, val in body["settings"].items():
            if k not in SETTING_SPECS:
                raise ValueError(f"unknown setting: {k}")
            cfg["settings"][k] = coerce_setting(k, val)
    if "outlets" in body:
        if not isinstance(body["outlets"], dict):
            raise ValueError("outlets must be an object: {vertical: {outlet id: {on, req, w} or null}}")
        for v, changes in body["outlets"].items():
            if v not in verticals or not isinstance(changes, dict):
                raise ValueError("outlets must be an object: {vertical: {outlet id: {on, req, w} or null}}")
            mine = cfg["outlets"].setdefault(v, {})
            for fid, ch in changes.items():
                if not str(fid).isdigit():
                    raise ValueError("outlet ids are numbers")
                if ch is None:
                    mine.pop(str(fid), None)
                    continue
                if not isinstance(ch, dict) or set(ch) - {"on", "req", "w"}:
                    raise ValueError("an outlet change is {on, req, w} (or null to use the catalog's)")
                o = mine.setdefault(str(fid), {})
                for k in ("on", "req"):
                    if k in ch:
                        if ch[k] is None:
                            o.pop(k, None)
                        elif isinstance(ch[k], bool):
                            o[k] = ch[k]
                        else:
                            raise ValueError(f"{k} must be true or false")
                if "w" in ch:
                    w = ch["w"]
                    if w is None:
                        o.pop("w", None)
                    elif isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 <= w <= MAX_WEIGHT:
                        raise ValueError(f"w must be a number from 0 to {MAX_WEIGHT:g}")
                    else:
                        o["w"] = float(w)
                if not o:
                    mine.pop(str(fid))
            if not mine:
                cfg["outlets"].pop(v)
    if not cfg["name"]:
        raise ValueError("name is required")
    if not cfg["verticals"]:
        raise ValueError("verticals is required")
    return cfg


# --- storage ---------------------------------------------------------------------------------

class GroupStore:
    """Groups in their own SQLite file (data/groups.db): they can span verticals."""

    def __init__(self, path: Path):
        self.path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.version = 0
        self._mem: tuple[int, list[dict]] | None = None
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, config TEXT NOT NULL, "
                      "updated_at REAL NOT NULL)")
            have_seeded = c.execute("SELECT 1 FROM sqlite_master WHERE name = 'seeded'").fetchone()
            c.execute("CREATE TABLE IF NOT EXISTS seeded (vertical TEXT PRIMARY KEY)")
            if not have_seeded:   # groups.db from before this table: verticals already in a group were seeded
                verticals = {v for (cfg,) in c.execute("SELECT config FROM groups") for v in json.loads(cfg)["verticals"]}
                c.executemany("INSERT OR IGNORE INTO seeded (vertical) VALUES (?)", [(v,) for v in verticals])

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        try:
            with c:
                yield c
        finally:
            c.close()

    def list(self) -> list[dict]:
        """All groups in tab order. Kept in memory between changes: treat the dicts as read-only."""
        with self._lock:
            if self._mem is not None and self._mem[0] == self.version:
                return self._mem[1]
            with self._conn() as c:
                rows = c.execute("SELECT id, config, updated_at FROM groups").fetchall()
            out = sorted(({**json.loads(cfg), "id": gid, "updated_at": at} for gid, cfg, at in rows),
                         key=lambda g: (g["position"], g["id"]))
            self._mem = (self.version, out)
            return out

    def get(self, gid: int) -> dict | None:
        return next((g for g in self.list() if g["id"] == gid), None)

    def create(self, cfg: dict) -> int:
        """Add a group (at the end unless cfg sets a position)."""
        with self._lock:
            if cfg.get("position") is None:
                cfg = {**cfg, "position": 1 + max((g["position"] for g in self.list()), default=-1)}
            return self._insert(cfg)

    def _insert(self, cfg: dict) -> int:
        with self._lock, self._conn() as c:
            gid = c.execute("INSERT INTO groups (config, updated_at) VALUES (?, ?)",
                            (json.dumps(cfg), time.time())).lastrowid
            self.version += 1
            return gid

    def update(self, gid: int, cfg: dict) -> bool:
        cfg = {k: v for k, v in cfg.items() if k not in ("id", "updated_at")}
        with self._lock, self._conn() as c:
            n = c.execute("UPDATE groups SET config = ?, updated_at = ? WHERE id = ?",
                          (json.dumps(cfg), time.time(), gid)).rowcount
            self.version += 1
            return n > 0

    def change(self, gid: int, body: dict, verticals: list[str]) -> dict | None:
        """Apply a validated change atomically (no lost updates); None if there's no such group.
        Raises ValueError for an invalid change."""
        with self._lock:
            current = self.get(gid)
            if current is None:
                return None
            self.update(gid, clean(body, current, verticals))
            return self.get(gid)

    def forget_outlet(self, vertical: str, feed_id: int) -> None:
        """Drop every group's settings for an outlet (when it's removed, or its id is given to a new feed)."""
        with self._lock:
            for g in self.list():
                if str(feed_id) in g.get("outlets", {}).get(vertical, {}):
                    g = json.loads(json.dumps(g))          # the cached copy is shared: change a copy
                    g["outlets"][vertical].pop(str(feed_id))
                    if not g["outlets"][vertical]:
                        g["outlets"].pop(vertical)
                    self.update(g["id"], g)

    def delete(self, gid: int) -> bool:
        """Remove a group; the last one can't be removed (ValueError)."""
        with self._lock:
            if self.get(gid) is None:
                return False
            if len(self.list()) <= 1:
                raise ValueError("Keep at least one group")
            return self._delete(gid)

    def _delete(self, gid: int) -> bool:
        with self._lock, self._conn() as c:
            n = c.execute("DELETE FROM groups WHERE id = ?", (gid,)).rowcount
            self.version += 1
            return n > 0

    def was_seeded(self, vertical: str) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM seeded WHERE vertical = ?", (vertical,)).fetchone() is not None

    def mark_seeded(self, vertical: str) -> None:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO seeded (vertical) VALUES (?)", (vertical,))

    def is_empty(self) -> bool:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


# --- views -----------------------------------------------------------------------------------

def _top_video(srcs: list[dict], weight, outlets: dict) -> dict | None:
    """The story's most popular YouTube video: most views (when known), then the higher-weighted
    channel, then the newest."""
    videos = [x for x in srcs if x.get("y")]
    if not videos:
        return None
    x = max(videos, key=lambda x: (x.get("v", -1), weight(x), x["p"]))
    out = {"t": x["t"], "l": x["l"], "o": outlets[x["f"]]["name"], "i": x["i"], "p": x["p"]}
    if "v" in x:
        out["v"] = x["v"]
    return out


def min_outlets(cfg: dict, countable: int) -> int:
    """The "reported by at least" number: a count, or a share of the outlets that can count (rounded
    up, at least 1; rounded exactly, so 7% of 100 is 7)."""
    if cfg["min_feeds_mode"] != "percent":
        return cfg["min_feeds"]
    return max(1, math.ceil(round(cfg["min_feeds_percent"] * countable / 100, 9)))


def build_view(group: dict, digest_for: Callable[[str, float], dict], catalog_for: Callable[[str], list[dict]],
               summarizer=None, now: float | None = None) -> dict:
    """Apply a group's filters to the shared digests of its verticals. catalog_for(v) gives the
    vertical's outlets [{"id", "name", "weight", "enabled"}]: names and the defaults groups override."""
    now = now or time.time()
    cfg = {**DEFAULT_SETTINGS, **group.get("settings", {})}
    cut = now - cfg["hours"] * 3600
    stories, min_out, countable, built, expires = [], {}, {}, 0, math.inf
    ai_on = bool(group.get("ai_summary"))
    for v in group["verticals"]:
        d = digest_for(v, cfg["hours"])
        built = max(built, d.get("generated_at", 0))
        over = group.get("outlets", {}).get(v, {})
        outlets = {}
        for o in catalog_for(v):
            ov = over.get(str(o["id"]), {})
            outlets[o["id"]] = {"name": o["name"], "on": ov.get("on", o["enabled"]), "req": ov.get("req", False),
                                "w": ov.get("w", o["weight"])}
        n_count = sum(1 for o in outlets.values() if o["on"] and o["w"] >= cfg["min_weight"])
        need = min_outlets(cfg, n_count)
        min_out[v], countable[v] = need, n_count
        required = {fid for fid, o in outlets.items() if o["req"]}
        for st in d["stories"]:
            srcs = [x for x in st["s"] if x["p"] >= cut and outlets.get(x["f"], {}).get("on")]
            if not srcs:
                continue
            expires = min(expires, min(x["p"] for x in srcs) + cfg["hours"] * 3600)   # first one to leave the window
            if required:
                have = {x["f"] for x in srcs} & required
                if not have or (cfg["require_all"] and len(have) < len(required)):
                    continue
            weight = lambda x: outlets[x["f"]]["w"]  # noqa: E731
            counted = [x for x in srcs if weight(x) >= cfg["min_weight"]]
            if not counted:
                continue
            score = sum(weight(x) for x in counted)
            extra = len(counted) < need or score < cfg["min_score"]
            # Headline, description and picture come from articles when the story has any (video titles
            # are written for clicks); a story made only of videos uses those.
            articles = [x for x in counted if not x.get("y")] or counted
            lead = matching.pick_headline(articles, title=lambda x: x["t"], weight=weight, published=lambda x: x["p"])
            best = sorted(srcs, key=lambda x: (bool(x.get("y")), -weight(x), -x["p"]))
            ordered = [lead] + sorted((x for x in srcs if x is not lead), key=lambda x: (-weight(x), x["p"]))
            keys = [item_key(x["l"], x["t"]) for x in st["s"]]
            out = {"id": st["id"], "v": v, "t": lead["t"], "l": lead["l"], "f": outlets[lead["f"]]["name"],
                   "d": lead["d"] or next((x["d"] for x in best if x["d"]), ""),
                   "i": lead["i"] or next((x["i"] for x in best if x["i"]), ""),
                   "k": st.get("k", [])[:1], "sc": round(score, 2), "cov": len(counted), "n": len(srcs),
                   "p": min(x["p"] for x in srcs), "u": max(x["p"] for x in srcs), "h": keys,
                   "s": [{"o": outlets[x["f"]]["name"], "t": x["t"], "l": x["l"], "p": x["p"], "w": weight(x), "y": x.get("y", 0),
                          "d": x["d"], "c": weight(x) >= cfg["min_weight"]} for x in ordered]}
            top = _top_video(srcs, weight, outlets)
            if top:
                out["mv"] = top                    # shown in the story's details
            if extra:
                out["x"] = 1
            elif ai_on and summarizer is not None:
                text = summarizer.lookup(keys)
                if text:
                    out["a"] = text
                else:
                    summarizer.want(keys, [x["t"] for x in st["s"]], len(srcs), out["u"])
            stories.append(out)
    stories.sort(key=lambda s: (-s["sc"], -s["cov"], -s["u"]))
    ai = {"on": ai_on}
    if ai_on:
        st = summarizer.status() if summarizer is not None else {"available": False, "reason": "AI summaries aren't set up"}
        ai.update(available=st["available"], reason=st.get("reason", ""),
                  pending=sum(1 for s in stories if "x" not in s and "a" not in s))
    public = {k: v for k, v in group.items() if k != "updated_at"}
    # generated_at is the digests' (not now), so an unchanged result has unchanged bytes and ETag.
    return {"_expires": expires, "group": public, "generated_at": built, "min_outlets": min_out, "countable": countable,
            "ai": ai, "stories": stories}


def _payload(obj) -> dict:
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return {"json": body, "gz": gzip.compress(body, 6), "etag": '"%s"' % hashlib.sha1(body).hexdigest()[:16]}


class Views:
    """Each group's view as ready-to-send bytes (gzip + ETag), the same for every viewer. Rebuilt only
    when an input changes (the group, its verticals' digests and catalogs, its AI headlines) or a
    story's oldest headline leaves the time window; one request rebuilds while others get the
    previous copy. Stories that miss the group's minimums (shown only for a viewer's watched phrases)
    are a separate payload, so most viewers don't download them."""

    def __init__(self, groups: GroupStore, digests: digest_mod.Digests, stores: dict, summarizer=None):
        self.groups, self.digests, self.stores, self.summarizer = groups, digests, stores, summarizer
        self._catalogs: dict[str, tuple[int, list[dict]]] = {}
        self._cache: dict[int, tuple[tuple, float, dict]] = {}     # gid -> (key, expires, entry)
        self._building: dict[int, threading.Lock] = {}
        self._lock = threading.Lock()

    def _digest(self, v: str, hours: float) -> tuple[str, dict]:
        return self.digests.data(v, hours)

    def _catalog(self, v: str) -> list[dict]:
        """The vertical's outlets, re-read only when the catalog changed."""
        store = self.stores[v]
        version = store.catalog_version
        hit = self._catalogs.get(v)
        if hit and hit[0] == version:
            return hit[1]
        outlets = [{"id": f["id"], "name": f.get("name") or f.get("title") or f["url"],
                    "weight": f["weight"] if f["weight"] is not None else matching.DEFAULT_WEIGHT,
                    "enabled": bool(f["enabled"])} for f in store.list_feeds()]
        self._catalogs[v] = (version, outlets)
        return outlets

    def _key(self, group: dict) -> tuple:
        hours = {**DEFAULT_SETTINGS, **group.get("settings", {})}["hours"]
        tags = tuple((self._digest(v, hours)[0], self.stores[v].catalog_version) for v in group["verticals"])
        ai_version = getattr(self.summarizer, "version", 0) if group.get("ai_summary") else 0
        return (group["updated_at"], tags, ai_version)

    def _fresh(self, gid: int, key: tuple) -> dict | None:
        with self._lock:
            hit = self._cache.get(gid)
        return hit[2] if hit and hit[0] == key and time.time() < hit[1] else None

    def get(self, gid: int) -> dict | None:
        """{"json", "gz", "etag", "view", "extras": {"json", "gz", "etag"}} or None for no such group."""
        group = self.groups.get(gid)
        if group is None:
            with self._lock:
                self._cache.pop(gid, None)
            return None
        key = self._key(group)
        entry = self._fresh(gid, key)
        if entry:
            return entry
        with self._lock:
            building = self._building.setdefault(gid, threading.Lock())
            previous = self._cache.get(gid)
        if not building.acquire(blocking=previous is None):
            return previous[2]                     # someone is rebuilding it: the previous copy meanwhile
        try:
            entry = self._fresh(gid, key)
            if entry:
                return entry
            view = build_view(group, lambda v, h: self._digest(v, h)[1], self._catalog, self.summarizer)
            expires = view.pop("_expires")
            extras = [st for st in view["stories"] if "x" in st]
            view["stories"] = [st for st in view["stories"] if "x" not in st]
            view["extras_count"] = len(extras)
            entry = {**_payload(view), "view": view, "extras": _payload({"stories": extras})}
            with self._lock:
                self._cache[gid] = (key, expires, entry)
        finally:
            building.release()
        if self.summarizer is not None and group.get("ai_summary"):
            self.summarizer.kick()
        return entry
