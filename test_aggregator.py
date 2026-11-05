"""Tests: python3 -m unittest -v"""

import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
