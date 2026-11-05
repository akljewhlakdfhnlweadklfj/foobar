"""Pick the test stories and write the model input (as run on Nov 5, 2026).

    python3 experiments/ai-headlines/select_stories.py DATA_DIR

Top stories by catalog weight from each vertical's 2-day digest (News 30, Games 25, Movies & TV 25,
Anime 10, Books 5, Manga 5). Writes stories.json and input.txt next to this file.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))
import app  # noqa: E402

QUOTA = {"news": 30, "games": 25, "movies": 25, "anime": 10, "books": 5, "manga": 5}

stores = app.open_stores(Path(sys.argv[1]), seed=False)
digests = app.make_digests(stores)
out = []
for v, n in QUOTA.items():
    d = digests.data(v, 48)[1]
    w = {f["id"]: (f["weight"] or 0 if f["enabled"] else 0) for f in stores[v].list_feeds()}
    for s in sorted(d["stories"], key=lambda s: -sum(w[x["f"]] for x in s["s"]))[:n]:
        titles = list(dict.fromkeys(x["t"] for x in s["s"]))
        short = min((t for t in titles if len(t.split()) >= 4), key=len, default=min(titles, key=len))   # the rule as of the test
        out.append({"n": len(out) + 1, "vertical": v, "id": s["id"], "outlets": len(s["s"]),
                    "current": short, "headlines": titles})
json.dump(out, open(HERE / "stories.json", "w"), ensure_ascii=False, indent=1)
lines = []
for s in out:
    lines.append(f"[{s['n']}]")
    lines += [f"- {t}" for t in s["headlines"]]
(HERE / "input.txt").write_text("\n".join(lines))
print(f"{len(out)} stories, {sum(len(s['headlines']) for s in out)} headlines")
