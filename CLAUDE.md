# Notes for Claude Code sessions on this repo

## Showing the user the UI (Claude cloud sessions)

- **Live preview does not work in cloud sessions.** Starting the server on 0.0.0.0:8000 and calling
  `report_preview_port` was tried; the user's app said "preview isn't available in this session"
  (it only works for local/SSH sessions). Don't offer it again from a cloud session.
- **What works: a static snapshot published as a claude.ai Artifact.**
  `tools/build_snapshot.py <data dir> <out.html>` renders the real UI's Stories view (All + each
  vertical, sort) with the current stories embedded and each vertical's saved settings applied;
  the Settings section is hidden. Publish it with the Artifact tool. To update the existing
  snapshot, publish the same file path again (or pass the artifact's URL) so the link stays the same:
  https://claude.ai/artifact/WM6AWSgespPKpaW8r2JAcd
  Before building, refresh the data (a backfill into a scratch data dir works when sites are blocked).
- For a live app the user runs it themselves (`python3 app.py --backfill 3`) or uses Codespaces.

## Conventions

- Commit as `news-aggregator <noreply@example.com>` (set in the repo's git config). Never commit
  personal names/emails or local paths; history was rewritten once to remove an email.
- Push to the `news-aggregator` branch of `akljewhlakdfhnlweadklfj/foobar`; `main` there is unrelated.
- Run `python3 -m unittest` before committing. Matching changes: also run `python3 eval_matching.py`
  and `python3 eval_realistic.py <games.db with 14 days of history>`.
