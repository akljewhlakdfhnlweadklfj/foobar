"""Measure headline-matching quality against Overworld's human-curated story groups.

Each item in Overworld's feed (https://www.overworld.vg/feed.xml) lists up to three outlets'
original headlines for one story. Those groups are our ground truth: headlines in the same
Overworld story should cluster together; headlines from different stories should not.

    python3 eval_matching.py                 # downloads Overworld's history from Feedly (cached)
    python3 eval_matching.py --sweep         # try a range of thresholds

Reports pairwise precision/recall/F1 and story-level recall (share of Overworld stories whose
headlines all landed in a single cluster).
"""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import feedly
import matching

OVERWORLD_FEED = "https://www.overworld.vg/feed.xml"
CACHE = Path(__file__).parent / "data" / "overworld_history.jsonl"
COVERAGE_RE = re.compile(r'<li><strong>(.*?):</strong>\s*<a href="[^"]+">(.*?)</a></li>', re.S)


def load_ground_truth(refresh: bool = False) -> list[list[matching.Item]]:
    if refresh or not CACHE.exists():
        CACHE.parent.mkdir(exist_ok=True)
        with CACHE.open("w") as f:
            for e in feedly.history(OVERWORLD_FEED):
                f.write(json.dumps(e) + "\n")
    stories, outlets, next_id = [], {}, 0
    for line in CACHE.open():
        e = json.loads(line)
        body = (e.get("content") or e.get("summary") or {}).get("content", "")
        group = []
        for name, title in COVERAGE_RE.findall(body):
            name = html.unescape(name).strip()
            title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
            fid = outlets.setdefault(name, len(outlets))
            group.append(matching.Item(id=next_id, feed_id=fid, title=title,
                                       published=e.get("published", 0) / 1000, feed_title=name))
            next_id += 1
        if len(group) >= 2:
            stories.append(group)
    return stories


def evaluate(stories: list[list[matching.Item]], threshold: float, window_days: float = 2.0) -> dict:
    items = sorted((i for s in stories for i in s), key=lambda i: i.published)
    truth = {i.id: n for n, s in enumerate(stories) for i in s}
    # Cluster in consecutive windows, like the app does with its time window.
    pred: dict[int, tuple[int, int]] = {}
    start, w = 0, 0
    while start < len(items):
        t0 = items[start].published
        end = start
        while end < len(items) and items[end].published < t0 + window_days * 86400:
            end += 1
        for c, story in enumerate(matching.cluster([matching.Item(**{**i.__dict__, "tokens": frozenset()})
                                                    for i in items[start:end]], threshold=threshold)):
            for it in story.items:
                pred[it.id] = (w, c)
        start, w = end, w + 1

    tp = fp = fn = 0
    by_pred = defaultdict(list)
    for iid, p in pred.items():
        by_pred[p].append(iid)
    for members in by_pred.values():
        for a, b in combinations(members, 2):
            if truth[a] == truth[b]:
                tp += 1
            else:
                fp += 1
    for s in stories:
        for a, b in combinations(s, 2):
            if pred[a.id] != pred[b.id]:
                fn += 1
    whole = sum(1 for s in stories if len({pred[i.id] for i in s}) == 1)
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return {"threshold": threshold, "precision": p, "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0, "story_recall": whole / len(stories),
            "stories": len(stories), "headlines": len(items)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=matching.DEFAULT_THRESHOLD)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="re-download Overworld history")
    a = ap.parse_args()
    stories = load_ground_truth(a.refresh)
    for t in ([x / 100 for x in range(5, 45, 3)] if a.sweep else [a.threshold]):
        r = evaluate(stories, t)
        print(f"threshold {r['threshold']:.2f}  precision {r['precision']:.3f}  recall {r['recall']:.3f}  "
              f"F1 {r['f1']:.3f}  stories fully grouped {r['story_recall']:.1%}  "
              f"({r['stories']} stories, {r['headlines']} headlines)")


if __name__ == "__main__":
    main()
