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
import re
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
MIN_HEADLINE_WORDS = 4
LABELLED = re.compile(r"^\s*(opinion|op-ed|watch|video|videos|listen|podcast|live|live updates|analysis|editorial|"
                      r"explainer|review|reviews|preview|quiz|letters?|photos?|in pictures|the latest|perspective|"
                      r"column|commentary|interview|recap)\b\s*[:|\-–—]", re.I)


def item_key(link: str, title: str) -> str:
    """Short, stable id for one outlet's article (for "last seen" and AI summary caching)."""
    return hashlib.blake2b((link or title).encode(), digest_size=5).hexdigest()


# --- validation ------------------------------------------------------------------------------

def _setting(name: str, value):
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
        "name": "", "verticals": [], "ai_summary": False, "position": 0, "settings": dict(DEFAULT_SETTINGS), "outlets": {}}
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
            cfg["settings"][k] = _setting(k, val)
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
        self._lock = threading.Lock()
        self.version = 0
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS groups (id INTEGER PRIMARY KEY, config TEXT NOT NULL, "
                      "updated_at REAL NOT NULL)")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        try:
            with c:
                yield c
        finally:
            c.close()

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, config, updated_at FROM groups").fetchall()
        out = [{**json.loads(cfg), "id": gid, "updated_at": at} for gid, cfg, at in rows]
        return sorted(out, key=lambda g: (g["position"], g["id"]))

    def get(self, gid: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT id, config, updated_at FROM groups WHERE id = ?", (gid,)).fetchone()
        return {**json.loads(row[1]), "id": row[0], "updated_at": row[2]} if row else None

    def create(self, cfg: dict) -> int:
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

    def delete(self, gid: int) -> bool:
        with self._lock, self._conn() as c:
            n = c.execute("DELETE FROM groups WHERE id = ?", (gid,)).rowcount
            self.version += 1
            return n > 0

    def is_empty(self) -> bool:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


# --- views -----------------------------------------------------------------------------------

def _headline(counted: list[dict], weight: Callable[[dict], float]) -> dict:
    """The shortest headline among counted outlets (4+ words, not a labelled format piece)."""
    full = [x for x in counted if len(x["t"].split()) >= MIN_HEADLINE_WORDS] or counted
    plain = [x for x in full if not LABELLED.match(x["t"])] or full
    return min(plain, key=lambda x: (len(x["t"]), -weight(x), x["p"]))


def build_view(group: dict, digest_for: Callable[[str, float], dict], summarizer=None, now: float | None = None) -> dict:
    """Apply a group's filters to the shared digests of its verticals."""
    now = now or time.time()
    cfg = {**DEFAULT_SETTINGS, **group.get("settings", {})}
    cut = now - cfg["hours"] * 3600
    stories, min_out, countable = [], {}, {}
    ai_on = bool(group.get("ai_summary"))
    for v in group["verticals"]:
        d = digest_for(v, cfg["hours"])
        over = group.get("outlets", {}).get(v, {})
        outlets = {}
        for o in d["outlets"]:
            ov = over.get(str(o["id"]), {})
            outlets[o["id"]] = {"name": o["name"], "on": ov.get("on", o["enabled"]), "req": ov.get("req", o["required"]),
                                "w": ov.get("w", o["weight"])}
        n_count = sum(1 for o in outlets.values() if o["on"] and o["w"] >= cfg["min_weight"])
        need = (max(1, math.ceil(cfg["min_feeds_percent"] / 100 * n_count)) if cfg["min_feeds_mode"] == "percent"
                else cfg["min_feeds"])
        min_out[v], countable[v] = need, n_count
        required = {fid for fid, o in outlets.items() if o["req"]}
        for st in d["stories"]:
            srcs = [x for x in st["s"] if x["p"] >= cut and outlets.get(x["f"], {}).get("on")]
            if not srcs:
                continue
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
            lead = _headline(counted, weight)
            best = sorted(srcs, key=lambda x: (-weight(x), -x["p"]))
            ordered = [lead] + sorted((x for x in srcs if x is not lead), key=lambda x: (-weight(x), x["p"]))
            keys = [item_key(x["l"], x["t"]) for x in st["s"]]
            out = {"id": st["id"], "v": v, "t": lead["t"], "l": lead["l"], "f": outlets[lead["f"]]["name"],
                   "d": lead["d"] or next((x["d"] for x in best if x["d"]), ""),
                   "i": lead["i"] or next((x["i"] for x in best if x["i"]), ""),
                   "k": st.get("k", []), "sc": round(score, 2), "cov": len(counted), "n": len(srcs),
                   "p": min(x["p"] for x in srcs), "u": max(x["p"] for x in srcs), "h": keys,
                   "s": [{"o": outlets[x["f"]]["name"], "t": x["t"], "l": x["l"], "p": x["p"], "w": weight(x),
                          "d": x["d"], "c": weight(x) >= cfg["min_weight"]} for x in ordered]}
            if extra:
                out["x"] = 1
            elif ai_on and summarizer is not None:
                text = summarizer.lookup(keys, len(srcs))
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
    return {"group": public, "generated_at": round(now), "min_outlets": min_out, "countable": countable,
            "ai": ai, "stories": stories}


class Views:
    """Each group's view as ready-to-send bytes, rebuilt only when its inputs change: the group, the
    digests of its verticals or the AI summaries (so every viewer gets the same cached file)."""

    def __init__(self, groups: GroupStore, digests: digest_mod.Digests, summarizer=None):
        self.groups, self.digests, self.summarizer = groups, digests, summarizer
        self._cache: dict[int, tuple[tuple, dict]] = {}
        self._lock = threading.Lock()

    def _digest(self, v: str, hours: float) -> tuple[str, dict]:
        return self.digests.data(v, hours)

    def get(self, gid: int) -> dict | None:
        group = self.groups.get(gid)
        if group is None:
            return None
        hours = {**DEFAULT_SETTINGS, **group.get("settings", {})}["hours"]
        tags = tuple(self._digest(v, hours)[0] for v in group["verticals"])
        # Time moves the window's cut-off too: rebuild at least once a minute.
        key = (group["updated_at"], tags, getattr(self.summarizer, "version", 0), int(time.time() // 60))
        with self._lock:
            hit = self._cache.get(gid)
            if hit and hit[0] == key:
                return hit[1]
        view = build_view(group, lambda v, h: self._digest(v, h)[1], self.summarizer)
        body = json.dumps(view, separators=(",", ":"), ensure_ascii=False).encode()
        entry = {"json": body, "gz": gzip.compress(body, 6),
                 "etag": '"%s"' % hashlib.sha1(body).hexdigest()[:16], "view": view}
        with self._lock:
            self._cache[gid] = (key, entry)
        if self.summarizer is not None and group.get("ai_summary"):
            self.summarizer.kick()
        return entry
