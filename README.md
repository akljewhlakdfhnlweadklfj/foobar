# Consensus News

An RSS/Atom aggregator that only shows stories reported by **at least N** of your feeds, ranked by the
prestige of the outlets covering them, with tabs for **Games**, **Movies & TV**, **Books** and
**General News**. You can watch for key phrases, which are highlighted and add bonus points.

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
| `--backfill DAYS` | load up to 14 days of history from Feedly first (resumable) |
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

- **Tabs**: one per vertical; each remembers its own filter settings.
- **Reported by at least N**: the core filter, counting distinct outlets.
- **Only count outlets weighted at least**: outlets below this weight still help group headlines and
  are listed (greyed out, "doesn't count"), but don't count toward "reported by" or the score.
- **Minimum prestige score**: optional; stories are ranked by score (see weights below).
- **Required outlets**: tick "Required" on outlets to only show stories that at least one of them
  reports (or all of them: choose in the Filter panel). Applies to watched stories too.
- **Time window** and **match strictness**: which headlines are compared, and how similar they must be.
- **Watched phrases**: add a phrase and a bonus (0–100). Stories mentioning it in any outlet's title or
  description are outlined, the phrase is highlighted, and the bonus is added to the score. Options:
  only show watched stories, and show watched stories even if fewer outlets report them.
- **Outlets**: switch each on or off (off = left out of stories, still fetched), set its weight
  (0–10), search the list, or turn all on/off. Add any RSS/Atom URL; remove with ×.
- **Scraping** (top right): progress while a refresh or backfill runs, Stop, Resume, and Auto (schedule).

## Verticals and data

| Vertical | Seed list | Database |
|---|---|---|
| Games | `feeds/games.txt` | `data/games.db` |
| Movies & TV | `feeds/movies.txt` | `data/movies.db` |
| Books | `feeds/books.txt` | `data/books.db` |
| General News | `feeds/news.txt` | `data/news.db` |

- **Separate databases**: each tab is one self-contained SQLite file (feeds, headlines, watched
  phrases). To migrate, copy the `.db` file and point `--data` at its folder. Older databases are
  upgraded in place (schema version in `PRAGMA user_version`; history in `store.py`).
- **Seed lists** are read once, when a tab's database is created. Format: `URL  # Display name | w=N | notes`.
  After that:
  - **Display names** are re-applied on every start.
  - **New URLs** are not added automatically. Add them with
    `python3 app.py --import feeds/<vertical>.txt --vertical <vertical> --no-serve` (existing feeds are skipped).
  - **Removed URLs** stay until you remove them in the UI. Deleting every feed in a tab doesn't bring
    the seeds back.
  - **Weights** only fill feeds that have none, so change an existing feed's weight in the UI.
- **One feed per publisher** in each vertical. Two feeds from the same newsroom (e.g. NYT Movies and
  NYT Television) often carry identical headlines and would count one story twice toward N.
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
- Each story shows the headline from its highest-weighted outlet.
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

## Scraping: API, scheduling and resume

A *scrape run* either **refreshes** feeds (fetches them now) or **backfills** them (loads up to 14 days
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

Per-tab endpoints are under `/api/<vertical>/` (`games`, `movies`, `books`, `news`).

| Method | Path | |
|---|---|---|
| GET | `/api/verticals` | tab list and defaults |
| GET | `/api/<v>/feeds` | feeds, their status, and scrape status |
| POST | `/api/<v>/feeds` `{"url": ...}` | add a feed (validated first) |
| PATCH | `/api/<v>/feeds/{id}` `{"weight": 4, "enabled": false, "required": true}` | change one feed |
| PATCH | `/api/<v>/feeds` `{"enabled": true}` / `{"required": false}` | switch every feed on or off / clear required |
| DELETE | `/api/<v>/feeds/{id}` | remove a feed and its items |
| GET | `/api/<v>/phrases` | watched phrases |
| POST | `/api/<v>/phrases` `{"phrase": "Switch 2", "bonus": 5}` | watch a phrase |
| PATCH | `/api/<v>/phrases/{id}` `{"bonus": 10}` | change its bonus |
| DELETE | `/api/<v>/phrases/{id}` | stop watching it |
| GET | `/api/<v>/stories?min_feeds=4&hours=48&threshold=0.12&min_score=0&min_weight=0&only_watched=0&watched_any=0&require_all=0` | stories, ranked by score |
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
| `tools/build_snapshot.py` | static read-only snapshot of the UI (for sharing as a page) |
| `static/index.html` | the UI |

## Tests

```bash
python3 -m unittest -v
```
