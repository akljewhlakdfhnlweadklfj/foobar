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
import groups
import keyterms
import sources
import matching
from matching import Item

NOW = time.time()


def items(rows):
    return [Item(id=i, feed_id=f, title=t, published=NOW) for i, (f, t) in enumerate(rows)]


class MatchingTests(unittest.TestCase):
    def cluster_titles(self, rows, **kw):
        stories = matching.cluster(items(rows), **kw)
        return sorted((sorted(i.title for i in s.items) for s in stories), key=len, reverse=True)

    def test_stories_span_at_most_24_hours(self):
        H = 3600
        rows = [(1, "Nintendo Direct announced for Thursday", NOW), (2, "Nintendo Direct announced for Thursday morning", NOW + 2 * H),
                (3, "Nintendo Direct announced for Thursday", NOW + 20 * H),
                (1, "Nintendo Direct announced for Thursday", NOW + 30 * 24 * H),     # next month's Direct
                (2, "Nintendo Direct announced for Thursday", NOW + 30 * 24 * H + H)]
        rows += [(9, t, NOW) for t in ("Valve raises Steam Deck price", "Sony confirms PS6 dev kits", "Ubisoft sells Arkane",
                                       "Xbox Game Pass adds day one titles", "Capcom hires new chief", "EA layoffs hit studio")]
        its = [Item(id=i, feed_id=f, title=t, published=p) for i, (f, t, p) in enumerate(rows)]
        spans = sorted(len(s.items) for s in matching.cluster(its) if "Direct" in s.items[0].title)
        self.assertEqual(spans, [2, 3])
        # A chain of headlines 15 hours apart can't stretch one story past 24 hours.
        chain = [Item(id=i, feed_id=i, title="Steam changes game review process", published=NOW + i * 15 * H) for i in range(4)]
        chain += [Item(id=10 + i, feed_id=9, title=t, published=NOW) for i, t in enumerate(
            ("Sony confirms PS6 dev kits", "Ubisoft sells Arkane", "Capcom hires new chief", "EA layoffs hit studio"))]
        steam = lambda **kw: [st for st in matching.cluster(chain, **kw) if "Steam" in st.items[0].title]
        self.assertGreater(len(steam()), 1)
        for st in steam():
            self.assertLessEqual(max(x.published for x in st.items) - min(x.published for x in st.items), 24 * H)
        self.assertEqual(len(steam(max_span_hours=None)), 1)

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
        self.assertEqual(matching.pick_headline(story.items).id, 4)                 # shortest headline

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
        store.update_feed(fa["id"], weight=2)
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
            self.assertEqual(patch("/1", {"required": True})[0], 400)          # "required" is per group now
            self.assertEqual(patch("", {"enabled": False})[0], 404)              # no bulk change


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
                sources.fetch("https://a.example/rss")
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


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = app.Store(self.tmp / "games.db")
        now = time.time()
        for n in (1, 2, 3):
            fid = self.store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", n)
            self.store.save_fetch(fid, feeds.ParsedFeed(f"Outlet {n}", [
                feeds.ParsedEntry(f"g{n}", f"Valve announces Steam Machine hardware {n}", f"Desc {n}", now - 3600),
                feeds.ParsedEntry(f"old{n}", f"Nintendo Direct dated for Thursday {n}", "", now - 10 * 86400),
                feeds.ParsedEntry(f"solo{n}", ["Capcom hires new chief", "Teacher strike enters week", "Volcano erupts near Iceland town"][n - 1], "", now)]), None)
        self.store.update_feed(3, enabled=False)
        self.digests = app.make_digests({"games": self.store}, self.tmp / "digests")

    def test_digest_has_every_outlet_and_multi_outlet_stories_only(self):
        d = self.digests.data("games", 48)[1]
        self.assertEqual(d["window_hours"], 48)
        self.assertNotIn("outlets", d)                               # outlet details come from the catalog
        self.assertEqual(len(d["stories"]), 1)                       # the old story is outside 48h; solos dropped
        st = d["stories"][0]
        self.assertEqual(sorted(x["f"] for x in st["s"]), [1, 2, 3])   # switched-off outlets included: viewers choose
        self.assertTrue(st["k"] and st["k"][0]["t"])
        d30 = self.digests.data("games", 300)[1]     # 300h -> 30-day window
        self.assertEqual((d30["window_hours"], len(d30["stories"])), (720, 2))

    def test_written_to_disk_and_reloaded_with_same_etag(self):
        e = self.digests.get("games", 24)
        self.assertTrue((self.tmp / "digests" / "games-48.json.gz").exists())
        again = app.make_digests({"games": self.store}, self.tmp / "digests")
        self.assertEqual(again.get("games", 24)["etag"], e["etag"])

    def test_http_etag_gzip_and_rebuild_after_catalog_change(self):
        import gzip, threading, urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler({"games": self.store}, app.Scraper({}, auto=False),
                                                                      digests=self.digests))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        get = lambda h: urllib.request.urlopen(urllib.request.Request(base + "/api/games/digest?hours=48", headers=h), timeout=10)
        with mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            with get({"Accept-Encoding": "gzip"}) as r:
                etag = r.headers["ETag"]
                self.assertEqual(r.headers["Content-Encoding"], "gzip")
                self.assertIn("max-age", r.headers["Cache-Control"])
                self.assertEqual(len(json.loads(gzip.decompress(r.read()))["stories"]), 1)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                get({"If-None-Match": etag})
            self.assertEqual(cm.exception.code, 304)
            with urllib.request.urlopen(base + "/api/verticals", timeout=10) as r:
                self.assertEqual(json.load(r)["retention_days"], 30)
            patch = lambda body: urllib.request.urlopen(urllib.request.Request(
                base + "/api/games/feeds/1", data=json.dumps(body).encode(), method="PATCH",
                headers={"Content-Type": "application/json"}), timeout=10).close()
            with mock.patch.object(self.digests, "request_rebuild") as rebuild:
                patch({"weight": 6})                  # a default weight doesn't change grouping: no rebuild
                rebuild.assert_not_called()
                patch({"only": "Nintendo"})           # a keyword filter does (outlet 1 drops its Valve item)
            rebuild.assert_called_once_with(["games"])
        self.digests.build_now("games", (48,))
        self.assertNotEqual(self.digests.get("games", 48)["etag"], etag)


class GroupTests(unittest.TestCase):
    """Filter groups: validation, views (server-side filters), API."""

    def setUp(self):
        import groups
        self.groups = groups
        self.tmp = Path(tempfile.mkdtemp())
        self.store = app.Store(self.tmp / "games.db")
        now = time.time()
        # Story A: outlets 1-4 (weights 1..4); story B: outlets 1-2 only.
        for n in (1, 2, 3, 4):
            fid = self.store.add_feed(f"https://{n}.example/rss", f"Outlet {n}", n)
            entries = [feeds.ParsedEntry(f"a{n}", f"Valve announces Steam Machine hardware price {n}", f"Desc {n}", now - 5400)]
            if n <= 2:
                entries.append(feeds.ParsedEntry(f"b{n}", f"Nintendo Direct dated for Thursday morning {n}", "", now - 1800))
            self.store.save_fetch(fid, feeds.ParsedFeed(f"Outlet {n}", entries), None)
        self.stores = {"games": self.store}
        self.digests = app.make_digests(self.stores)
        self.gs = groups.GroupStore(self.tmp / "groups.db")
        self.views = groups.Views(self.gs, self.digests, self.stores)

    def view(self, **cfg):
        body = {"name": "G", "verticals": ["games"], **cfg}
        group = self.groups.clean(body, None, ["games"])
        return self.groups.build_view(group, lambda v, h: self.digests.data(v, h)[1], self.views._catalog)

    def shown(self, view):
        return sorted(len(s["s"]) for s in view["stories"] if "x" not in s)

    def test_clean_validates(self):
        c = self.groups.clean
        g = c({"name": " Big  stories ", "verticals": ["games"], "settings": {"min_feeds": 3}}, None, ["games"])
        self.assertEqual((g["name"], g["settings"]["min_feeds"], g["settings"]["hours"]), ("Big stories", 3, 48.0))
        for bad in ({"name": ""}, {"verticals": []}, {"verticals": ["nope"]}, {"settings": {"min_feeds": 0}},
                    {"settings": {"hours": 1000}}, {"settings": {"min_feeds_mode": "share"}}, {"ai_summary": "yes"},
                    {"outlets": {"games": {"x": {"on": False}}}}, {"outlets": {"games": {"1": {"w": 11}}}}, {"color": "red"}):
            with self.assertRaises(ValueError, msg=bad):
                c({"name": "G", "verticals": ["games"], **bad}, None, ["games"])
        g = c({"outlets": {"games": {"1": {"on": False, "w": 3}}}}, g, ["games"])
        self.assertEqual(g["outlets"], {"games": {"1": {"on": False, "w": 3.0}}})
        g = c({"outlets": {"games": {"1": {"on": None}}}}, g, ["games"])
        self.assertEqual(g["outlets"], {"games": {"1": {"w": 3.0}}})
        self.assertEqual(c({"outlets": {"games": {"1": None}}}, g, ["games"])["outlets"], {})

    def test_view_applies_filters_on_the_server(self):
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2})), [2, 4])
        v = self.view(settings={"min_feeds": 3})
        self.assertEqual(self.shown(v), [4])
        self.assertEqual([len(s["s"]) for s in v["stories"] if "x" in s], [2])      # kept for watched phrases
        # Weight floor: outlets 1-2 don't count; percent of the outlets that can count (2 of 4).
        v = self.view(settings={"min_weight": 3, "min_feeds_mode": "percent", "min_feeds_percent": 100})
        self.assertEqual((v["min_outlets"], v["countable"]), ({"games": 2}, {"games": 2}))
        self.assertEqual(self.shown(v), [4])
        a = next(s for s in v["stories"] if "x" not in s)
        self.assertEqual((a["cov"], a["sc"]), (2, 7))
        # Outlet overrides: switch 3 and 4 off -> story A has 2 outlets; weight override changes the score.
        v = self.view(settings={"min_feeds": 2}, outlets={"games": {"3": {"on": False}, "4": {"on": False}, "1": {"w": 9}}})
        self.assertEqual(self.shown(v), [2, 2])
        self.assertEqual(sorted(s["sc"] for s in v["stories"]), [11, 11])
        # Required outlet 3: only story A. Required 1 and 3: any -> both; all -> only A (B lacks 3).
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2}, outlets={"games": {"3": {"req": True}}})), [4])
        both = {"games": {"1": {"req": True}, "3": {"req": True}}}
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2}, outlets=both)), [2, 4])
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2, "require_all": True}, outlets=both)), [4])
        # Catalog default off (outlets 3 and 4): groups without their own setting leave them out...
        self.store.update_feed(3, enabled=False); self.store.update_feed(4, enabled=False)
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2})), [2, 2])
        # ...and a group can switch one back on.
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2}, outlets={"games": {"3": {"on": True}}})), [2, 3])
        self.store.update_feed(3, enabled=True); self.store.update_feed(4, enabled=True)
        # Minimum score: A scores 10, B scores 3.
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2, "min_score": 5})), [4])
        # Time window: B is 30 min old, A 90 minutes.
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2, "hours": 1})), [2])
        self.assertEqual(self.shown(self.view(settings={"min_feeds": 2, "hours": 2})), [2, 4])
        s = self.view(settings={"min_feeds": 2})["stories"][0]
        self.assertEqual(len(s["h"]), len(s["s"]))
        self.assertTrue(all(len(k) == 10 for k in s["h"]))

    def test_new_vertical_gets_one_group_once(self):
        import sqlite3 as _sql
        db = self.tmp / "old_groups.db"                         # a groups.db from before seeding was tracked
        c = _sql.connect(db)
        c.execute("CREATE TABLE groups (id INTEGER PRIMARY KEY, config TEXT NOT NULL, updated_at REAL NOT NULL)")
        c.execute("INSERT INTO groups (config, updated_at) VALUES (?, 0)",
                  (json.dumps(self.groups.clean({"name": "Mine", "verticals": ["games"], "position": 0}, None, ["games"])),))
        c.commit(); c.close()
        gs = self.groups.GroupStore(db)
        stores = {"games": self.store, "tech": app.Store(self.tmp / "tech.db")}
        self.assertEqual(app.seed_groups(gs, stores), 1)                       # only the new vertical
        self.assertEqual([(g["name"], g["verticals"]) for g in gs.list()], [("Mine", ["games"]), ("Consumer Tech", ["tech"])])
        self.assertEqual(app.seed_groups(gs, stores), 0)
        gs.delete(gs.list()[1]["id"])                                          # deleting it doesn't bring it back
        self.assertEqual(app.seed_groups(gs, stores), 0)

    def test_seed_and_api(self):
        self.assertEqual(app.seed_groups(self.gs, self.stores), 1)
        self.assertEqual(app.seed_groups(self.gs, self.stores), 0)
        g = self.gs.list()[0]
        self.assertEqual((g["name"], g["verticals"], g["settings"]["min_feeds"]), ("Games", ["games"], 4))
        import threading, urllib.request
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.stores, app.Scraper({}, auto=False),
                                                                      digests=self.digests, group_store=self.gs))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(method, path, body=None, headers=None):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read() or b"{}"), r.headers
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read() or b"{}"), e.headers

        with mock.patch.dict("os.environ", {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            code, d, _ = call("POST", "/api/groups", {"name": "Two+", "verticals": ["games"], "settings": {"min_feeds": 2}})
            self.assertEqual(code, 201)
            gid = d["group"]["id"]
            self.assertEqual(d["group"]["position"], 1)
            code, v, h = call("GET", f"/api/groups/{gid}/stories")
            self.assertEqual((code, len([s for s in v["stories"] if "x" not in s])), (200, 2))
            self.assertEqual(call("GET", f"/api/groups/{gid}/stories", headers={"If-None-Match": h["ETag"]})[0], 304)
            code, d, _ = call("PATCH", f"/api/groups/{gid}", {"settings": {"min_feeds": 3}, "outlets": {"games": {"4": {"on": False}}}})
            self.assertEqual((code, d["group"]["settings"]["min_feeds"]), (200, 3))
            v = call("GET", f"/api/groups/{gid}/stories")[1]
            self.assertEqual([len(s["s"]) for s in v["stories"] if "x" not in s], [3])
            self.assertEqual(call("PATCH", f"/api/groups/{gid}", {"settings": {"min_feeds": "3"}})[0], 400)
            self.assertEqual(call("PATCH", "/api/groups/999", {"name": "x"})[0], 404)
            self.assertEqual(len(call("GET", "/api/groups")[1]["groups"]), 2)
            self.assertEqual(call("DELETE", f"/api/groups/{gid}")[0], 200)
            self.assertEqual(call("DELETE", f"/api/groups/{g['id']}")[0], 400)    # keep at least one
            self.assertEqual(call("GET", f"/api/groups/{gid}/stories")[0], 404)


class SummarizerTests(unittest.TestCase):
    """AI headlines with a fake Claude client: headline = first title + " today"; a story whose titles
    contain REFUSE makes the whole request refused; a 400 can be raised for the fallback form."""

    def fake(self, bad_request=None, shuffle=False):
        import re as _re
        import types
        calls = []

        def create(**kw):
            calls.append(kw)
            if bad_request and "fallbacks" in kw:
                raise type("BadRequestError", (Exception,), {})(bad_request)
            text = kw["messages"][0]["content"]
            if "REFUSE" in text:
                return types.SimpleNamespace(stop_reason="refusal", content=[])
            stories = _re.findall(r"<story n=(\d+)>\n- ([^\n]*)", text)
            out = [{"n": int(n) if not shuffle else len(stories) + 1 - int(n), "headline": f"  {t}   today "} for n, t in stories]
            return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=json.dumps({"headlines": out}))])
        msgs = types.SimpleNamespace(create=create)
        return types.SimpleNamespace(messages=msgs, beta=types.SimpleNamespace(messages=msgs)), calls

    def test_writes_caches_and_matches_by_most_articles(self):
        import ai
        client, calls = self.fake()
        path = Path(tempfile.mkdtemp()) / "ai.db"
        s = ai.Summarizer(path, model="m", client=client)
        self.assertIsNone(s.lookup(["a", "b"]))
        s.want(["a", "b"], ["Valve announces Steam Machine", "Steam Machine revealed"], 2, 200.0)
        s.want(["c", "d", "e"], ["Nintendo Direct dated", "Direct on Thursday"], 3, 100.0)
        self.assertFalse(s.run_once())
        self.assertEqual(s.lookup(["a", "b"]), "Valve announces Steam Machine today")
        self.assertEqual(s.lookup(["a", "b", "z"]), "Valve announces Steam Machine today")   # an outlet joined
        self.assertIsNone(s.lookup(["a", "x", "y"]))                  # shares one article: a different story
        self.assertIsNone(s.lookup(["a", "b", "x", "y", "w"]))        # mostly new articles: write again
        self.assertIsNone(s.lookup(["c", "q"]))                       # only 1 of the 3 it was written from
        self.assertEqual(calls[0]["fallbacks"], "default")
        self.assertEqual(calls[0]["output_config"]["effort"], "low")
        self.assertIn("- Steam Machine revealed", calls[0]["messages"][0]["content"])
        again = ai.Summarizer(path, client=client)                   # kept on disk, as is today's count
        self.assertEqual(again.lookup(["c", "d"]), "Nintendo Direct dated today")
        self.assertEqual(again.status()["written_today"], 2)

    def test_a_declined_story_doesnt_block_its_batch(self):
        import ai
        client, calls = self.fake()
        s = ai.Summarizer(None, client=client)
        for i in range(5):
            s.want([f"k{i}"], [f"Story number {i} REFUSE" if i == 3 else f"Story number {i}"], 2, float(i))
        s.run_once()
        self.assertEqual([s.lookup([f"k{i}"]) is not None for i in range(5)], [True, True, True, False, True])
        n_calls = len(calls)
        s.want(["k3"], ["Story number 3 REFUSE"], 2, 3.0)            # not asked again for a while
        self.assertFalse(s.run_once())
        self.assertEqual(len(calls), n_calls)

    def test_bad_replies_cap_and_fallback_handling(self):
        import ai
        client, _ = self.fake(shuffle=True)                         # headlines numbered for the wrong stories
        s = ai.Summarizer(None, client=client)
        s.want(["a"], ["Valve announces Steam Machine"], 2, 2.0)
        s.want(["b"], ["Nintendo Direct dated"], 2, 1.0)
        s.run_once()
        self.assertEqual((s.lookup(["a"]), s.lookup(["b"])), (None, None))
        client, calls = self.fake(bad_request="Unknown parameter: fallbacks")
        s = ai.Summarizer(None, client=client)
        s.want(["a"], ["Valve announces Steam Machine"], 2, 0); s.run_once()
        self.assertEqual(s.lookup(["a"]), "Valve announces Steam Machine today")
        self.assertEqual(["fallbacks" in c for c in calls], [True, False])
        client, calls = self.fake(bad_request="Your credit balance is too low")
        s = ai.Summarizer(None, client=client)
        s.want(["a"], ["Valve announces Steam Machine"], 2, 0); s.run_once()
        self.assertTrue(s._use_fallbacks)                            # an unrelated 400 doesn't turn them off
        self.assertIn("credit", s.status()["reason"])
        client, _ = self.fake()
        with mock.patch.object(ai, "MAX_PER_DAY", 3):
            s = ai.Summarizer(None, client=client)
            for i in range(5):
                s.want([f"k{i}"], [f"Story number {i}"], 2, float(i))
            s.run_once(); s.run_once()
            self.assertEqual(sum(s.lookup([f"k{i}"]) is not None for i in range(5)), 3)
            self.assertIn("daily limit", s.status()["reason"])
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}), \
             mock.patch.object(ai.Path, "home", lambda: Path(tempfile.mkdtemp())):
            st = ai.Summarizer(None).status()
        self.assertFalse(st["available"])
        self.assertTrue(st["reason"])
        self.assertFalse(ai.Summarizer(None, enabled=False).available)


class YouTubeTests(unittest.TestCase):
    FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Some Channel</title>
 <entry>
  <id>yt:video:abcdefghijk</id><yt:videoId>abcdefghijk</yt:videoId><yt:channelId>UCBJycsmduvYEL83R_U4JriQ</yt:channelId>
  <title>Nintendo Switch 3 Revealed</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abcdefghijk"/>
  <author><name>Some Channel</name></author>
  <published>2026-11-04T10:00:00+00:00</published>
  <media:group>
   <media:title>Nintendo Switch 3 Revealed</media:title>
   <media:content url="https://www.youtube.com/v/abcdefghijk?version=3" type="application/x-shockwave-flash" width="640" height="390"/>
   <media:thumbnail url="https://i1.ytimg.com/vi/abcdefghijk/hqdefault.jpg" width="480" height="360"/>
   <media:description>Nintendo just showed the Switch 3.
Here is everything.</media:description>
   <media:community><media:starRating count="5321" average="5.00" min="1" max="5"/><media:statistics views="123456"/></media:community>
  </media:group>
 </entry>
</feed>"""

    def test_native_feed_gives_description_thumbnail_and_views(self):
        e = feeds.parse(self.FEED, base_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCBJycsmduvYEL83R_U4JriQ").entries[0]
        self.assertEqual((e.title, e.views, e.guid), ("Nintendo Switch 3 Revealed", 123456, "yt:video:abcdefghijk"))
        self.assertEqual(e.description, "Nintendo just showed the Switch 3. Here is everything.")
        self.assertEqual(e.image, "https://i1.ytimg.com/vi/abcdefghijk/hqdefault.jpg")
        self.assertEqual(feeds.clean_text("thoughts.<br><br>Merch: <b>x</b> 3 < 5"), "thoughts. Merch: x 3 < 5")

    def test_urls_counts_and_channel_page(self):
        import youtube
        cid = "UCBJycsmduvYEL83R_U4JriQ"
        feed = youtube.FEED_URL.format(cid)
        self.assertEqual(youtube.feed_url_for(f"https://www.youtube.com/channel/{cid}"), feed)
        self.assertEqual(youtube.feed_url_for(feed), feed)
        self.assertIsNone(youtube.feed_url_for("https://example.com/rss"))
        page = ('<script>var ytInitialData = {"metadata": {"channelMetadataRenderer": {"title": "MKBHD"}}, "x": [{"lockupViewModel": '
                '{"contentId": "g6Die8gVFn0", "metadata": {"lockupMetadataViewModel": {"title": {"content": "OLED iPad Mini"}, '
                '"metadata": {"contentMetadataViewModel": {"metadataRows": [{"metadataParts": [{"text": {"content": "8.1M"}}, '
                '{"text": {"content": "1d ago"}}]}]}}}}}}]};</script>')
        with mock.patch.object(feeds, "fetch", lambda url, **kw: page.encode()):
            name, vids = youtube.channel_videos(cid)
        self.assertEqual((name, vids[0].title, vids[0].views, vids[0].guid), ("MKBHD", "OLED iPad Mini", 8100000, "yt:video:g6Die8gVFn0"))
        self.assertAlmostEqual(vids[0].published, time.time() - 86400, delta=60)
        self.assertEqual([youtube.parse_count(t) for t in ("8.1M", "12K views", "935", "1.2B", "")], [8100000, 12000, 935, 1200000000, None])

    def test_fallback_chain_feedly_plus_page_views(self):
        import youtube
        cid = "UCBJycsmduvYEL83R_U4JriQ"
        url = youtube.FEED_URL.format(cid)
        feedly_entry = {"originId": "yt:video:g6Die8gVFn0", "title": "OLED iPad Mini", "published": time.time() * 1000,
                        "alternate": [{"href": "https://www.youtube.com/watch?v=g6Die8gVFn0"}], "content": {"content": "A refresh.<br>More"}}
        page_vids = [feeds.ParsedEntry("yt:video:g6Die8gVFn0", "OLED iPad Mini", "https://www.youtube.com/watch?v=g6Die8gVFn0", time.time(), "", "", 8100000),
                     feeds.ParsedEntry("yt:video:zzzzzzzzzzz", "Older video", "https://www.youtube.com/watch?v=zzzzzzzzzzz", time.time(), "", "", 500)]
        with mock.patch.object(feeds, "fetch_and_parse", side_effect=feeds.FeedError("Fetch failed: HTTP 404 Not Found")), \
             mock.patch("feedly.recent", lambda url, **kw: ("MKBHD", [feedly_entry])), \
             mock.patch.object(youtube, "channel_videos", lambda c: ("MKBHD", page_vids)):
            parsed, via, note = sources.fetch(url)                       # views never read: read the page
            self.assertEqual(via, "feedly")
            first = next(e for e in parsed.entries if e.guid.endswith("g6Die8gVFn0"))
            self.assertEqual((first.views, first.description), (8100000, "A refresh. More"))
            self.assertEqual(len(parsed.entries), 2)                    # the older video updates its count too
            self.assertIsNotNone(parsed.views_checked_at)
            parsed, via, _ = sources.fetch(url, views_checked_at=time.time() - 60)    # read a minute ago: skip the page
            self.assertEqual((len(parsed.entries), parsed.entries[0].views, parsed.views_checked_at), (1, None, None))
        with mock.patch.object(feeds, "fetch_and_parse", side_effect=feeds.FeedError("404")), \
             mock.patch("feedly.recent", lambda url, **kw: ("", [])), \
             mock.patch.object(youtube, "channel_videos", lambda c: ("MKBHD", page_vids)):
            parsed, via, _ = sources.fetch(url)
        self.assertEqual((via, len(parsed.entries)), ("page", 2))
        store = app.Store(Path(tempfile.mkdtemp()) / "y.db")             # the kind is stored, the time is kept
        fid = store.add_feed(url)
        store.save_fetch(fid, parsed, None, via, "note")
        f = store.get_feed(fid)
        self.assertEqual((f["kind"], f["via"], f["fetch_note"], f["last_error"]), ("youtube", "page", "note", None))
        self.assertIsNotNone(f["views_checked_at"])
        self.assertEqual({i.kind for i in store.items_since(0)}, {"youtube"})

    def test_youtube_link_in_an_rss_feed_is_not_a_video(self):
        import digest
        it = matching.Item(id=1, feed_id=1, title="Trailer", link="https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertNotIn("y", digest._source(it))
        it.kind = "youtube"
        self.assertEqual(digest._source(it)["y"], 1)

    def test_story_prefers_article_headline_and_shows_top_video(self):
        tmp = Path(tempfile.mkdtemp())
        store = app.Store(tmp / "games.db")
        now = time.time()
        rows = [("https://a.example/rss", "Outlet A", 4, "Nintendo reveals Switch 3 hardware and price", "https://a/1", None),
                ("https://b.example/rss", "Outlet B", 3, "Nintendo Switch 3 hardware price revealed today", "https://b/1", None),
                ("https://www.youtube.com/feeds/videos.xml?channel_id=UCaaaaaaaaaaaaaaaaaaaaaa", "Chan 1", 2, "Switch 3 hardware price!", "https://www.youtube.com/watch?v=aaaaaaaaaaa", 5000),
                ("https://www.youtube.com/feeds/videos.xml?channel_id=UCbbbbbbbbbbbbbbbbbbbbbb", "Chan 2", 2, "Nintendo Switch 3 hardware price reaction", "https://www.youtube.com/watch?v=bbbbbbbbbbb", 90000)]
        for n, (url, name, w, title, link, v) in enumerate(rows):
            fid = store.add_feed(url, name, w)
            store.save_fetch(fid, feeds.ParsedFeed(name, [feeds.ParsedEntry(f"g{n}", title, link, now - 600, "", "", v)]), None)
        stores = {"games": store}
        views = groups.Views(groups.GroupStore(tmp / "g.db"), app.make_digests(stores), stores)
        g = groups.clean({"name": "G", "verticals": ["games"], "settings": {"min_feeds": 2}}, None, ["games"])
        s = groups.build_view(g, lambda v, h: views.digests.data(v, h)[1], views._catalog)["stories"][0]
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["t"], "Nintendo reveals Switch 3 hardware and price")        # shortest *article* headline
        self.assertEqual((s["mv"]["o"], s["mv"]["v"]), ("Chan 2", 90000))               # most-viewed video
        self.assertEqual(sorted(x["y"] for x in s["s"]), [0, 0, 1, 1])


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
        self.assertEqual(self.call("POST", "/api/groups", raw=b'{"name": "x"}',
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
        self.assertEqual(self.call("POST", "/api/groups", raw=b"x" * (70 * 1024),
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
        def slow_fetch(url, **kw):
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

    def test_refresh_keeps_a_stopped_backfill_resumable(self):
        sc = self.make()
        backfilled = []
        def history(url, since=0, **kw):
            backfilled.append(url); time.sleep(0.2)
            return iter(())
        with mock.patch("feedly.history", history):
            sc.start("backfill", ["games"], 3)
            time.sleep(0.3)
            sc.stop(wait=True)
            bf = sc.status()["run"]
            self.assertEqual((bf["kind"], bf["status"]), ("backfill", "stopped"))
            sc.start("refresh", ["games"])                      # e.g. the scheduler
            self.wait_idle(sc)
            st = sc.status()
            self.assertEqual((st["run"]["id"], st["resumable"]), (bf["id"], True))
            n = len(backfilled)
            sc.start("backfill", ["games"], 3)                  # resumes it
            self.assertEqual(sc.status()["run"]["id"], bf["id"])
            self.wait_idle(sc)
            self.assertEqual(sc.status()["run"]["status"], "completed")
            self.assertEqual(len(set(backfilled)), 20)
            self.assertEqual(len(backfilled), 20)                # nothing loaded twice
            self.assertGreater(n, 0)

    def test_schedule_toggle_persists(self):
        sc = self.make()
        sc.set_schedule(auto=False, interval_minutes=30)
        sc2 = app.Scraper({"games": self.store}, self.tmp / "state.json", auto=True, interval_minutes=15)
        self.assertEqual((sc2.status()["auto"], sc2.status()["interval_minutes"]), (False, 30))


class SettingsApiTests(HttpTestCase):
    """Per vertical, only match strictness is left (everything else is per filter group)."""

    def test_threshold_only(self):
        store = app.Store(Path(tempfile.mkdtemp()) / "t.db")
        store.set_settings({"min_feeds": 7})                     # saved by an earlier version: ignored now
        self.start_server({"games": store})
        got = self.call("GET", "/api/games/settings")[1]
        self.assertEqual((got["threshold"], got["grouped_threshold"]), (matching.DEFAULT_THRESHOLD, matching.DEFAULT_THRESHOLD))
        self.assertEqual(self.call("PATCH", "/api/games/settings", {"threshold": 0.2})[1], {"threshold": 0.2, "rebuilding": True})
        for bad in ({"threshold": 2}, {"threshold": "0.2"}, {"min_feeds": 3}, {}):
            self.assertEqual(self.call("PATCH", "/api/games/settings", bad)[0], 400, bad)
        self.assertEqual(self.call("PATCH", "/api/games/settings", {"reset": True})[1]["threshold"], matching.DEFAULT_THRESHOLD)
        self.assertEqual(app.earlier_settings(app.VERTICALS_BY_KEY["games"], store)["min_feeds"], 4)   # reset cleared it
        self.assertEqual(self.call("GET", "/api/games/stories")[0], 404)
        self.assertEqual(self.call("GET", "/api/games/phrases")[0], 404)


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
    def test_shortest_unlabelled_headline_is_used(self):
        story = matching.Story(items=[
            Item(id=1, feed_id=1, title="Review: The Legend of Zelda: Ocarina of Time Remake - One of the Best", weight=5),
            Item(id=2, feed_id=2, title="Ocarina remake reviewed", weight=2),          # short headlines count too
            Item(id=3, feed_id=3, title="Zelda: Ocarina of Time remake review roundup", weight=3),
            Item(id=4, feed_id=4, title="Ocarina of Time remake: our review", weight=1, image="https://x/i.jpg"),
        ])
        self.assertEqual(matching.pick_headline(story.items).id, 2)
        self.assertEqual(matching.pick_headline([i for i in story.items if i.weight != 2]).id, 4)   # "Review: ..." is labelled
        self.assertEqual(matching.pick_headline([i for i in story.items if i.weight >= 3]).id, 3)
        self.assertEqual(matching.pick_headline([Item(id=9, feed_id=9, title="R.I.P. Patrick Stewart"),
                                                 Item(id=8, feed_id=8, title="Patrick Stewart dies at 86")]).id, 9)
        labelled = matching.Story(items=[Item(id=1, feed_id=1, title="Opinion | The end of aid to Israel?"),
                                         Item(id=2, feed_id=2, title="US will stop $4B in aid to Israel, Trump says")])
        self.assertEqual(matching.pick_headline(labelled.items).id, 2)
        # Per outlet too: an outlet's plain headline isn't dropped for its shorter "Opinion |" piece.
        import digest
        both = [Item(id=1, feed_id=1, title="Opinion | Aid ends?", published=1),
                Item(id=2, feed_id=1, title="US will stop $4B in aid to Israel, Trump says", published=2)]
        self.assertEqual([i.id for i in digest._per_outlet(both)], [2])

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
        stores = {"games": store}
        views = groups.Views(groups.GroupStore(Path(tempfile.mkdtemp()) / "g.db"), app.make_digests(stores), stores)
        g = groups.clean({"name": "G", "verticals": ["games"], "settings": {"min_feeds": 3}}, None, ["games"])
        s = groups.build_view(g, lambda v, h: views.digests.data(v, h)[1], views._catalog)["stories"][0]
        self.assertEqual(s["t"], "Valve announces Steam Machine hardware")             # shortest
        self.assertEqual((s["d"], s["i"]), ("Long text.", "https://i/1.jpg"))           # from another outlet


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
        keyterms.story_terms(stories)
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
        keyterms.story_terms([gov])
        self.assertNotIn(gov["keywords"][0]["term"].lower(), ("record", "female", "elected"))
        self.assertTrue(keyterms._noun_like(("ubisoft",), "Ubisoft", True))
        self.assertFalse(keyterms._noun_like(("black",), "Black", True))           # capitalised adjective
        self.assertFalse(keyterms._noun_like(("delay",), "delayed", False))
        self.assertTrue(keyterms._noun_like(("name", "god", "of", "war"), "God of War", False))
        self.assertEqual(sorted(keyterms._name_candidates("Dragon’s Dogma 2 Drops Surprise Collab").values(), key=len)[-1],
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
        keyterms.story_terms([suda, mieville])
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
        keyterms.story_terms([stewart] + tributes)
        terms = [k["term"] for k in stewart["keywords"]]
        self.assertEqual(terms[0], "Patrick Stewart")
        self.assertIn("Star Trek", terms)                 # quoted title in Title Case headlines
        self.assertNotIn("Trek", terms)
        self.assertEqual(keyterms._name_candidates("Sir Patrick Stewart, Beloved For Star Trek, X-Men And More, Dies Aged 86"), {})

    def test_description_mentions_break_ties(self):
        def story(titles):
            return {"title": titles[0], "score": 10, "sources": [{"title": t} for t in titles]}
        a = story(["Valve and Nintendo team up", "Nintendo and Valve team up"])
        keyterms.story_terms([a], bodies=[["Valve's hardware chief explained the deal.", "Valve said more soon."]])
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
        keyterms.story_terms([laufey, midnight, cod])
        terms = lambda s: [k["term"] for k in s["keywords"]]
        self.assertIn("God of War Laufey", terms(laufey))
        self.assertNotIn("War Laufey", terms(laufey))
        self.assertIn("South of Midnight", terms(midnight))
        self.assertFalse(any(t in terms(midnight) for t in ("South", "Midnight", "Midnight director")))
        self.assertIn("Call of Duty", terms(cod))
        self.assertIn("Modern Warfare 4", terms(cod))
        self.assertEqual(sorted(keyterms._name_candidates("New Xbox Series X Revealed").values(), key=len)[-1], "Xbox Series X")
        quoted = story(["South of Midnight director says he wouldn’t have funded it ‘Rome burns’",
                        "‘There’s always a night when Rome burns’: South of Midnight director speaks"])
        keyterms.story_terms([quoted])                                    # curly quotes and contractions: no crash
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

    def test_v11_drops_subreddit_feeds(self):
        db = Path(tempfile.mkdtemp()) / "r.db"
        st = app.Store(db)
        keep = st.add_feed("https://a.example/rss")
        gone = st.add_feed("https://www.reddit.com/r/Games/.rss")
        for fid in (keep, gone):
            st.save_fetch(fid, feeds.ParsedFeed("x", [feeds.ParsedEntry(f"g{fid}", "Some headline here", "", time.time())]), None)
        with st._conn() as c:
            c.execute("PRAGMA user_version = 10")
        st = app.Store(db)
        self.assertEqual([f["url"] for f in st.list_feeds()], ["https://a.example/rss"])
        self.assertEqual(len(st.items_since(0, include_disabled=True)), 1)
        st.add_feed("https://www.reddit.com/r/x/.rss")      # added by hand later: kept
        self.assertEqual(len(app.Store(db).list_feeds()), 2)

    def test_dns_rebinding_is_refused_at_connect(self):
        import socket as _socket
        answers = iter([[(2, 1, 6, "", ("93.184.216.34", 443))], [(2, 1, 6, "", ("127.0.0.1", 80))]])
        with mock.patch.object(feeds.urllib.request, "getproxies", lambda: {}), \
             mock.patch.object(_socket, "getaddrinfo", lambda *a, **k: next(answers)):
            with self.assertRaises(feeds.FeedError) as cm:
                feeds.fetch("http://rebind.example/rss")
        self.assertIn("private or local", str(cm.exception))

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
            parsed, via, note = sources.fetch("https://a.example/rss")
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
