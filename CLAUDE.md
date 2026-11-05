# Notes for Claude Code sessions on this repo

## Showing the user the UI (Claude cloud sessions)

- **Live preview does not work in cloud sessions.** Starting the server on 0.0.0.0:8000 and calling
  `report_preview_port` was tried; the user's app said "preview isn't available in this session"
  (it only works for local/SSH sessions). Don't offer it again from a cloud session.
- **What works: a static snapshot published as a claude.ai Artifact.**
  `tools/build_snapshot.py <data dir> <out.html>` renders the real UI with each filter group's stories
  embedded: Stories (group tabs), last seen and Settings › Watched phrases work; the group editor,
  catalogs and Scraping are hidden. `experiments/ai-headlines/add_tab.py <out.html> experiments/ai-headlines`
  adds the one-time AI headline test as a tab.
  Publish it with the Artifact tool. To update the existing
  snapshot, publish the same file path again (or pass the artifact's URL) so the link stays the same:
  https://claude.ai/artifact/WM6AWSgespPKpaW8r2JAcd
  Before building, refresh the data (a backfill into a scratch data dir works when sites are blocked).
- For a live app the user runs it themselves (`python3 app.py --backfill 3`) or uses Codespaces.

## Seed caches

`seed_cache/anime.jsonl.gz` and `seed_cache/manga.jsonl.gz` hold 14 days of pre-fetched items (built
2026-11-05) so those verticals start full without a slow backfill. They go stale after ~14 days:
rebuild with a 14-day backfill into a scratch data dir, then `python3 tools/build_seed_cache.py <dir> anime manga`.

## Architecture

The server groups stories once per vertical after each scrape (digest.py: 48h/168h/720h digests on
disk and in memory), then applies each filter group's settings to them (groups.py; cached per group,
served as static bytes). The browser only does watched phrases and "last seen" (localStorage). Keep
per-viewer state out of the server and per-group work out of the browser. AI headlines (ai.py) are
optional and need ANTHROPIC_API_KEY; tests use a fake client.

## Conventions

- Commit as `news-aggregator <noreply@example.com>` (set in the repo's git config). Never commit
  personal names/emails or local paths; history was rewritten once to remove an email.
- Push to the `news-aggregator` branch of `akljewhlakdfhnlweadklfj/foobar`; `main` there is unrelated.
- Run `python3 -m unittest` before committing. Matching changes: also run `python3 eval_matching.py`
  and `python3 eval_realistic.py <games.db with 14 days of history>`.
