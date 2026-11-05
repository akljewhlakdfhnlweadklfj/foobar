# Consensus News

An RSS/Atom aggregator that only shows stories reported by **at least N** of your feeds, ranked by the
prestige of the outlets covering them, for six verticals: **Games**, **Movies & TV**, **Books**,
**General News**, **Anime** and **Manga**. You can watch for key phrases, which are highlighted and add bonus points.

No dependencies: Python 3.10+ standard library only.

```bash
python3 app.py --backfill 3     # first run: seed every tab and load 3 days of history
python3 app.py                  # afterwards
# open http://localhost:8000
```

| Option | |
|---|---|
| `--port 8000`, `--host 127.0.0.1` | where to listen |
| `--data data/` | folder with one database per tab |
| `--interval 15` | minutes between automatic refreshes |
| `--no-auto` | no scheduled refreshes; something else calls the scrape API |
| `--backfill DAYS` | load up to 30 days of history from Feedly first (resumable) |
| `--import FILE --vertical games` | add the feed URLs in FILE to a tab |
| `--no-serve` | exit after seeding / import / backfill |
| `--api-token TOKEN` (or `$NEWS_API_TOKEN`) | let other machines call the API with `Authorization: Bearer TOKEN` |
| `--allow-host HOST` | extra hostname allowed to make changes through the browser (`*` = any) |
| `--allow-private-feeds` | allow feed URLs on localhost / private networks (for testing) |
| `--no-resume` | don't resume an interrupted scrape on startup |

## Run in GitHub Codespaces

On the repo page, click **Code → Codespaces → Create codespace** on this branch. The dev container
seeds every tab and backfills 3 days on first creation, starts the server on every start, and opens
the app in a browser tab (or use the **Ports** panel → port 8000). Server log: `/tmp/news-aggregator.log`.

The server is already running in the Codespace, so `python3 app.py` there fails with "address already
in use". To restart it after changing code:

```bash
pkill -f app.py; nohup python3 app.py --host 0.0.0.0 --port 8000 > /tmp/news-aggregator.log 2>&1 &
```

Codespaces stop after ~30 minutes idle and count against your free monthly hours. Data persists in
`data/` until the codespace is deleted.

**Keep the port private.** Without an API token, anyone who can open the app can change feeds and
phrases. A Codespaces port is private (only you, signed in) by default; don't switch it to Public
unless you trust everyone with the link.

## Using it

The app has two sections (top left):

**Stories**: the reader. Pick **All** (every vertical, each using its own settings, with a label on
each story) or a single vertical, and sort by **Top** (score) or **Latest**.
- The list is compact: one line per story, **`Key term: Headline`**.
  - **Key term**: the story's most prominent proper noun (a person, title, brand or place: "Suda51",
    "God of War Laufey", "China Miéville"), kept whole when it's a name with small connecting words.
    A term must be in at least 40% of the story's headlines. Its prominence is:
    `(share of headlines x position + 0.3 x share of descriptions) x distinctiveness`, where
    *position* weighs a term 2x at the start of a headline down to 1x at the end, and
    *distinctiveness* favours terms specific to this story. Names get a 1.5x boost. The lead is the
    top proper noun if it scores at least 35% of the top term; otherwise the top noun-like term
    (verbs and adjectives like "delayed", "Black" are skipped via a word list and word endings,
    since there's no part-of-speech tagger). Shown in the accent colour; click it to show only
    stories mentioning it.
  - **Headline**: the shortest one among the story's outlets (4+ words; labelled pieces like
    "Opinion |" or "WATCH:" are skipped when possible).
- Click a story (or its arrow) to expand it: outlets and score, outlet and times, description,
  image (when the feeds have one; loaded only when expanded), watched-phrase chips, and every outlet's
  headline. Description and image fall back to the highest-weighted outlet that has them.
  Watched phrases are highlighted.

**Settings**: one sub-tab per vertical, plus **Scraping**. Your settings, watched phrases and outlet
choices are **saved in your browser** (each viewer has their own; see [Architecture](#architecture-many-viewers-little-server-work)).
- **Which stories show** (per vertical): reported by at least N outlets, or at
  least a percentage of the outlets that can count: switched on and weighted at least the
  "only count outlets weighted at least" value (rounded up, minimum 1; it follows both settings), time window, only count outlets weighted at least X (lower-weighted outlets still help group headlines
  but don't count toward N or the score), minimum score, match strictness, and, when outlets are
  marked required, whether stories need any or all of them. Time windows go up to 30 days.
  Changes save automatically; **Reset to defaults** returns to the vertical's defaults.
- **Watched phrases**: add a phrase and a bonus (0–100). Stories mentioning it in any outlet's title
  or description are outlined and highlighted, and the bonus is added to the score. Options: only
  show watched stories; show watched stories even if fewer outlets report them.
- **Outlets**: switch each on or off for you (off = left out of your stories); mark **Required**
  (stories must include it); set your own weight (0–10); search; all on / all off.
  **Catalog (all users)** under an outlet changes the shared catalog: its default weight, keyword
  filter, or removing it. **Shared catalog (admin)** adds feeds and sets the match strictness. These
  go to the server and regroup stories for everyone; until the new grouping is ready (seconds, up to
  about half a minute for General News) a "Re-grouping in progress…" flag shows and your new value stays put.
- **Scraping**: progress of the current run, Refresh all, Stop, Resume, Backfill N days, and the
  automatic schedule (on/off and interval).

The header always shows scraping status, with a quick Refresh / Stop.

## Architecture: many viewers, little server work

Grouping headlines into stories is the only expensive step, and it doesn't depend on who is looking.
So the server does it **once per vertical after each scrape** (and after a catalog change), for
three windows (2, 7 and 30 days), and saves the result as a static **digest**:
`data/digests/<vertical>-<hours>.json` (plus `.json.gz`), also kept in memory and served by
`GET /api/<v>/digest?hours=N` with gzip, an `ETag` (unchanged digests cost a 304) and
`Cache-Control: public, max-age=60`. Every viewer downloads the same file, so the files can sit behind
a CDN or be served by any static host.

The browser does everything personal: which outlets are on, weights, required outlets, "only count
outlets weighted at least", watched phrases and bonuses, "reported by at least N / N%", minimum
score, the exact time window (cut from the smallest digest that covers it), sorting and key-term
filters. Preferences live in `localStorage` (`consensus-prefs-v1`), so the server stores no per-user
state. A digest holds every outlet in the catalog (including ones switched off by default), one
headline per outlet per story (its shortest full one), descriptions trimmed to 240 characters, and the
story's key terms. Stories reported by a single outlet are left out to keep files small, so the
lowest "reported by" setting is effectively 2.

Rough sizes (2 days of data): General News ~690 stories, ~480 KB gzipped, built in ~8 s; Games ~200
stories, ~130 KB. Settings and phrases saved on the server by earlier versions remain as each
vertical's defaults (`/api/<v>/settings`), but personal changes are now browser-only.

## Verticals and data

| Vertical | Seed list | Database |
|---|---|---|
| Games | `feeds/games.txt` | `data/games.db` |
| Movies & TV | `feeds/movies.txt` | `data/movies.db` |
| Books | `feeds/books.txt` | `data/books.db` |
| General News | `feeds/news.txt` | `data/news.db` |
| Anime | `feeds/anime.txt` | `data/anime.db` |
| Manga | `feeds/manga.txt` | `data/manga.db` |

- **Separate databases**: each tab is one self-contained SQLite file (feeds, headlines, watched
  phrases, story settings). To migrate, copy the `.db` file and point `--data` at its folder. Older databases are
  upgraded in place (schema version in `PRAGMA user_version`; history in `store.py`).
- **Seed lists** are read once, when a tab's database is created. Format:
  `URL  # Display name | w=N | only=keyword, keyword | notes` (`only=` is optional).
  After that:
  - **Display names** are re-applied on every start.
  - **New URLs** are not added automatically. Add them with
    `python3 app.py --import feeds/<vertical>.txt --vertical <vertical> --no-serve` (existing feeds are skipped).
  - **Removed URLs** stay until you remove them in the UI. Deleting every feed in a tab doesn't bring
    the seeds back.
  - **Weights** only fill feeds that have none, so change an existing feed's weight in the UI.
- **Retention**: headlines are kept for 30 days (`RETENTION_DAYS` in `store.py`), the longest time window.
- **One feed per publisher** in each vertical. Two feeds from the same newsroom (e.g. NYT Movies and
  NYT Television) often carry identical headlines and would count one story twice toward N.
- **Cached history** (`seed_cache/<vertical>.jsonl.gz`, currently Anime and Manga): 14 days of
  pre-fetched items, loaded in about a second the first time a vertical has feeds but no headlines,
  instead of a slow Feedly backfill. Items older than the 30-day retention are skipped on load, so rebuild the cache
  now and then:
  ```bash
  python3 app.py --data /tmp/seed --backfill 14 --no-serve
  python3 tools/build_seed_cache.py /tmp/seed anime manga
  ```
- **Keyword filters** (`only=`, or "Keyword filter" under an outlet in Settings): the outlet only
  contributes items whose headline or description mentions one of the keywords (matched at word
  starts, so "manga" also matches "mangaka"). Applied when stories are built, so changing it is
  instant. Manga uses this because no anime/manga outlet has a manga-only feed: the shared feeds
  (Anime News Network, MyAnimeList, ComicBook.com, Anime Mojo) and mixed ones (ICv2, The Outerhaven,
  Publishers Weekly Comics) only count their manga items.
- **Games** sources are the outlets [Overworld](https://www.overworld.vg/) aggregates, extracted from
  its full story archive. The other verticals mix trade publications with section feeds of general
  publications (NYT, WSJ, Guardian, etc.).
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

- A story's **score** = the sum of its distinct (counted) outlets' weights + the bonus of every watched
  phrase it mentions.
- Weights decide ranking, not the headline: each story shows its shortest headline (see Stories above).
- Weights can be changed per outlet in the UI (0–10); feeds you add yourself start at 2.

**Games** (57 feeds)

| Weight | Outlets |
|---|---|
| 5 | GamesIndustry.biz |
| 4 | Aftermath, Ars Technica (Gaming), Digital Foundry, Eurogamer, Game Developer, Game File, Game Informer, GameSpot, Gematsu, IGN, Kotaku, PC Gamer, Polygon, Rock Paper Shotgun, The Game Business, The Verge (Gaming), Video Games Chronicle |
| 3 | GamesBeat, GamesRadar+, Massively Overpowered, Nintendo Life, PlayStation Blog, Push Square, Rogue, RPG Site, Shacknews, Siliconera, Time Extension, Xbox Wire |
| 2 | DualShockers, GamingOnLinux, GoNintendo, Insider Gaming, Jank, MMORPG.com, My Nintendo News, Nintendo Everything, Nintendo World Report, Noisy Pixel, Pocket Tactics, Pure Xbox, RPGamer, TechRaptor |
| 1 | COGConnected, Console Creatures, covergeek, Crossplay, GameGrin, Hey Poor Player, Loot Level Chill, MonsterVine, PixelByte, Retro Handhelds, So Many Games, The Wand Report, Vooks |

**Movies & TV** (29 feeds)

| Weight | Outlets |
|---|---|
| 5 | Deadline, New York Times (Movies), The Hollywood Reporter, Variety |
| 4 | BBC News (Entertainment & Arts), IndieWire, Los Angeles Times (Entertainment & Arts), NPR (Movies), Screen Daily (Latest), The Guardian (Film), TheWrap, Vulture |
| 3 | /Film, ABC News (Entertainment), Boxoffice Pro, CBC (Arts), CBS News (Entertainment), Empire, Rolling Stone (TV & Movies), The A.V. Club, The Independent (Film), The Playlist, TVLine |
| 2 | Collider, ComingSoon.net, Fox News (Entertainment), Gold Derby, JoBlo, World of Reel |

**Books** (24 feeds)

| Weight | Outlets |
|---|---|
| 5 | New York Times (Books), Publishers Weekly (All recent), The Bookseller (News) |
| 4 | Financial Times (Books), Literary Hub, NPR (Books), Publishers Lunch, The Atlantic (Books), The Guardian (Books) |
| 3 | Christian Science Monitor (Books), CrimeReads, Electric Literature, Locus, Los Angeles Times (Books), PEN America, Publishing Perspectives, Reactor (formerly Tor.com), The Independent (Books) |
| 2 | Book Riot, Irish Independent (Books), Jane Friedman, Library Journal infoDOCKET, The Literary Saloon (Complete Review) |
| 1 | Good e-Reader |

**General News** (32 feeds)

| Weight | Outlets |
|---|---|
| 5 | BBC News (World), Bloomberg (Politics), Financial Times (World), New York Times (Top Stories), Reuters, Wall Street Journal (World News), Washington Post (Politics) |
| 4 | ABC News (Top Stories), Al Jazeera English, Axios, CBC News (World), CBS News (Latest), Deutsche Welle (English), Los Angeles Times (World & Nation), NBC News (Top Stories), NPR (News), PBS NewsHour (Latest), Politico (Top Stories), The Guardian (World) |
| 3 | Christian Science Monitor (World), Euronews, Fox News (Politics), France 24 (English), RFI (English), Semafor, Sky News (World), South China Morning Post (News), The Hill (Just In), The Independent (World), The Japan Times, TIME |
| 2 | UPI (Top News) |

**Anime** (14 feeds)

| Weight | Outlets |
|---|---|
| 5 | Anime News Network |
| 4 | Crunchyroll News, MyAnimeList News |
| 3 | Animation Magazine, Anime Corner, Anime Trending, Cartoon Brew |
| 2 | Animehunch, CBR (Anime News), ComicBook.com (Anime), Screen Rant (Anime News) |
| 1 | Anime Mojo, Game Rant (Anime), OtakuKart |

**Manga** (9 feeds)

| Weight | Outlets |
|---|---|
| 5 | Anime News Network (keyword-filtered) |
| 4 | ICv2 (keyword-filtered), Publishers Weekly (Comics) (keyword-filtered) |
| 3 | MyAnimeList News (keyword-filtered), The Beat (Manga) |
| 2 | ComicBook.com (Anime) (keyword-filtered), Seven Seas Entertainment |
| 1 | Anime Mojo (keyword-filtered), The Outerhaven (keyword-filtered) |

## Scraping: API, scheduling and resume

A *scrape run* either **refreshes** feeds (fetches them now) or **backfills** them (loads up to 30 days
of history from Feedly). By default a refresh runs every 15 minutes; turn that off (`--no-auto`, the
Auto checkbox, or the schedule endpoint) to drive scraping from cron, Airflow or similar.

| Method | Path | Body | |
|---|---|---|---|
| GET | `/api/scrape/status` | | state (`idle` / `running` / `stopping`), current run with progress, last completed run, schedule |
| POST | `/api/scrape/start` | `{"kind": "refresh"}` or `{"kind": "backfill", "days": 3}`, optional `"verticals": ["news"]`, `"resume": false` | start, or resume the unfinished run of the same kind (default) |
| POST | `/api/scrape/stop` | | stop after the feeds in progress; the run stays resumable |
| POST | `/api/scrape/schedule` | `{"auto": false}`, `{"interval_minutes": 30}` | change the schedule (kept across restarts) |

```bash
curl -X POST -H "Authorization: Bearer $NEWS_API_TOKEN" -H "Content-Type: application/json" \
     -d '{"kind": "refresh"}' http://localhost:8000/api/scrape/start
```

**Resume.** Progress is saved after every feed: the run (`data/scrape_state.json`, written atomically)
and, in each tab's database, the run that last finished each feed. A stopped run resumes where it
left off when started again; a run interrupted by a crash or restart resumes automatically on startup.
Feeds already done in that run aren't fetched again. A backfill also remembers how far back each feed
has been loaded, so a later backfill for the same or fewer days skips it.

## Feedly: history, fallback and throttling

Most feeds only expose their latest few dozen items. [Feedly](https://feedly.com)'s public API keeps
the full history of any feed its users subscribe to, with no account needed (`feedly.py`). The app
uses it for backfills, and as a fallback when a feed can't be fetched directly (blocked, down);
those feeds show an amber dot and "via Feedly".

Feedly rate-limits by IP and doesn't publish its limits, so requests are paced (about 2 per second,
shared by all threads). A `429 Too Many Requests` pauses all Feedly traffic for 30 s, doubling on each
further 429 up to 5 minutes (or as long as Feedly's `Retry-After` says). A backfill saves items as they
arrive; if Feedly keeps refusing, the run stops, keeps everything loaded so far, and can be resumed.
During normal refreshes the fallback doesn't wait out a pause: the feed keeps its headlines and is
retried at the next refresh.

Backfill works best with the exact feed URL Feedly's subscribers use, so the seed lists prefer those
URLs (sometimes the older `http://` form).

## How headlines are matched

`matching.py`:

1. **Text**: each headline plus the first sentence of its description.
2. **Tokens**: lowercased, punctuation stripped, stopwords and headline filler dropped, light stemming,
   synonyms (reveals/confirms/unveils → announce, lays off → layoff, U.S./America → usa…). Words that
   appear in most of one feed's items (a " - Reuters" suffix, "Review:") are ignored for that feed.
3. **Weights**: IDF across the time window, so distinctive names ("Silksong", "ZeniMax") count most.
4. **Similarity**: IDF-weighted cosine, for pairs from different outlets sharing ≥ 2 tokens.
5. **Clustering**: average linkage (UPGMA). Groups merge while their average cross-pair similarity is
   ≥ the threshold, which stops chaining (A~B and B~C pulling unrelated A and C together). Running
   similarity sums are updated on each merge, so ~9,000 headlines cluster in about 3 seconds.
   Results are cached until new data arrives.

### Evaluation

Two checks against [Overworld](https://www.overworld.vg/)'s human-curated story groups:

- `eval_matching.py`: 1,183 Overworld stories (May–Nov 2026, 3,549 outlet headlines), titles only.
- `eval_realistic.py DB`: 14 days of the full Games feeds (~6,800 headlines) as distractors, clustered
  in 48-hour windows like the app, scored on the 65 Overworld stories from that period. Closer to real use.

| Version | Titles-only eval F1 | Realistic eval: precision / recall / F1 / stories fully grouped |
|---|---|---|
| Original (weighted Jaccard, single link, 0.40) | 0.36 | |
| Cosine + average link, titles, 0.15 | 0.82 | 0.98 / 0.78 / 0.87 / 68% |
| **Current: + description's first sentence, 0.12** | (titles only) | **1.00 / 0.87 / 0.93 / 80%** |
| Title + full description, 0.12 | | 1.00 / 0.79 / 0.88 / 72% |
| Tested, not adopted: sentence embeddings (bge-small) | 0.86 | |

Embeddings added little and need a ~220 MB install and a model download, so the default stays
dependency-free. The realistic sample is small (65 stories), so treat its numbers as indicative.

```bash
python3 eval_matching.py --sweep
python3 app.py --data /tmp/eval --backfill 14 --no-serve && python3 eval_realistic.py /tmp/eval/games.db --sweep
```

## Security

The app has no user accounts. To make it safe to run on a laptop or in a Codespace:

- Changes (POST/PATCH/DELETE) from a browser must come from the app's own page: POST bodies must be
  JSON, a browser `Origin` must match the `Host`, and the `Host` must be localhost, a Codespaces
  forwarding domain, or one given with `--allow-host` (stops other websites and DNS-rebinding pages).
- With `--api-token`, requests carrying `Authorization: Bearer <token>` are allowed from anywhere.
- Feed URLs (and every redirect) may not point to localhost, private or link-local addresses
  unless `--allow-private-feeds` is set. Downloads are capped at 10 MB and 45 seconds.
- Request bodies are capped at 64 KB; malformed requests get a 4xx error.

## API

Per-tab endpoints are under `/api/<vertical>/` (`games`, `movies`, `books`, `news`, `anime`, `manga`).

| Method | Path | |
|---|---|---|
| GET | `/api/verticals` | tab list, defaults, digest `windows`, `retention_days` |
| GET | `/api/<v>/digest?hours=N` | the shared digest covering N hours (gzip, ETag, 60 s cache); what the UI uses |
| GET | `/api/<v>/feeds` | feeds, their status, and scrape status |
| POST | `/api/<v>/feeds` `{"url": ...}` | add a feed (validated first) |
| PATCH | `/api/<v>/feeds/{id}` `{"weight": 4, "enabled": false, "required": true, "only": "manga, manhwa"}` | change one feed (`"only": ""` clears the filter) |
| PATCH | `/api/<v>/feeds` `{"enabled": true}` / `{"required": false}` | switch every feed on or off / clear required |
| DELETE | `/api/<v>/feeds/{id}` | remove a feed and its items |
| GET | `/api/<v>/phrases` | watched phrases |
| POST | `/api/<v>/phrases` `{"phrase": "Switch 2", "bonus": 5}` | watch a phrase |
| PATCH | `/api/<v>/phrases/{id}` `{"bonus": 10}` | change its bonus |
| DELETE | `/api/<v>/phrases/{id}` | stop watching it |
| GET | `/api/<v>/settings` | the vertical's story settings (and defaults) |
| PATCH | `/api/<v>/settings` `{"min_feeds": 3, "hours": 72}`, `{"min_feeds_mode": "percent", "min_feeds_percent": 10}` / `{"reset": true}` | change / reset them (responses include `min_feeds_effective`) |
| GET | `/api/<v>/stories` | (for scripts; the UI uses digests) stories ranked by score, using the saved settings; any of `min_feeds`, `hours`, `threshold`, `min_score`, `min_weight`, `only_watched`, `watched_any`, `require_all` as query parameters override them |
| | `/api/scrape/...` | see [Scraping](#scraping-api-scheduling-and-resume) |

## Code layout

| File | |
|---|---|
| `app.py` | HTTP server, API, security, stories (scoring, phrases), CLI |
| `scraper.py` | scrape runs: start/stop, progress, schedule, resume; refresh and backfill |
| `store.py` | SQLite storage and schema upgrades |
| `feeds.py` | fetching (with SSRF guard) and RSS/Atom parsing |
| `feedly.py` | Feedly API: history paging, pacing, rate-limit handling |
| `matching.py` | tokenizer, similarity, clustering, lead headline |
| `eval_matching.py`, `eval_realistic.py` | matching evaluations |
| `seedcache.py`, `tools/build_seed_cache.py` | cached history for new verticals (load / build) |
| `digest.py` | shared per-vertical story digests (build, cache, disk, background rebuilds) |
| `tools/build_snapshot.py` | static snapshot of the UI with the digests embedded (for sharing as a page) |
| `static/index.html` | the UI |

## Tests

```bash
python3 -m unittest -v
```
