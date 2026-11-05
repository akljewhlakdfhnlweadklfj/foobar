# Consensus News

An RSS/Atom aggregator that only shows stories reported by **at least N** of your feeds, split
into tabs: **Games**, **Movies & TV**, **Books** and **General News**.

No dependencies: Python 3.10+ standard library only.

```bash
python3 app.py --backfill 3     # first run: seed all verticals + load 3 days of history
python3 app.py                  # afterwards
# open http://localhost:8000
```

Options: `--port 8000`, `--host 127.0.0.1`, `--data data/`, `--interval 15` (minutes between
refreshes), `--backfill DAYS` (max 14), `--import FILE --vertical games`, `--no-serve`.

## Run in GitHub Codespaces

On the repo page, click **Code → Codespaces → Create codespace** on this branch. The dev container
seeds every vertical and backfills 3 days of history on first creation, starts the server on every
start, and opens the app in a browser tab (or use the **Ports** panel → port 8000). Server log:
`/tmp/news-aggregator.log`.

Codespaces stop after ~30 minutes idle and count against your free monthly hours. Data persists in
`data/` until the codespace is deleted.

## Verticals and data

| Vertical | Seed list | Database |
|---|---|---|
| Games | `feeds/games.txt` | `data/games.db` |
| Movies & TV | `feeds/movies.txt` | `data/movies.db` |
| Books | `feeds/books.txt` | `data/books.db` |
| General News | `feeds/news.txt` | `data/news.db` |

- **Separate databases**: each vertical is one self-contained SQLite file with the same schema.
  To migrate, copy the `.db` file and point `--data` at its folder. Older databases are upgraded
  in place automatically (schema version is stored in `PRAGMA user_version`).
- **Seed lists** are read when a vertical's database is empty. Format: `URL  # Display name | w=N | notes`.
  The display name (text before the first `|`) is applied to existing feeds on every start.
- **Games** sources are the outlets [Overworld](https://www.overworld.vg/) aggregates, extracted
  from its full story archive. The other verticals mix trade publications with section feeds
  of general publications (NYT, WSJ, Guardian, etc.).
- Adding a vertical: add a `Vertical(...)` entry to `VERTICALS` in `app.py` and a `feeds/<key>.txt`.

## Prestige weights

Each outlet has a prestige weight from 1 to 5, judged **within its vertical** (Variety is a 5 for
Movies & TV). The weights are editorial judgment calls assigned by Claude, not measured data.

| Weight | Meaning |
|---|---|
| 5 | The field's paper of record or top trade publication |
| 4 | Major, well-respected outlet |
| 3 | Established mid-tier outlet |
| 2 | Smaller, or heavy on aggregation / listicles |
| 1 | Small blog or fan site |

How they're used:
- **Ranking**: a story's *prestige score* is the sum of its distinct outlets' weights, and stories are
  sorted by it. The "reported by at least N" filter still counts outlets.
- **Optional filter**: "Minimum prestige score" hides stories below a score.
- **Lead headline**: each story shows the headline from its highest-weighted outlet.
- **Editing**: weights are `w=N` in the seed files. They can also be changed per feed in the UI
  (★ menu). A UI change is kept across restarts; seed-file weights only fill feeds with no weight yet.
  Feeds you add yourself start at 2.

**Games**

| Weight | Outlets |
|---|---|
| 5 | GamesIndustry.biz |
| 4 | Aftermath, Ars Technica, Digital Foundry, Eurogamer, Game Developer, Game File, Game Informer, GameSpot, Gematsu, IGN, Kotaku, PC Gamer, Polygon, Rock Paper Shotgun, The Game Business, The Verge (Gaming), Video Games Chronicle |
| 3 | GamesBeat, GamesRadar+, Massively Overpowered, Nintendo Life, PlayStation Blog, Push Square, Rogue, RPG Site, Shacknews, Siliconera, Time Extension, Xbox Wire |
| 2 | DualShockers, GamingOnLinux, GoNintendo, Insider Gaming, Jank, MMORPG.com, My Nintendo News, Nintendo Everything, Nintendo World Report, Noisy Pixel, Pocket Tactics, Pure Xbox, RPGamer, TechRaptor |
| 1 | COGConnected, Console Creatures, covergeek, Crossplay, GameGrin, Hey Poor Player, Loot Level Chill, MonsterVine, PixelByte, Retro Handhelds, So Many Games, The Wand Report, Vooks |

**Movies & TV**

| Weight | Outlets |
|---|---|
| 5 | Deadline, New York Times (Movies), New York Times (Television), The Hollywood Reporter, Variety |
| 4 | BBC News (Entertainment & Arts), IndieWire, Los Angeles Times (Entertainment & Arts), NPR (Movies), Screen Daily (Latest), The Guardian (Film), TheWrap, Vulture |
| 3 | /Film, ABC News (Entertainment), Boxoffice Pro, CBC (Arts), CBS News (Entertainment), Empire, Rolling Stone (TV & Movies), The A.V. Club, The Independent (Film), The Playlist, TVLine |
| 2 | Collider, ComingSoon.net, Fox News (Entertainment), Gold Derby, JoBlo, World of Reel |

**Books**

| Weight | Outlets |
|---|---|
| 5 | New York Times (Books), Publishers Weekly (All recent), The Bookseller (News) |
| 4 | Financial Times (Books), Literary Hub, NPR (Books), Publishers Lunch, The Atlantic (Books), The Guardian (Books) |
| 3 | Christian Science Monitor (Books), CrimeReads, Electric Literature, Locus, Los Angeles Times (Books), PEN America, Publishing Perspectives, Reactor (formerly Tor.com), The Independent (Books) |
| 2 | Book Riot, Irish Independent (Books), Jane Friedman, Library Journal infoDOCKET, The Literary Saloon (Complete Review) |
| 1 | Good e-Reader |

**General News**

| Weight | Outlets |
|---|---|
| 5 | BBC News (World), Bloomberg (Politics), Financial Times (World), New York Times (Top Stories), Reuters, Wall Street Journal (World News), Washington Post (Politics) |
| 4 | ABC News (Top Stories), Al Jazeera English, Axios, CBC News (World), CBS News (Latest), Deutsche Welle (English), Los Angeles Times (World & Nation), NBC News (Top Stories), NPR (News), PBS NewsHour (Latest), Politico (Top Stories), The Guardian (World) |
| 3 | Christian Science Monitor (World), Euronews, Fox News (Politics), France 24 (English), RFI (English), Semafor, Sky News (World), South China Morning Post (News), The Hill (Just In), The Independent (World), The Japan Times, TIME |
| 2 | U.S. News & World Report, UPI (Top News) |

## Feedly: history and fallback

Most feeds only expose their latest few dozen items. [Feedly](https://feedly.com)'s public API keeps
the full history of any feed its users subscribe to, with no account needed (`feedly.py`):

- `--backfill DAYS` loads recent history for every feed, so a new install has data immediately.
- If a feed can't be fetched directly (blocked, down, rate-limited), the app uses Feedly's copy.
  These feeds show an amber dot and "via Feedly".
- Backfill works best with the exact feed URL Feedly's subscribers use, so the seed lists prefer
  those URLs (sometimes the older `http://` form).

## How headlines are matched

`matching.py`:

1. Tokenizes each title: lowercases it, strips punctuation, drops stopwords and headline filler,
   applies light stemming, and maps synonyms (reveals/confirms/unveils → announce, lays off → layoff…).
2. Weights tokens by **IDF** across the time window, so distinctive names ("Silksong", "ZeniMax")
   count much more than common words.
3. Scores pairs from *different* feeds that share ≥ 2 tokens by **IDF-weighted cosine similarity**.
4. **Average-linkage clustering**: strongest pairs merge first, and two groups merge only if their
   average cross-pair similarity is ≥ the threshold. This stops chaining, where A~B and B~C pull
   unrelated A and C into one cluster.
5. A story's coverage is the number of *distinct* feeds in its cluster.

### Evaluation

`eval_matching.py` scores the matcher against Overworld's human-curated story groups: 1,171 stories
and 3,513 outlet headlines, May–Nov 2026, downloaded from Feedly and cached in `data/`.

| Version | Precision | Recall | F1 | Stories fully grouped |
|---|---|---|---|---|
| Original (weighted Jaccard, single link, 0.40) | 0.93 | 0.23 | 0.36 | 14% |
| Current (weighted cosine, average link, 0.15) | 0.82 | 0.82 | 0.82 | 75% |
| Tested, not adopted: + sentence embeddings (bge-small) | 0.86 | 0.85 | 0.86 | 79% |

Embeddings added about 3 F1 points but need a ~220 MB install and a model download, so the default
stays dependency-free.

```bash
python3 eval_matching.py --sweep
```

## API

All per-vertical endpoints are under `/api/<vertical>/` (`games`, `movies`, `books`, `news`).

| Method | Path | |
|---|---|---|
| GET | `/api/verticals` | tab list and defaults |
| GET | `/api/<v>/feeds` | feeds and their status |
| POST | `/api/<v>/feeds` `{"url": ...}` | add a feed (validated first) |
| DELETE | `/api/<v>/feeds/{id}` | remove a feed and its items |
| POST | `/api/refresh` | refresh every vertical now |
| PATCH | `/api/<v>/feeds/{id}` `{"weight": 4}` | set a feed's prestige weight |
| GET | `/api/<v>/stories?min_feeds=4&hours=48&threshold=0.15&min_score=0` | aggregated stories, ranked by prestige score |

## Tests

```bash
python3 -m unittest -v
```
