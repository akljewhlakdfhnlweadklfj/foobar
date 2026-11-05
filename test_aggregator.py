"""Tests: python3 -m unittest -v"""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app
import feeds
import matching
from matching import Item

NOW = time.time()


def items(rows):
    return [Item(id=i, feed_id=f, title=t, published=NOW) for i, (f, t) in enumerate(rows)]


class MatchingTests(unittest.TestCase):
    def cluster_titles(self, rows, **kw):
        stories = matching.cluster(items(rows), **kw)
        return sorted((sorted(i.title for i in s.items) for s in stories), key=len, reverse=True)

    def test_same_story_different_wording(self):
        rows = [
            (1, "Nintendo Switch 2 price revealed: $449 at launch"),
            (2, "Switch 2 will cost $449, Nintendo confirms"),
            (3, "Nintendo confirms Switch 2 price of $449"),
            (4, "Hollow Knight Silksong finally gets a release date"),
            (5, "Ubisoft announces layoffs at Toronto studio"),
            (6, "Ubisoft Toronto hit by layoffs"),
        ]
        groups = [set(g) for g in self.cluster_titles(rows)]
        self.assertEqual(len(groups[0]), 3)  # Switch 2 price
        self.assertIn({"Ubisoft Toronto hit by layoffs", "Ubisoft announces layoffs at Toronto studio"}, groups)
        self.assertIn({"Hollow Knight Silksong finally gets a release date"}, groups)

    def test_shared_common_word_not_enough(self):
        rows = [
            (1, "Nintendo Direct announced for Thursday"),
            (2, "Nintendo sues Palworld developer over patents"),
            (3, "Nintendo Switch Online adds GameCube classics"),
        ]
        self.assertTrue(all(len(g) == 1 for g in self.cluster_titles(rows)))

    def test_same_feed_items_not_linked(self):
        rows = [(1, "GTA 6 delayed to 2026"), (1, "GTA 6 delayed again to 2026")]
        self.assertTrue(all(len(g) == 1 for g in self.cluster_titles(rows)))

    def test_coverage_counts_distinct_feeds(self):
        rows = [(1, "Valve announces Steam Machine"), (2, "Valve announces new Steam Machine hardware"),
                (2, "Valve's Steam Machine announced"), (3, "Steam Machine announced by Valve")]
        stories = matching.cluster(items(rows))
        self.assertEqual(max(s.coverage for s in stories), 3)

    def test_average_linkage_prevents_chaining(self):
        # B bridges A and C lexically, but A and C are unrelated stories.
        its = items([
            (1, "Capcom announces Resident Evil Requiem DLC"),
            (2, "Capcom announces Resident Evil Requiem DLC and Monster Hunter Wilds expansion"),
            (3, "Monster Hunter Wilds expansion announced by Capcom"),
        ])
        sims = {(0, 1): 0.5, (1, 2): 0.5, (0, 2): 0.0}
        self.assertEqual(len(matching.agglomerate(its, sims, 0.4, linkage="single")), 1)
        self.assertEqual(len(matching.agglomerate(its, sims, 0.4, linkage="average")), 2)

    def test_tokenize(self):
        self.assertEqual(matching.tokenize("Sony's PlayStation raises prices!"),
                         matching.tokenize("Sony hikes the price of PlayStation"))
        self.assertEqual(matching.tokenize("Game announced"), matching.tokenize("games announce"))


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Test &amp; Co</title>
<item><title>First &lt;b&gt;story&lt;/b&gt;</title><link>https://a.example/1</link>
<guid>a1</guid><pubDate>Tue, 04 Nov 2025 10:00:00 GMT</pubDate></item>
<item><title><![CDATA[Second story]]></title><link>https://a.example/2</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Atom Test</title><entry><title>Atom story</title><id>tag:x,1</id>
<link rel="alternate" href="https://b.example/1"/><updated>2025-11-04T10:00:00Z</updated></entry></feed>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>RDF Test</title></channel>
<item rdf:about="https://c.example/1"><title>RDF story</title><link>https://c.example/1</link>
<dc:date>2025-11-04T10:00:00+00:00</dc:date></item></rdf:RDF>"""


class ParserTests(unittest.TestCase):
    def test_rss2(self):
        f = feeds.parse(RSS)
        self.assertEqual(f.title, "Test & Co")
        self.assertEqual([e.title for e in f.entries], ["First story", "Second story"])
        self.assertEqual(f.entries[0].published, 1762250400.0)
        self.assertEqual(f.entries[1].guid, "https://a.example/2")

    def test_atom(self):
        f = feeds.parse(ATOM)
        self.assertEqual((f.title, f.entries[0].title, f.entries[0].link),
                         ("Atom Test", "Atom story", "https://b.example/1"))
        self.assertEqual(f.entries[0].published, 1762250400.0)

    def test_rdf(self):
        f = feeds.parse(RDF)
        self.assertEqual((f.title, f.entries[0].title), ("RDF Test", "RDF story"))

    def test_garbage(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse(b"<html><body>not a feed</body></html>")


class WeightTests(unittest.TestCase):
    def test_score_and_lead_use_weights(self):
        story = matching.Story(items=[
            Item(id=1, feed_id=1, title="Small blog: Studio X shuts down", weight=1),
            Item(id=2, feed_id=2, title="Studio X closes after 20 years", weight=5),
            Item(id=3, feed_id=2, title="Studio X closes (update)", weight=5),  # same outlet counts once
            Item(id=4, feed_id=3, title="Studio X is closing", weight=3),
        ])
        self.assertEqual(story.score, 9)
        self.assertEqual(matching.lead_item(story).feed_id, 2)

    def test_seed_weights_parsed_and_ui_edits_kept(self):
        tmp = Path(tempfile.mkdtemp())
        seed = tmp / "seed.txt"
        seed.write_text("https://a.example/rss  # Outlet A | w=5 | notes\nhttps://b.example/rss # Outlet B\n")
        feeds_ = app.read_feed_list(seed)
        self.assertEqual((feeds_["https://a.example/rss"].name, feeds_["https://a.example/rss"].weight), ("Outlet A", 5.0))
        self.assertIsNone(feeds_["https://b.example/rss"].weight)
        store = app.Store(tmp / "x.db")
        store.add_feeds(feeds_)
        fa = next(f for f in store.list_feeds() if f["name"] == "Outlet A")
        store.set_weight(fa["id"], 2)
        store.sync_seed(feeds_)  # restart: the seed must not overwrite the UI edit
        self.assertEqual(store.get_feed(fa["id"])["weight"], 2)

    def test_every_seed_feed_has_a_weight(self):
        for v in app.VERTICALS:
            missing = [u for u, s in app.read_feed_list(app.FEEDS_DIR / f"{v.key}.txt").items() if s.weight is None]
            self.assertEqual(missing, [], v.key)


class OutletToggleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = app.Store(self.tmp / "t.db")
        now = time.time()
        for n in (1, 2, 3):
            fid = self.store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", 3)
            self.store.save_fetch(fid, feeds.ParsedFeed(f"Outlet {n}", [
                feeds.ParsedEntry(f"g{n}", f"Valve announces Steam Machine hardware {n}", "", now)]), None)

    def test_disabled_outlets_are_left_out_of_stories(self):
        res = app.build_stories(self.store, 3, 48, 0.15)
        self.assertEqual(res["stories"][0]["coverage"], 3)
        self.store.update_feed(2, enabled=False)
        self.assertEqual(app.build_stories(self.store, 3, 48, 0.15)["stories"], [])
        self.assertEqual(app.build_stories(self.store, 2, 48, 0.15)["stories"][0]["coverage"], 2)
        self.assertEqual(self.store.set_all_enabled(True), 3)
        self.assertEqual(app.build_stories(self.store, 3, 48, 0.15)["stories"][0]["coverage"], 3)

    def test_required_outlets_filter_stories(self):
        now = time.time()
        fid4 = self.store.add_feed("https://4.example/rss", "Outlet 4", 3)
        for n in (1, 2, 4):  # a second story, which Outlet 3 doesn't report
            fid = fid4 if n == 4 else n
            self.store.save_fetch(fid, feeds.ParsedFeed("x", [feeds.ParsedEntry(
                f"d{n}", f"Nintendo Direct dated for Thursday {n}", "", now)]), None)
        titles = lambda **kw: sorted(s["title"][:8] for s in app.build_stories(self.store, 3, 48, 0.12, **kw)["stories"])
        self.assertEqual(titles(), ["Nintendo", "Valve an"])
        self.store.update_feed(3, required=True)                 # Outlet 3 only reported the Valve story
        self.assertEqual(titles(), ["Valve an"])
        self.store.update_feed(fid4, required=True)              # any of {3, 4}: both stories
        self.assertEqual(titles(), ["Nintendo", "Valve an"])
        self.assertEqual(titles(require_all=True), [])           # no story has both 3 and 4
        self.store.add_phrase("Direct", 5)                       # watched stories still need a required outlet
        self.store.update_feed(fid4, required=False)
        self.assertEqual(titles(watched_any_coverage=True), ["Valve an"])
        self.assertEqual(self.store.clear_required(), 1)
        self.assertEqual(self.store.required_feed_ids(), set())

    def test_keyword_filter_limits_a_feeds_items(self):
        now = time.time()
        self.store.save_fetch(1, feeds.ParsedFeed("x", [
            feeds.ParsedEntry("m1", "Frieren anime gets season 3", "", now),
            feeds.ParsedEntry("m2", "Dandadan creator talks", "", now, "The mangaka discussed the next arc.")]), None)
        before = {i.title for i in self.store.items_since(0) if i.feed_id == 1}
        self.store.update_feed(1, only="manga, light novel")
        after = {i.title for i in self.store.items_since(0) if i.feed_id == 1}
        self.assertIn("Frieren anime gets season 3", before)
        self.assertEqual(after, {"Dandadan creator talks"})          # matched "mangaka" in the description
        self.store.update_feed(1, only="")                            # blank clears the filter
        self.assertEqual({i.title for i in self.store.items_since(0) if i.feed_id == 1}, before)
        seeds = app.read_feed_list(app.FEEDS_DIR / "manga.txt")
        self.assertTrue(seeds["http://www.animenewsnetwork.com/newsfeed/rss.xml"].only.startswith("manga"))

    def test_min_weight_controls_what_counts(self):
        self.store.update_feed(1, weight=5); self.store.update_feed(2, weight=1)
        story = lambda **kw: app.build_stories(self.store, 1, 48, 0.12, **kw)["stories"][0]
        s = story()
        self.assertEqual((s["coverage"], s["base_score"]), (3, 9))
        s = story(min_weight=3)                                   # outlet 2 (weight 1) no longer counts
        self.assertEqual((s["coverage"], s["base_score"], s["outlets"]), (2, 8, 3))
        self.assertEqual(sorted(x["counted"] for x in s["sources"]), [False, True, True])
        self.assertEqual(app.build_stories(self.store, 3, 48, 0.12, min_weight=3)["stories"], [])
        self.assertEqual(app.build_stories(self.store, 1, 48, 0.12, min_weight=6)["stories"], [])

    def test_patch_api_validates_weight_and_enabled(self):
        import json, threading, urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler({"games": self.store}, app.Scraper({}, auto=False)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}/api/games/feeds"

        def patch(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="PATCH",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)

        with mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            self.assertEqual(patch("/1", {"weight": 7.5})[1]["feed"]["weight"], 7.5)
            self.assertEqual(patch("/1", {"weight": 0})[1]["feed"]["weight"], 0)
            self.assertEqual(patch("/1", {"enabled": False})[1]["feed"]["enabled"], 0)
            for bad in ({"weight": 11}, {"weight": -1}, {"weight": "5"}, {"weight": True}, {"enabled": "no"}, {}):
                self.assertEqual(patch("/1", bad)[0], 400, bad)
            self.assertEqual(patch("/99", {"enabled": True})[0], 404)
            self.assertEqual(patch("", {"enabled": False}), (200, {"updated": 3}))
            self.assertEqual(patch("", {"weight": 3})[0], 400)
            self.assertEqual(patch("/1", {"required": True})[1]["feed"]["required"], 1)
            self.assertEqual(patch("/1", {"required": "yes"})[0], 400)
            self.assertEqual(patch("", {"required": False}), (200, {"updated": 1}))


class FeedlyThrottlingTests(unittest.TestCase):
    """Simulated 429s: no network, timings shrunk so the suite stays fast."""

    def setUp(self):
        import feedly
        self.feedly = feedly
        patches = [mock.patch.object(feedly, "MIN_INTERVAL", 0.0), mock.patch.object(feedly, "COOLDOWN_START", 0.2),
                   mock.patch.object(feedly, "COOLDOWN_MAX", 0.4), mock.patch.object(feedly, "MAX_WAIT", 1.0)]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        feedly._blocked_until = feedly._next_slot = feedly._cooldown = 0.0

    def _responses(self, *codes, retry_after=None):
        """Fake urlopen: each call pops the next status (429 or 200)."""
        import io, urllib.error
        from email.message import Message
        queue = list(codes)
        calls = []

        def fake(req, timeout=None):
            calls.append(time.monotonic())
            code = queue.pop(0) if queue else 200
            if code == 429:
                h = Message()
                if retry_after is not None:
                    h["Retry-After"] = str(retry_after)
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", h, io.BytesIO(b""))
            r = mock.MagicMock()
            r.__enter__.return_value = io.BytesIO(b'{"items": [], "title": "T"}')
            return r
        return fake, calls

    def test_429_pauses_then_retries(self):
        fake, calls = self._responses(429, 429, 200)
        with mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(self.feedly._get("streams/contents", {}), {"items": [], "title": "T"})
        self.assertEqual(len(calls), 3)
        self.assertGreaterEqual(calls[1] - calls[0], 0.19)   # first cooldown
        self.assertGreaterEqual(calls[2] - calls[1], 0.38)   # doubled
        self.assertEqual(self.feedly._cooldown, 0.0)          # reset after success

    def test_retry_after_header_is_honoured(self):
        fake, calls = self._responses(429, 200, retry_after=0.6)
        with mock.patch("urllib.request.urlopen", fake):
            self.feedly._get("x", {})
        self.assertGreaterEqual(calls[1] - calls[0], 0.58)

    def test_gives_up_after_max_wait_and_no_wait_mode_fails_fast(self):
        fake, _ = self._responses(*[429] * 50)
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(self.feedly.RateLimited):
                self.feedly._get("x", {})
            t = time.monotonic()
            with self.assertRaises(self.feedly.RateLimited):   # fallback path: don't sit out the cooldown
                self.feedly.recent("https://a.example/rss")
            self.assertLess(time.monotonic() - t, 0.1)

    def test_fallback_reports_rate_limit_and_keeps_headlines(self):
        with mock.patch.object(feeds, "fetch", side_effect=feeds.FeedError("blocked")), \
             mock.patch("feedly.recent", side_effect=self.feedly.RateLimited(30)):
            with self.assertRaises(feeds.FeedError) as cm:
                app.fetch_with_fallback("https://a.example/rss")
        self.assertIn("rate-limited", str(cm.exception))

    def test_backfill_keeps_partial_results_and_resumes(self):
        store = app.Store(Path(tempfile.mkdtemp()) / "b.db")
        a = store.add_feed("https://a.example/rss", "A")
        b = store.add_feed("https://b.example/rss", "B")
        old = (time.time() - 2 * 86400) * 1000
        entry = lambda u, i: {"id": f"{u}{i}", "originId": f"{u}{i}", "title": f"Story {i} {u}", "published": old,
                              "crawled": old, "alternate": [{"href": f"https://x.example/{i}"}]}

        def throttled(url, since=0):
            yield entry(url, 1); yield entry(url, 2)
            if "b.example" in url:
                raise self.feedly.RateLimited(60)

        with mock.patch("feedly.history", throttled), mock.patch("builtins.print"):
            first = app.backfill({"games": store}, 3)
        self.assertEqual(first["status"], "stopped")          # rate-limited: stopped, resumable
        self.assertIn("resume", first["error"])
        self.assertEqual({store.get_feed(a)["item_count"], store.get_feed(b)["item_count"]}, {2})  # partial kept

        calls = []
        def recording(url, since=0):
            calls.append(url); yield from ()
        sc = app.Scraper({"games": store}, auto=False)
        sc.state["run"] = first                                # same run: resume it
        with mock.patch("feedly.history", recording):
            ok, _ = sc.start("backfill", ["games"], 3, wait=True)
        self.assertTrue(ok)
        self.assertEqual(calls, ["https://b.example/rss"])     # A was finished, only B is redone
        self.assertEqual(sc.status()["run"]["status"], "completed")
        calls.clear()
        with mock.patch("feedly.history", recording), mock.patch("builtins.print"):
            app.backfill({"games": store}, 2.5)                # new run: both already cover 2.5 days
        self.assertEqual(calls, [])


class HttpTestCase(unittest.TestCase):
    """Starts the real handler on a free port."""

    def start_server(self, stores, scraper=None, security=None):
        import threading
        from http.server import ThreadingHTTPServer
        self.scraper = scraper or app.Scraper(stores, auto=False)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(stores, self.scraper, security))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_address[1]}"
        env = mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        env.start(); self.addCleanup(env.stop)

    def call(self, method, path, body=None, headers=None, raw=None):
        import json, urllib.request
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class SecurityTests(HttpTestCase):
    def setUp(self):
        self.store = app.Store(Path(tempfile.mkdtemp()) / "s.db")
        self.fid = self.store.add_feed("https://a.example/rss", "A")
        self.start_server({"games": self.store}, security=app.Security(api_token="s3cret"))

    def test_cross_site_and_non_json_writes_are_refused(self):
        self.assertEqual(self.call("POST", "/api/games/phrases", raw=b'{"phrase": "x"}',
                                   headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.call("PATCH", f"/api/games/feeds/{self.fid}", {"weight": 3},
                                   headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("PATCH", f"/api/games/feeds/{self.fid}", {"weight": 3},
                                   headers={"Host": "rebound.evil.example"})[0], 403)
        self.assertEqual(self.call("PATCH", f"/api/games/feeds/{self.fid}", {"weight": 3},
                                   headers={"Origin": self.base})[0], 200)       # same-origin page: fine
        self.assertEqual(self.call("GET", "/api/games/feeds", headers={"Host": "rebound.evil.example"})[0], 200)

    def test_api_token_allows_remote_orchestrator(self):
        h = {"Host": "orchestrator.internal", "Authorization": "Bearer s3cret"}
        self.assertEqual(self.call("POST", "/api/scrape/schedule", {"auto": False}, headers=h)[0], 200)
        h["Authorization"] = "Bearer wrong"
        self.assertEqual(self.call("POST", "/api/scrape/schedule", {"auto": False}, headers=h)[0], 403)

    def test_bad_bodies_get_clean_errors(self):
        self.assertEqual(self.call("PATCH", f"/api/games/feeds/{self.fid}", raw=b"[1]",
                                   headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("PATCH", f"/api/games/feeds/{self.fid}", raw=b"{nope",
                                   headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.call("POST", "/api/games/phrases", raw=b"x" * (70 * 1024),
                                   headers={"Content-Type": "application/json"})[0], 413)

    def test_private_feed_urls_are_refused(self):
        code, body = self.call("POST", "/api/games/feeds", {"url": "http://127.0.0.1:9/rss"})
        self.assertEqual(code, 400)
        self.assertIn("private or local", body["error"])


class ScrapeControlTests(HttpTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = app.Store(self.tmp / "g.db")
        for n in range(20):
            self.store.add_feed(f"https://{n}.example/rss", f"F{n}")
        self.fetched = []
        def slow_fetch(url):
            self.fetched.append(url); time.sleep(0.4)
            return feeds.ParsedFeed("T", [feeds.ParsedEntry("g", "Headline " + url, url, time.time())]), "direct", None
        p = mock.patch("scraper.fetch_with_fallback", slow_fetch)
        p.start(); self.addCleanup(p.stop)

    def make(self):
        return app.Scraper({"games": self.store}, self.tmp / "state.json", auto=False)

    def wait_idle(self, sc):
        for _ in range(200):
            if not sc.running:
                return
            time.sleep(0.05)

    def test_start_stop_status_and_resume_over_http(self):
        self.start_server({"games": self.store}, scraper=self.make())
        code, st = self.call("POST", "/api/scrape/start", {"verticals": ["games"]})
        self.assertEqual((code, st["state"]), (202, "running"))
        self.assertEqual(self.call("POST", "/api/scrape/start", {})[0], 409)   # already running
        time.sleep(0.2)
        self.assertEqual(self.call("POST", "/api/scrape/stop", {})[0], 202)
        self.wait_idle(self.scraper)
        st = self.call("GET", "/api/scrape/status")[1]
        self.assertEqual((st["state"], st["run"]["status"], st["resumable"]), ("idle", "stopped", True))
        done_before = st["run"]["done"]
        self.assertLess(done_before, 20)
        first_run = st["run"]["id"]
        self.call("POST", "/api/scrape/start", {})                             # resumes by default
        self.wait_idle(self.scraper)
        st = self.call("GET", "/api/scrape/status")[1]
        self.assertEqual((st["run"]["id"], st["run"]["status"], st["run"]["done"]), (first_run, "completed", 20))
        self.assertEqual(sorted(self.fetched), sorted(set(self.fetched)))       # no feed fetched twice

    def test_run_killed_midway_resumes_on_startup(self):
        sc = self.make()
        sc.start("refresh", ["games"])
        time.sleep(0.2)
        # Simulate the process dying: the state file still says "running".
        state = json.loads((self.tmp / "state.json").read_text())
        sc.stop(wait=True)
        state["run"]["status"] = "running"
        (self.tmp / "state.json").write_text(json.dumps(state))
        already = [f for f in self.store.list_feeds() if f["last_run"] == state["run"]["id"]]
        self.fetched.clear()
        sc2 = self.make()
        self.assertEqual(sc2.status()["run"]["status"], "interrupted")
        self.assertTrue(sc2.resume_interrupted())
        self.wait_idle(sc2)
        self.assertEqual(sc2.status()["run"]["status"], "completed")
        self.assertEqual(len(self.fetched), 20 - len(already))                 # only the unfinished feeds
        self.assertGreater(len(already), 0)

    def test_schedule_toggle_persists(self):
        sc = self.make()
        sc.set_schedule(auto=False, interval_minutes=30)
        sc2 = app.Scraper({"games": self.store}, self.tmp / "state.json", auto=True, interval_minutes=15)
        self.assertEqual((sc2.status()["auto"], sc2.status()["interval_minutes"]), (False, 30))


class PhraseTests(HttpTestCase):
    def setUp(self):
        self.store = app.Store(Path(tempfile.mkdtemp()) / "p.db")
        now = time.time()
        for n in (1, 2, 3):
            fid = self.store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", 3)
            self.store.save_fetch(fid, feeds.ParsedFeed("x", [
                feeds.ParsedEntry(f"a{n}", f"Valve announces Steam Machine hardware {n}", "", now,
                                  "The console-like PC ships in early 2027." if n == 2 else ""),
                feeds.ParsedEntry(f"b{n}", f"Nintendo Direct dated for Thursday {n}", "", now)]), None)
        self.start_server({"games": self.store})

    def stories(self, **q):
        from urllib.parse import urlencode
        return self.call("GET", "/api/games/stories?" + urlencode({"min_feeds": 3, **q}))[1]["stories"]

    def test_phrase_in_description_adds_bonus_and_is_reported(self):
        base = {s["title"][:13]: s["score"] for s in self.stories()}
        code, ph = self.call("POST", "/api/games/phrases", {"phrase": "early   2027", "bonus": 7})
        self.assertEqual((code, ph["phrase"]["phrase"]), (201, "early 2027"))
        steam = next(s for s in self.stories() if s["title"].startswith("Valve"))
        self.assertEqual((steam["bonus"], steam["score"]), (7, base["Valve announc"] + 7))
        self.assertEqual([w["phrase"] for w in steam["watched"]], ["early 2027"])
        self.assertTrue(any(src["watched"] for src in steam["sources"]))
        self.assertEqual(self.stories()[0]["title"][:5], "Valve")              # bonus lifts it to the top
        self.assertEqual([s["title"][:5] for s in self.stories(only_watched=1)], ["Valve"])
        self.assertEqual(self.call("POST", "/api/games/phrases", {"phrase": "EARLY 2027"})[0], 409)

    def test_whole_words_only_and_watched_any_coverage(self):
        self.call("POST", "/api/games/phrases", {"phrase": "Direct", "bonus": 1})
        self.call("POST", "/api/games/phrases", {"phrase": "machin"})          # partial word: no match
        nin = next(s for s in self.stories() if s["title"].startswith("Nintendo"))
        self.assertEqual([w["phrase"] for w in nin["watched"]], ["Direct"])
        steam = next(s for s in self.stories() if s["title"].startswith("Valve"))
        self.assertEqual(steam["watched"], [])
        self.assertEqual(len(self.stories(min_feeds=9)), 0)
        self.assertEqual([s["title"][:8] for s in self.stories(min_feeds=9, watched_any=1)], ["Nintendo"])

    def test_phrase_api_validation_and_delete(self):
        self.assertEqual(self.call("POST", "/api/games/phrases", {"phrase": "  "})[0], 400)
        self.assertEqual(self.call("POST", "/api/games/phrases", {"phrase": "x", "bonus": 1000})[0], 400)
        pid = self.call("POST", "/api/games/phrases", {"phrase": "Switch 2"})[1]["phrase"]["id"]
        self.assertEqual(self.call("PATCH", f"/api/games/phrases/{pid}", {"bonus": 12})[1]["phrase"]["bonus"], 12)
        self.assertEqual(self.call("DELETE", f"/api/games/phrases/{pid}")[0], 200)
        self.assertEqual(self.call("GET", "/api/games/phrases")[1]["phrases"], [])


class SettingsApiTests(HttpTestCase):
    def setUp(self):
        self.store = app.Store(Path(tempfile.mkdtemp()) / "games.db")
        now = time.time()
        for n in (1, 2, 3):
            fid = self.store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", 3)
            self.store.save_fetch(fid, feeds.ParsedFeed("x", [
                feeds.ParsedEntry(f"a{n}", f"Valve announces Steam Machine hardware {n}", "", now)]), None)
        self.start_server({"games": self.store})

    def test_defaults_save_validate_reset(self):
        code, d = self.call("GET", "/api/games/settings")
        self.assertEqual((code, d["settings"]["min_feeds"], d["defaults"]["min_feeds"]), (200, 4, 4))
        self.assertEqual(self.call("GET", "/api/games/stories")[1]["stories"], [])          # 3 outlets < 4
        code, d = self.call("PATCH", "/api/games/settings", {"min_feeds": 3, "only_watched": False})
        self.assertEqual((code, d["settings"]["min_feeds"]), (200, 3))
        res = self.call("GET", "/api/games/stories")[1]
        self.assertEqual((len(res["stories"]), res["settings"]["min_feeds"]), (1, 3))       # saved value used
        self.assertEqual(len(self.call("GET", "/api/games/stories?min_feeds=9")[1]["stories"]), 0)  # query wins
        for bad in ({"min_feeds": 0}, {"min_feeds": "3"}, {"threshold": 2}, {"only_watched": 1}, {"nope": 1}):
            self.assertEqual(self.call("PATCH", "/api/games/settings", bad)[0], 400, bad)
        self.assertEqual(self.call("PATCH", "/api/games/settings", {"reset": True})[1]["settings"]["min_feeds"], 4)
        self.assertEqual(self.store.get_settings(), {})


class SeedCacheTests(unittest.TestCase):
    def test_export_load_roundtrip_only_into_empty_vertical(self):
        import seedcache
        tmp = Path(tempfile.mkdtemp())
        src = app.Store(tmp / "src.db")
        fid = src.add_feed("https://a.example/rss", "A")
        old, now = time.time() - 30 * 86400, time.time()
        src.save_fetch(fid, feeds.ParsedFeed("", [feeds.ParsedEntry("g1", "Fresh story", "https://a/1", now - 3600, "desc"),
                                                   feeds.ParsedEntry("g2", "Recent story", "https://a/2", now - 5 * 86400)]), None)
        cache = tmp / "a.jsonl.gz"
        self.assertEqual(seedcache.export(src, "games", cache), 2)
        dst = app.Store(tmp / "dst.db")
        dst.add_feed("https://a.example/rss", "A")
        dst.add_feed("https://unrelated.example/rss", "B")
        self.assertTrue(seedcache.store_is_empty(dst))
        self.assertEqual(seedcache.load(dst, cache), (2, 1))
        items = {i.title: i for i in dst.items_since(0)}
        self.assertEqual(items["Fresh story"].description, "desc")
        self.assertFalse(seedcache.store_is_empty(dst))
        self.assertIsNotNone(dst.list_feeds()[0]["backfilled_since"])   # backfill won't refetch this
        self.assertIsNotNone(old)

    def test_shipped_caches_load_for_their_seed_feeds(self):
        import seedcache
        stores = app.open_stores(Path(tempfile.mkdtemp()))
        for key in ("anime", "manga"):
            if seedcache.cache_path(key).exists():
                self.assertFalse(seedcache.store_is_empty(stores[key]), key)


class HeadlineAndImageTests(unittest.TestCase):
    def test_shortest_full_headline_is_used(self):
        story = matching.Story(items=[
            Item(id=1, feed_id=1, title="Review: The Legend of Zelda: Ocarina of Time Remake - One of the Best", weight=5),
            Item(id=2, feed_id=2, title="Ocarina remake reviewed", weight=2),          # 3 words: too short
            Item(id=3, feed_id=3, title="Zelda: Ocarina of Time remake review roundup", weight=3),
            Item(id=4, feed_id=4, title="Ocarina of Time remake: our review", weight=1, image="https://x/i.jpg"),
        ])
        self.assertEqual(app.headline_item(story).id, 4)
        self.assertEqual(app.headline_item(story, min_weight=2).id, 3)               # outlet 4 doesn't count
        labelled = matching.Story(items=[Item(id=1, feed_id=1, title="Opinion | The end of aid to Israel?"),
                                         Item(id=2, feed_id=2, title="US will stop $4B in aid to Israel, Trump says")])
        self.assertEqual(app.headline_item(labelled).id, 2)

    def test_images_from_rss_atom_and_feedly(self):
        rss = b"""<rss xmlns:media="http://search.yahoo.com/mrss/"><channel>
          <item><title>A</title><link>https://s/1</link><media:content url="https://cdn/a.jpg" medium="image"/></item>
          <item><title>B</title><link>https://s/2</link><enclosure url="/img/b.png" type="image/png"/></item>
          <item><title>C</title><link>https://s/3</link><description>&lt;p&gt;&lt;img src="https://cdn/c.webp"&gt; hi&lt;/p&gt;</description></item>
          <item><title>D</title><link>https://s/4</link><enclosure url="https://s/pod.mp3" type="audio/mpeg"/></item>
        </channel></rss>"""
        imgs = [e.image for e in feeds.parse(rss, base_url="https://s/feed").entries]
        self.assertEqual(imgs, ["https://cdn/a.jpg", "https://s/img/b.png", "https://cdn/c.webp", ""])
        import feedly
        self.assertEqual(feedly.entry_image({"visual": {"url": "https://v/x.jpg"}}), "https://v/x.jpg")
        self.assertEqual(feedly.entry_image({"visual": {"url": "none"}, "thumbnail": [{"url": "https://t/y.jpg"}]}),
                         "https://t/y.jpg")

    def test_story_output_has_image_and_description_fallback(self):
        store = app.Store(Path(tempfile.mkdtemp()) / "i.db")
        now = time.time()
        for n, (title, desc, img) in enumerate([("Valve announces new Steam Machine hardware today", "", ""),
                                                 ("Valve announces Steam Machine hardware", "", ""),
                                                 ("Valve's Steam Machine hardware announced by the company", "Long text.", "https://i/1.jpg")], 1):
            fid = store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", n)
            store.save_fetch(fid, feeds.ParsedFeed("x", [feeds.ParsedEntry(f"g{n}", title, f"https://{n}/a", now, desc, img)]), None)
        s = app.build_stories(store, 3, 48, 0.12)["stories"][0]
        self.assertEqual(s["title"], "Valve announces Steam Machine hardware")        # shortest
        self.assertEqual((s["lead"]["description"], s["lead"]["image"]), ("Long text.", "https://i/1.jpg"))


class KeywordTests(unittest.TestCase):
    def test_story_keywords_ordered_by_prominence(self):
        def story(title_list, score=10):
            return {"title": title_list[0], "score": score, "sources": [{"title": t} for t in title_list]}
        stories = [
            story(["Bethesda reveals Elder Scrolls 6 is called Sentinel", "Elder Scrolls 6 gets its name: Sentinel",
                   "The Elder Scrolls VI: Sentinel officially revealed"]),
            story(["Valve announces Steam Machine", "Steam Machine announced by Valve", "Valve's new Steam Machine"]),
            story(["Elder Scrolls Online gets an update", "Elder Scrolls Online patch notes"]),
        ]
        app.story_terms(stories)
        kw = [k["term"] for k in stories[0]["keywords"]]
        self.assertEqual(set(kw[:2]), {"Sentinel", "Elder Scrolls"})   # in every headline; phrase replaces its words
        self.assertNotIn("Elder", kw)
        self.assertEqual(stories[0]["keywords"][0]["weight"], 1.0)
        self.assertTrue(all(a["weight"] >= b["weight"] for a, b in zip(stories[0]["keywords"], stories[0]["keywords"][1:])))

    def test_lead_keyword_is_noun_like(self):
        def story(title_list):
            return {"title": title_list[0], "score": 10, "sources": [{"title": t} for t in title_list]}
        gov = story(["Record number of female governors elected", "Female governors elected in record numbers",
                     "Record number of women elected governor"])
        app.story_terms([gov])
        self.assertNotIn(gov["keywords"][0]["term"].lower(), ("record", "female", "elected"))
        self.assertTrue(app._noun_like(("ubisoft",), "Ubisoft", True))
        self.assertFalse(app._noun_like(("black",), "Black", True))           # capitalised adjective
        self.assertFalse(app._noun_like(("delay",), "delayed", False))
        self.assertTrue(app._noun_like(("name", "god", "of", "war"), "God of War", False))
        self.assertEqual(sorted(app._name_candidates("Dragon’s Dogma 2 Drops Surprise Collab").values(), key=len)[-1],
                         "Dragon’s Dogma 2")

    def test_proper_noun_at_headline_start_leads(self):
        def story(title_list):
            return {"title": title_list[0], "score": 10, "sources": [{"title": t} for t in title_list]}
        suda = story([
            "‘Travis Touchdown will definitely be back’: Suda51 is planning something for No More Heroes’ 20th anniversary",
            "Suda51 Wants A Now-Independent Grasshopper To Bring Back No More Heroes (And One Of His Earliest Games)",
            "Travis Touchdown 'Will Definitely Be Back' for No More Heroes 20th Anniversary",
            "Suda51 teases “something special” for No More Heroes’ 20th anniversary",
            "Suda51 says No More Heroes' Travis Touchdown \"is definitely coming back\" to celebrate the franchise's 20th anniversary"])
        mieville = story(["China Miéville has won the 2026 Goldsmiths Prize", "Goldsmiths Prize goes to China Miéville",
                          "China Miéville wins Goldsmiths Prize with longest novel in its history"])
        app.story_terms([suda, mieville])
        self.assertEqual(suda["keywords"][0]["term"], "Suda51")
        self.assertIn("No More Heroes", [k["term"] for k in suda["keywords"]])
        self.assertEqual(mieville["keywords"][0]["term"], "China Miéville")

    def test_recurring_names_are_not_demoted(self):
        def story(titles, score=12):
            return {"title": titles[0], "score": score, "sources": [{"title": t} for t in titles]}
        stewart = story(["Patrick Stewart, ‘Star Trek’ Captain and Professor X on ‘X-Men,’ Dies at 86",
                         "Patrick Stewart, Shakespearean actor turned 'Star Trek' captain and 'X-Men' star, dies at 86",
                         "Star Trek actor Patrick Stewart dead at 86",
                         "Sir Patrick Stewart, Beloved For Star Trek, X-Men And More, Dies Aged 86",
                         "A look back at Patrick Stewart's extraordinary life"], 23)
        tributes = [story(["Jonathan Frakes pays tribute to Patrick Stewart", "Patrick Stewart tributes from Jonathan Frakes"]),
                    story(["Hugh Jackman remembers Patrick Stewart", "Patrick Stewart remembered by Hugh Jackman"]),
                    story(["Patrick Stewart's best roles, ranked", "The best Patrick Stewart performances"])]
        app.story_terms([stewart] + tributes)
        terms = [k["term"] for k in stewart["keywords"]]
        self.assertEqual(terms[0], "Patrick Stewart")
        self.assertIn("Star Trek", terms)                 # quoted title in Title Case headlines
        self.assertNotIn("Trek", terms)
        self.assertEqual(app._name_candidates("Sir Patrick Stewart, Beloved For Star Trek, X-Men And More, Dies Aged 86"), {})

    def test_description_mentions_break_ties(self):
        def story(titles):
            return {"title": titles[0], "score": 10, "sources": [{"title": t} for t in titles]}
        a = story(["Valve and Nintendo team up", "Nintendo and Valve team up"])
        app.story_terms([a], bodies=[["Valve's hardware chief explained the deal.", "Valve said more soon."]])
        self.assertEqual(a["keywords"][0]["term"], "Valve")

    def test_names_with_connectors_are_kept_whole(self):
        def story(title_list):
            return {"title": title_list[0], "score": 10, "sources": [{"title": t} for t in title_list]}
        laufey = story(["God of War Laufey delayed to May 20, 2027", "God Of War Laufey Delayed To May",
                        "God of War Laufey has officially been delayed", "God Of War Laufey Delayed"])
        midnight = story(["South of Midnight director says he wouldn't have funded it",
                          "South Of Midnight Director Says He Would Not Have Funded The Game",
                          "South of Midnight creative director would have turned off Xbox money tap"])
        cod = story(["Call of Duty: Modern Warfare 4 DMZ patch nerfs UAV towers",
                     "Review: Call Of Duty: Modern Warfare 4", "Call of Duty: Modern Warfare 4 DMZ Season 1 trailer"])
        app.story_terms([laufey, midnight, cod])
        terms = lambda s: [k["term"] for k in s["keywords"]]
        self.assertIn("God of War Laufey", terms(laufey))
        self.assertNotIn("War Laufey", terms(laufey))
        self.assertIn("South of Midnight", terms(midnight))
        self.assertFalse(any(t in terms(midnight) for t in ("South", "Midnight", "Midnight director")))
        self.assertIn("Call of Duty", terms(cod))
        self.assertIn("Modern Warfare 4", terms(cod))
        self.assertEqual(sorted(app._name_candidates("New Xbox Series X Revealed").values(), key=len)[-1], "Xbox Series X")
        quoted = story(["South of Midnight director says he wouldn’t have funded it ‘Rome burns’",
                        "‘There’s always a night when Rome burns’: South of Midnight director speaks"])
        app.story_terms([quoted])                                    # curly quotes and contractions: no crash
        self.assertIn("South of Midnight", [k["term"] for k in quoted["keywords"]])


class ParserAndPagingTests(unittest.TestCase):
    def test_parser_details(self):
        future = time.time() + 30 * 86400
        from email.utils import formatdate
        rss = f"""  \n<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
        <item><title>Why 3 &lt; 5 and 7 &gt; 2 matters</title><link>/article/1?a=1&amp;region=us</link>
        <guid>g&amp;sect=9</guid><pubDate>{formatdate(future, usegmt=True)}</pubDate>
        <description>&lt;p&gt;Short summary.&lt;/p&gt; The post Why appeared first on Site.</description></item>
        </channel></rss>""".encode()
        e = feeds.parse(rss, base_url="https://site.example/feed/").entries[0]
        self.assertEqual(e.title, "Why 3 < 5 and 7 > 2 matters")
        self.assertEqual(e.link, "https://site.example/article/1?a=1&region=us")
        self.assertEqual(e.guid, "g&sect=9")
        self.assertLessEqual(e.published, time.time())
        self.assertEqual(e.description, "Short summary.")

    def test_placeholder_descriptions_dropped(self):
        self.assertEqual(feeds.clean_description("null"), "")
        self.assertEqual(feeds.clean_description(" None "), "")
        self.assertEqual(feeds.clean_description("Nullified contract"), "Nullified contract")
        self.assertEqual(feeds.clean_description("Big news here. submitted by /u/someone [link] [comments]"), "Big news here.")
        db = Path(tempfile.mkdtemp()) / "n.db"
        st = app.Store(db)
        fid = st.add_feed("https://a.example/rss")
        with st._conn() as c:
            c.execute("INSERT INTO items (feed_id, guid, title, published, fetched_at, description) "
                      "VALUES (?, 'g', 'T', ?, ?, 'null')", (fid, time.time(), time.time()))
        self.assertEqual(app.Store(db).items_since(0)[0].description, "")   # cleaned on open

    def test_google_news_suffix_removed(self):
        rss = b"""<rss><channel><item><title>Democrats win the Senate - Reuters</title><link>https://x/1</link></item>
                  </channel></rss>"""
        self.assertEqual(feeds.parse(rss, base_url="https://news.google.com/rss/search?q=x").entries[0].title,
                         "Democrats win the Senate")
        self.assertEqual(feeds.parse(rss, base_url="https://other.example/rss").entries[0].title,
                         "Democrats win the Senate - Reuters")

    def test_feedly_paging_handles_ties_and_late_published(self):
        import feedly
        # 120 entries; crawl times tie in groups of 7; some have published > crawled.
        entries = [{"id": str(i), "crawled": 1000 + (i // 7), "published": 1000 + (i // 7) + (500 if i % 11 == 0 else 0)}
                   for i in range(120)]

        def fake_get(path, params, **kw):
            page = sorted((e for e in entries if e["crawled"] > params["newerThan"]), key=lambda e: e["crawled"])
            return {"items": page[:50]}
        with mock.patch.object(feedly, "_get", fake_get):
            got = list(feedly.history("https://a.example/rss", since=0))
        self.assertEqual(sorted(int(e["id"]) for e in got), list(range(120)))


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_v1_database_is_migrated(self):
        db = self.tmp / "old.db"
        import contextlib
        with contextlib.closing(sqlite3.connect(db)) as c, c:  # schema from the first release, without feeds.via
            c.executescript("""CREATE TABLE feeds (id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '', added_at REAL NOT NULL, last_fetched REAL, last_error TEXT,
                item_count INTEGER NOT NULL DEFAULT 0);""")
            c.execute("INSERT INTO feeds (url, added_at) VALUES ('https://a.example/rss', 0)")
        store = app.Store(db)
        self.assertEqual(store.list_feeds()[0]["via"], None)
        self.assertEqual(store.list_feeds()[0]["enabled"], 1)  # v5 column added with default "on"
        store.save_fetch(1, feeds.parse(RSS), None, "direct")
        self.assertEqual(store.list_feeds()[0]["via"], "direct")

    def test_feedly_fallback_when_direct_fetch_fails(self):
        entry = {"id": "x", "originId": "g1", "title": "Hello &amp; welcome", "published": 1762250400000,
                 "alternate": [{"href": "https://a.example/1"}]}
        with mock.patch.object(feeds, "fetch", side_effect=feeds.FeedError("blocked")), \
             mock.patch("feedly.recent", return_value=("Feed A", [entry])):
            parsed, via, note = app.fetch_with_fallback("https://a.example/rss")
        self.assertEqual((via, parsed.title, parsed.entries[0].title), ("feedly", "Feed A", "Hello & welcome"))
        self.assertIn("blocked", note)

    def test_verticals_get_separate_seeded_databases(self):
        stores = app.open_stores(self.tmp)
        self.assertEqual(set(stores), {v.key for v in app.VERTICALS})
        for v in app.VERTICALS:
            self.assertTrue((self.tmp / f"{v.key}.db").exists())
        self.assertGreater(len(stores["games"].list_feeds()), 40)
        names = {f["url"]: f["name"] for f in stores["games"].list_feeds()}
        self.assertEqual(names["http://feeds.ign.com/ign/all"], "IGN")


if __name__ == "__main__":
    unittest.main()
