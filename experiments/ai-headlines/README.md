# AI headline test (one-time, Nov 5, 2026)

Can an LLM write a shorter, still coherent headline for each story than the app's current rule
(the shortest outlet headline)? Kept as a record: the app now has this as a feature ("AI summary" per filter group, `ai.py`), and nothing here is used by it.

## Setup

- **Stories:** the top 100 from the 2-day digests by catalog weight: General News 30, Games 25,
  Movies & TV 25, Anime 10, Books 5, Manga 5 (`select_stories.py`, output in `stories.json`).
- **Input:** every outlet's headline for each story, 934 headlines in all (`input.txt`), with the
  instructions in `system.txt`, sent as a single request.
- **Model:** Claude Opus 5.5. No API key was available, so the request ran inside a Claude Code
  session rather than through the billed API. Output: `output.json` (story number → headline).

## Results

- **Not shorter:** 8.5 words on average vs 8.4 for the current headline; 56 of 100 are longer, since
  the model adds specifics ("Democrat Annie Andrews flips South Carolina Senate seat").
- **Fixes wrong headlines:** when grouping pulls an unrelated article into a story, that stray headline
  is sometimes the shortest, so the app shows it. The model writes what most outlets agree on:
  - Everest helicopter collision, currently shown as "Miami helicopter crash leaves one person dead…"
  - Steam review-process changes, currently "CRYMELIGHT Delayed on PC"
  - Bill Hader on *Abbott Elementary*, currently a review of *The Cat in the Hat*

## Cost

About 71,000 characters in and 6,300 out, roughly 18–24K input and 1.6–2.1K output tokens
(estimated from size, not billed):

| Model | Cost for 100 stories |
|---|---|
| Claude Opus 5.5 ($4 / $20 per M tokens) | about $0.10–$0.14 |
| Claude Sonnet 5.5 ($2 / $10) | about $0.05–$0.07 |
| Claude Haiku 5.5 ($0.10 / $0.50) | about $0.003 |

## Viewing

`add_tab.py` adds an "AI headlines" tab (just the 100 headlines; click one for its outlets'
headlines) to a snapshot built with `tools/build_snapshot.py`. It matches stories by id, so it only
lines up with the data the test was run on.
