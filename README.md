# Consensus News

An RSS/Atom aggregator that only shows stories reported by **at least N** of your feeds.

No dependencies: Python 3.10+ standard library only (SQLite, `http.server`, `xml.etree`).

```bash
python3 app.py --import feeds.txt     # first run: load the seed feeds
python3 app.py                        # afterwards
# open http://localhost:8000
```

Options: `--port 8000`, `--host 127.0.0.1`, `--db news.db`, `--interval 15` (minutes between refreshes).

## Using it

- **Seed feeds**: `feeds.txt` lists the publications Overworld (overworld.vg) aggregates. Import it with `--import`.
- **Add/remove feeds** in the left panel. A feed is fetched and validated before it's saved, so a bad URL is reported immediately. A red dot means the last fetch failed, and the error is shown under the feed.
- **Reported by at least N**: the core filter. Stories are sorted by how many feeds carry them, then by recency.
- **Time window**: only headlines published in this window are compared. Headlines are stored for 14 days.
- **Match strictness**: the similarity threshold for treating two headlines as the same story (default 0.40).
- Click a story's source list to see each outlet's original headline and link.

## How headlines are matched

Outlets word the same story differently, so exact matching doesn't work. `matching.py` does the following:

1. Tokenizes each title: lowercases it, strips punctuation, drops stopwords and headline filler ("says", "report", "watch"), applies light stemming, and maps synonyms (reveals/confirms/unveils → announce, hikes → raise, lays off → layoff…).
2. Weights tokens by **IDF** across the current window, so distinctive words ("Silksong", "Ubisoft") count much more than common ones ("Nintendo" when every feed mentions it).
3. Links two headlines **from different feeds** when their IDF-weighted Jaccard similarity is ≥ the threshold and they share at least 2 tokens.
4. Treats connected groups as stories. A story's coverage is the number of *distinct* feeds in its group.

This is a simple lexical approach. It handles reworded headlines well, but it will miss pairs that share almost no words ("Switch 2 costs $449" vs "Nintendo's new console priced at $449"). If that matters, the place to upgrade is the similarity function in `matching.cluster`, e.g. sentence embeddings.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/feeds` | list feeds + status |
| POST | `/api/feeds` `{"url": ...}` | add (validated) |
| DELETE | `/api/feeds/{id}` | remove feed and its items |
| POST | `/api/refresh` | trigger an immediate refresh |
| GET | `/api/stories?min_feeds=2&hours=48&threshold=0.4` | aggregated stories |

## Tests

```bash
python3 -m unittest -v
```
