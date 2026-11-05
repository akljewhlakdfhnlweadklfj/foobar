"""Realistic matcher evaluation: full outlet feeds as distractors, Overworld stories as labels.

Needs a games database with history covering the evaluation period, e.g.:
    python3 app.py --data /tmp/eval --backfill 14 --no-serve      # (games vertical is enough)
    python3 eval_realistic.py /tmp/eval/games.db [--sweep]

Every 48-hour window is clustered with *all* headlines in it (as the app does). Only headlines that
appear in an Overworld story's coverage list are scored: pairs from the same Overworld story should
share a cluster (recall); pairs from different stories should not (precision).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import eval_matching
import matching
from store import Store

norm = lambda t: re.sub(r"\W+", " ", t.lower()).strip()


def load(db: Path):
    items = Store(db).items_since(0)
    lo, hi = min(i.published for i in items), max(i.published for i in items)
    by_title = defaultdict(list)
    for i in items:
        by_title[norm(i.title)].append(i)
    label: dict[int, int] = {}
    stories = 0
    for n, story in enumerate(eval_matching.load_ground_truth()):
        if not lo + 86400 <= story[0].published <= hi:
            continue
        found = {m.id for h in story for m in by_title.get(norm(h.title), [])}
        if len(found) >= 2:
            stories += 1
            for iid in found:
                label[iid] = n
    return items, label, stories, (lo, hi)


def evaluate(items, label, threshold, mode, window_h=48):
    matching.TEXT_MODE = mode
    items = sorted(items, key=lambda i: i.published)
    pred: dict[int, tuple[int, int]] = {}
    start, w = 0, 0
    while start < len(items):
        end = start
        while end < len(items) and items[end].published < items[start].published + window_h * 3600:
            end += 1
        batch = [matching.Item(**{**i.__dict__, "tokens": frozenset()}) for i in items[start:end]]
        for c, story in enumerate(matching.cluster(batch, threshold=threshold)):
            for it in story.items:
                if it.id in label:
                    pred[it.id] = (w, c)
        start, w = end, w + 1
    tp = fp = fn = 0
    by_pred = defaultdict(list)
    for iid, p in pred.items():
        by_pred[p].append(iid)
    for members in by_pred.values():
        for a, b in combinations(members, 2):
            tp += label[a] == label[b]
            fp += label[a] != label[b]
    by_story = defaultdict(list)
    for iid, s in label.items():
        by_story[s].append(iid)
    whole = 0
    for members in by_story.values():
        fn += sum(1 for a, b in combinations(members, 2) if pred[a] != pred[b])
        whole += len({pred[i] for i in members}) == 1
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    return p, r, (2 * p * r / (p + r) if p + r else 0), whole / len(by_story)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db", type=Path)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--modes", default="title,title+lead,title+description")
    a = ap.parse_args()
    items, label, stories, (lo, hi) = load(a.db)
    print(f"{len(items)} headlines, {stories} labelled Overworld stories ({len(label)} labelled headlines), "
          f"{(hi - lo) / 86400:.1f} days; {sum(1 for i in items if i.description)} headlines have a description")
    for mode in a.modes.split(","):
        for t in ([0.10, 0.12, 0.15, 0.18, 0.22, 0.26, 0.30] if a.sweep else [matching.DEFAULT_THRESHOLD]):
            p, r, f1, whole = evaluate(items, label, t, mode)
            print(f"{mode:18s} threshold {t:.2f}  precision {p:.3f}  recall {r:.3f}  F1 {f1:.3f}  stories fully grouped {whole:.1%}")


if __name__ == "__main__":
    main()
