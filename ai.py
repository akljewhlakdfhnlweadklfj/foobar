"""AI headlines ("AI summary"): one short headline per story, written by Claude from every outlet's
headline for it. Used by filter groups that switch it on (see groups.py).

- Needs the `anthropic` package and credentials (ANTHROPIC_API_KEY); without them the groups show
  the usual headlines and say why.
- Written in the background, about 20 stories per request, and cached in data/ai.db by the stories'
  articles: a story keeps its headline while it grows, until it has twice as many outlets (and at
  least 3 more) than when it was written; then it's rewritten.
- At most MAX_PER_DAY stories a day are written (cost guard; see README for prices).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_MODEL = "claude-opus-5-5"
BATCH = 20
MAX_PER_DAY = 2000
MAX_TITLES = 25          # headlines sent per story
RETENTION_DAYS = 31

SYSTEM = """You write news headlines. Each numbered story lists every outlet's headline for the same story.
For each story, write the shortest headline that is still coherent and accurate: a complete statement a
reader understands on its own, keeping the key names (people, titles, companies, places). Use only facts
the headlines agree on; drop filler, hype, opinion framing and outlet labels. Use sentence case."""

SCHEMA = {
    "type": "object",
    "properties": {"headlines": {"type": "array", "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "headline": {"type": "string"}},
        "required": ["n", "headline"], "additionalProperties": False}}},
    "required": ["headlines"], "additionalProperties": False,
}


def _default_client():
    """An Anthropic client, or (None, reason) when the package or credentials are missing."""
    try:
        import anthropic
    except ImportError:
        return None, "the anthropic package isn't installed (pip install anthropic)"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no API key (set ANTHROPIC_API_KEY on the server)"
    return anthropic.Anthropic(), ""


class Summarizer:
    def __init__(self, path: Path | None, model: str = DEFAULT_MODEL, log: Callable[[str], None] | None = None,
                 client=None, enabled: bool = True):
        self.model = model
        self.log = log or (lambda m: None)
        self.version = 0                       # bumped when new headlines are saved
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: dict[tuple, tuple[list[str], list[str], int, float]] = {}
        self._by_key: dict[str, set[int]] = {}
        self._rows: dict[int, tuple[str, int]] = {}   # id -> (headline, outlets when written)
        self._today: tuple[str, int] = ("", 0)
        self._use_fallbacks = True
        self.last_error = ""
        if not enabled:
            self.client, self.reason = None, "turned off on the server (--no-ai)"
        elif client is not None:
            self.client, self.reason = client, ""
        else:
            self.client, self.reason = _default_client()
        self.path = str(path) if path else ":memory:"
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if not path else None
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS summaries (id INTEGER PRIMARY KEY, headline TEXT NOT NULL, "
                      "model TEXT, outlets INTEGER NOT NULL, created_at REAL NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS summary_items (key TEXT NOT NULL, summary_id INTEGER NOT NULL, "
                      "PRIMARY KEY (key, summary_id))")
            old = time.time() - RETENTION_DAYS * 86400
            c.execute("DELETE FROM summary_items WHERE summary_id IN (SELECT id FROM summaries WHERE created_at < ?)", (old,))
            c.execute("DELETE FROM summaries WHERE created_at < ?", (old,))
            for sid, text, n in c.execute("SELECT id, headline, outlets FROM summaries"):
                self._rows[sid] = (text, n)
            for key, sid in c.execute("SELECT key, summary_id FROM summary_items"):
                self._by_key.setdefault(key, set()).add(sid)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._mem is not None:
            with self._mem:
                yield self._mem
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, timeout=30)
        try:
            with c:
                yield c
        finally:
            c.close()

    @property
    def available(self) -> bool:
        return self.client is not None

    def status(self) -> dict:
        reason = self.reason or self.last_error
        return {"available": self.available, "reason": reason, "model": self.model,
                "pending": len(self._pending), "written_today": self._today[1], "max_per_day": MAX_PER_DAY}

    # --- lookups (called while building views) ---

    def _best(self, keys: list[str]) -> int | None:
        votes: dict[int, int] = {}
        with self._lock:
            for k in keys:
                for sid in self._by_key.get(k, ()):
                    votes[sid] = votes.get(sid, 0) + 1
        return max(votes, key=lambda s: (votes[s], s)) if votes else None

    def lookup(self, keys: list[str], outlets: int) -> str | None:
        """The cached headline for a story with these articles, unless it has grown a lot since."""
        sid = self._best(keys)
        if sid is None:
            return None
        text, n = self._rows[sid]
        if outlets >= 2 * n and outlets >= n + 3:
            return None
        return text

    def want(self, keys: list[str], titles: list[str], outlets: int, latest: float) -> None:
        if not self.available:
            return
        with self._lock:
            self._pending[tuple(sorted(keys))] = (keys, titles, outlets, latest)

    def kick(self) -> None:
        """Start writing whatever is pending (background thread)."""
        if not self.available or not self._pending:
            return
        self._wake.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="ai-headlines")
            self._thread.start()

    # --- writing ---

    def _loop(self) -> None:
        while True:
            self._wake.wait(60)
            self._wake.clear()
            while self.run_once():
                pass

    def _budget(self) -> int:
        day = time.strftime("%Y-%m-%d")
        if self._today[0] != day:
            self._today = (day, 0)
        return MAX_PER_DAY - self._today[1]

    def run_once(self) -> bool:
        """Write one batch (newest stories first). Returns True if there may be more to do."""
        budget = self._budget()
        with self._lock:
            if not self._pending or budget <= 0 or not self.available:
                if budget <= 0 and self._pending:
                    self.last_error = f"daily limit of {MAX_PER_DAY} AI headlines reached; more tomorrow"
                return False
            batch = sorted(self._pending.items(), key=lambda kv: -kv[1][3])[:min(BATCH, budget)]
            for k, _ in batch:
                del self._pending[k]
        stories = [v for _, v in batch]
        try:
            texts = self._write([s[1] for s in stories])
        except Exception as e:  # noqa: BLE001 - keep serving the usual headlines
            self.last_error = f"AI headlines failed: {type(e).__name__}: {str(e)[:200]}"
            self.log(self.last_error)
            if self._fatal(e):
                self.client, self.reason = None, self.last_error
            return False
        now = time.time()
        with self._conn() as c:
            for (keys, _titles, outlets, _), text in zip(stories, texts):
                if not text:
                    continue
                sid = c.execute("INSERT INTO summaries (headline, model, outlets, created_at) VALUES (?, ?, ?, ?)",
                                (text, self.model, outlets, now)).lastrowid
                c.executemany("INSERT OR IGNORE INTO summary_items (key, summary_id) VALUES (?, ?)",
                              [(k, sid) for k in keys])
                with self._lock:
                    self._rows[sid] = (text, outlets)
                    for k in keys:
                        self._by_key.setdefault(k, set()).add(sid)
        self._today = (self._today[0], self._today[1] + len(stories))
        self.version += 1
        self.last_error = ""
        self.log(f"AI headlines: wrote {sum(1 for t in texts if t)} of {len(stories)}")
        return bool(self._pending)

    @staticmethod
    def _fatal(e: Exception) -> bool:
        name = type(e).__name__
        return name in ("AuthenticationError", "PermissionDeniedError", "NotFoundError")

    def _write(self, stories: list[list[str]]) -> list[str]:
        lines = []
        for n, titles in enumerate(stories, 1):
            lines.append(f"[{n}]")
            lines += [f"- {t}" for t in list(dict.fromkeys(titles))[:MAX_TITLES]]
        params = dict(model=self.model, max_tokens=8000, system=SYSTEM,
                      messages=[{"role": "user", "content": "\n".join(lines)}],
                      output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
        if self._use_fallbacks:
            try:
                # If Claude declines (safety classifiers), the API retries on its recommended fallback model.
                resp = self.client.beta.messages.create(**params, betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks="default")
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ != "BadRequestError":
                    raise
                self._use_fallbacks = False      # not accepted for this model/account: plain requests from now on
                resp = self.client.messages.create(**params)
        else:
            resp = self.client.messages.create(**params)
        if getattr(resp, "stop_reason", None) == "refusal":
            return [""] * len(stories)
        text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "{}")
        got = {h["n"]: " ".join(str(h["headline"]).split()) for h in json.loads(text).get("headlines", [])}
        return [got.get(n, "")[:200] for n in range(1, len(stories) + 1)]
