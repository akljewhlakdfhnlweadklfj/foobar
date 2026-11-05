"""Scrape runs: start/stop, progress, scheduling and resume after interruption.

A *run* refreshes (fetches the live feeds) or backfills (loads Feedly history) the feeds of one or
more verticals. Progress is durable at two levels:
  - the run itself (id, kind, verticals, status, counters) is written to data/scrape_state.json
    after every feed, via write-to-temp-and-rename;
  - each feed records the id of the last run that finished it (feeds.last_run in its vertical DB).
So if a run is stopped, or the process is killed partway, starting it again ("resume") only
processes feeds that run hasn't finished yet. On startup an interrupted run is resumed
automatically (unless disabled).

Scheduling: with auto=True a refresh starts every `interval_minutes` while idle. Turn auto off to
leave scheduling to an external orchestrator that calls the start/stop API.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import feedly
import feeds
import sources
from store import RETENTION_DAYS, Store

KINDS = ("refresh", "backfill")
UNFINISHED = ("running", "stopping", "stopped", "interrupted")


# Fetching a single source (with fallbacks) lives in sources.py; the name is kept for callers and tests.
fetch_with_fallback = sources.fetch


class Scraper:
    def __init__(self, stores: dict[str, Store], state_path: Path | None = None,
                 interval_minutes: float = 15, auto: bool = True, log: Callable[[str], None] | None = None,
                 on_finish: Callable[[list[str]], None] | None = None):
        self.stores = stores
        self.on_finish = on_finish  # called with the run's verticals when a run ends (e.g. rebuild digests)
        self.state_path = state_path
        self.log = log or (lambda msg: None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = {"auto": auto, "interval_minutes": interval_minutes, "run": None, "last_run": None,
                      "next_auto_at": None}
        self._load()
        if self.state.get("run") and self.state["run"]["status"] in ("running", "stopping"):
            self.state["run"]["status"] = "interrupted"  # the process ended while it was running
            self._save()

    # --- persistence ---------------------------------------------------------------------------

    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.state.update({k: saved[k] for k in ("run", "last_run") if k in saved})
                # Schedule changes made through the API outlive restarts; otherwise CLI flags apply.
                for key, flag in (("auto", "auto_set"), ("interval_minutes", "interval_set")):
                    if saved.get(flag):
                        self.state[key], self.state[flag] = saved[key], True
            except (OSError, ValueError):
                pass  # unreadable state: start fresh

    def _save(self) -> None:
        if not self.state_path:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_path)

    # --- public API ----------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        with self._lock:
            run = self.state.get("run")
            held = self.state.get("held_backfill")
            if held and not self.running and not (run and run["status"] in UNFINISHED):
                run = held                     # a stopped backfill waiting behind the scheduled refreshes
            return {
                "state": ("stopping" if self._stop.is_set() else "running") if self.running else "idle",
                "run": dict(run) if run else None,
                "last_run": self.state.get("last_run"),
                "resumable": bool(run and not self.running and run["status"] in UNFINISHED),
                "auto": self.state["auto"],
                "interval_minutes": self.state["interval_minutes"],
                "next_auto_at": self.state.get("next_auto_at") if self.state["auto"] else None,
            }

    def start(self, kind: str = "refresh", verticals: list[str] | None = None, days: float | None = None,
              resume: bool = True, trigger: str = "api", wait: bool = False) -> tuple[bool, dict | str]:
        """Start a run (or resume the unfinished one). Returns (ok, status | error message)."""
        if kind not in KINDS:
            return False, f"kind must be one of {', '.join(KINDS)}"
        verticals = list(verticals or self.stores)
        bad = [v for v in verticals if v not in self.stores]
        if bad:
            return False, f"unknown vertical(s): {', '.join(bad)}"
        if kind == "backfill":
            if not days or days <= 0:
                return False, "backfill needs days > 0"
            days = min(float(days), RETENTION_DAYS)
        with self._lock:
            if self.running:
                return False, "a scrape is already running; stop it first"
            prev, held = self.state.get("run"), self.state.get("held_backfill")
            cand = prev if prev and prev["status"] in UNFINISHED and prev["kind"] == kind else (
                held if kind == "backfill" else None)
            if resume and cand and (kind != "backfill" or cand.get("days") == days) \
                    and set(verticals) <= set(cand["verticals"]):
                run = cand
                if cand is held:
                    self.state.pop("held_backfill")
                    if prev and prev is not held and prev["status"] in UNFINISHED:
                        prev["status"] = "abandoned"           # a stopped refresh: recorded, not silently dropped
                        self.state["last_run"] = prev
                run.update(status="running", resumed_at=time.time(), trigger=trigger, error=None)
                run["resumes"] = run.get("resumes", 0) + 1
            else:
                if prev and prev["status"] in UNFINISHED:
                    if prev["kind"] == "backfill" and kind == "refresh":
                        self.state["held_backfill"] = prev     # refreshes don't abandon a resumable backfill
                    else:
                        prev["status"] = "abandoned"
                        self.state["last_run"] = prev
                if kind == "backfill" and held and held is not prev:
                    held["status"] = "abandoned"               # a new backfill replaces the held one
                    self.state.pop("held_backfill", None)
                run = {"id": f"{kind}-{int(time.time() * 1000)}", "kind": kind, "verticals": verticals,
                       "days": days, "status": "running", "trigger": trigger, "started_at": time.time(),
                       "finished_at": None, "total": 0, "done": 0, "failed": 0, "current": [], "error": None}
            self.state["run"] = run
            self._stop.clear()
            self._save()
            self._thread = threading.Thread(target=self._execute, args=(run,), daemon=True, name=f"scrape-{run['id']}")
            self._thread.start()
        if wait:
            self._thread.join()
        return True, self.status()

    def stop(self, wait: bool = False) -> tuple[bool, dict | str]:
        """Ask the running scrape to stop after the feeds in progress (progress is kept)."""
        with self._lock:
            if not self.running:
                return False, "no scrape is running"
            self._stop.set()
            self.state["run"]["status"] = "stopping"
            self._save()
            thread = self._thread
        if wait:
            thread.join()
        return True, self.status()

    def set_schedule(self, auto: bool | None = None, interval_minutes: float | None = None) -> dict:
        with self._lock:
            if auto is not None:
                self.state["auto"], self.state["auto_set"] = auto, True
            if interval_minutes is not None:
                self.state["interval_minutes"], self.state["interval_set"] = interval_minutes, True
            self.state["next_auto_at"] = time.time() + self.state["interval_minutes"] * 60
            self._save()
        return self.status()

    def resume_interrupted(self) -> bool:
        """On startup: resume a run the process was killed in the middle of."""
        run = self.state.get("run")
        if run and run["status"] == "interrupted":
            self.log(f"Resuming interrupted {run['kind']} run {run['id']} ({run['done']}/{run['total']} feeds done)")
            ok, _ = self.start(run["kind"], run["verticals"], run.get("days"), resume=True, trigger="startup-resume")
            return ok
        return False

    def loop(self) -> None:
        """Scheduler: start a refresh every interval while auto is on and nothing is running."""
        with self._lock:
            if not self.state.get("next_auto_at"):
                self.state["next_auto_at"] = time.time()  # first refresh right away
        while True:
            try:
                with self._lock:
                    due = self.state["auto"] and not self.running and time.time() >= (self.state.get("next_auto_at") or 0)
                if due:
                    with self._lock:
                        self.state["next_auto_at"] = time.time() + self.state["interval_minutes"] * 60
                    self.start("refresh", trigger="schedule")
            except Exception:  # noqa: BLE001 - the scheduler must never die
                self.log("Scheduler error:\n" + traceback.format_exc())
            time.sleep(1)

    # --- the run itself ------------------------------------------------------------------------

    def _progress(self, run: dict, label: str, *, started: bool = False, ok: bool = True) -> None:
        with self._lock:
            if started:
                run["current"].append(label)
            else:
                if label in run["current"]:
                    run["current"].remove(label)
                run["done"] += 1          # finished feeds, failures included
                if not ok:
                    run["failed"] += 1
            self._save()

    def _execute(self, run: dict) -> None:
        try:
            jobs = []
            total = 0
            for key in run["verticals"]:
                store = self.stores[key]
                for feed in store.list_feeds():
                    total += 1
                    if feed.get("last_run") != run["id"]:
                        jobs.append((key, store, feed))
            with self._lock:
                run["total"] = total
                run["done"] = total - len(jobs)  # feeds this run already finished before a stop/crash
                run["failed"] = run.get("failed", 0) if run.get("resumes") else 0
                run["current"] = []
                self._save()
            since = time.time() - run["days"] * 86400 if run["kind"] == "backfill" else None
            work = (lambda job: self._backfill_one(run, since, *job)) if run["kind"] == "backfill" \
                else (lambda job: self._refresh_one(run, *job))
            with ThreadPoolExecutor(max_workers=2 if run["kind"] == "backfill" else 8) as pool:
                list(pool.map(work, jobs))
            with self._lock:
                run["status"] = "stopped" if self._stop.is_set() else "completed"
                if run["status"] == "completed":
                    run["finished_at"] = time.time()
                    self.state["last_run"] = dict(run)
                self._save()
            self.log(f"Scrape {run['id']} {run['status']}: {run['done']}/{run['total']} feeds, {run['failed']} failed")
            if self.on_finish:
                self.on_finish(list(run["verticals"]))
        except Exception as e:  # noqa: BLE001 - record the failure instead of dying silently
            with self._lock:
                run["status"], run["error"] = "interrupted", f"{type(e).__name__}: {e}"
                self._save()
            self.log("Scrape run crashed (resumable):\n" + traceback.format_exc())

    def _label(self, key: str, feed: dict) -> str:
        return f"{key}: {feed.get('name') or feed.get('title') or feed['url']}"

    def _refresh_one(self, run: dict, key: str, store: Store, feed: dict) -> None:
        if self._stop.is_set():
            return
        label = self._label(key, feed)
        self._progress(run, label, started=True)
        ok = True
        try:
            # After a good Feedly fetch, ask Feedly only for what it crawled since (an hour's overlap).
            since = (feed["last_fetched"] - 3600 if feed.get("via") == "feedly" and not feed.get("last_error")
                     and feed.get("last_fetched") else None)
            parsed, via, note = fetch_with_fallback(feed["url"], views_checked_at=feed.get("views_checked_at") or 0,
                                                    feedly_since=since)
            store.save_fetch(feed["id"], parsed, None, via, note)
        except feeds.FeedError as e:
            ok = False
            store.save_fetch(feed["id"], None, str(e))
        except Exception as e:  # noqa: BLE001
            ok = False
            try:
                store.save_fetch(feed["id"], None, f"Unexpected error: {e}")
            except Exception:  # noqa: BLE001
                pass
        store.mark_run(feed["id"], run["id"])  # finished (successfully or not) for this run
        self._progress(run, label, ok=ok)

    def _backfill_one(self, run: dict, since: float, key: str, store: Store, feed: dict) -> None:
        if self._stop.is_set():
            return
        label = self._label(key, feed)
        done_to = feed.get("backfilled_since")
        if done_to is not None and done_to <= since + 3600:  # an earlier backfill already covers this
            store.mark_run(feed["id"], run["id"])
            self._progress(run, label, started=True)
            self._progress(run, label)
            return
        self._progress(run, label, started=True)
        got, buf = 0, []

        def save():
            nonlocal got, buf
            if buf:
                store.save_fetch(feed["id"], feedly.to_parsed(buf, feed_url=feed["url"]), feed.get("last_error"),
                                 feed.get("via") or "feedly")
                got += len(buf)
                buf = []

        try:
            for e in feedly.history(feed["url"], since=since):
                buf.append(e)
                if len(buf) >= 100:  # save as we go: a later failure keeps what we have
                    save()
                if self._stop.is_set():
                    save()
                    with self._lock:
                        run["current"].remove(label)
                    return  # not marked: resuming continues this feed
        except feedly.RateLimited as e:
            save()
            with self._lock:
                run["error"] = f"{e}. Stopped after {run['done']} feeds; start again to resume."
            self._stop.set()  # Feedly said no for minutes: stop the run, keep it resumable
            with self._lock:
                run["current"].remove(label)
                self._save()
            self.log(f"  {label}: rate-limited after {got} items (kept); stopping run, resume later")
            return
        except Exception as e:  # noqa: BLE001
            save()
            self.log(f"  {label}: Feedly error after {got} items: {e}")
            store.mark_run(feed["id"], run["id"])
            self._progress(run, label, ok=False)
            return
        save()
        store.mark_backfilled(feed["id"], since)
        store.mark_run(feed["id"], run["id"])
        self.log(f"  {label}: {got} items")
        self._progress(run, label)
