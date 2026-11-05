"""Group headlines from different feeds that describe the same story.

Different outlets word the same story differently ("Fed raises rates by 0.25%"
vs "Federal Reserve hikes interest rates a quarter point"), so exact matching
is useless. Instead:

1. Normalise each title into a set of content tokens (lower-case, strip
   punctuation, drop stopwords, crude stemming).
2. Weight tokens by IDF over the current window, so rare words ("Zelensky",
   "Boeing") count for much more than common ones ("says", "new").
3. Similarity = cosine of the IDF-weighted token sets, for pairs from *different* feeds that
   share at least 2 tokens.
4. Agglomerative clustering with average linkage: strongest pairs first, and two groups merge
   only if their average cross-pair similarity clears the threshold. (Single linkage chained
   unrelated stories into one giant cluster on real data.)
5. A cluster's "coverage" is the number of distinct feeds in it.

Tuned with eval_matching.py against Overworld's human-curated story groups.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be been before
    being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers him his how i if in
    into is it its itself just me more most my no nor not now of off on once only
    or other our out over own same she should so some such than that the their
    them then there these they this those through to too under until up very was
    we were what when where which while who whom why will with would you your
    says said say saying new news live update updates latest breaking report
    reports reported watch video photos analysis opinion exclusive amid via
    after over us vs
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# A few common headline synonyms, mapped to one canonical form.
SYNONYMS = {
    "hike": "raise", "hikes": "raise", "hiked": "raise", "raises": "raise", "raised": "raise",
    "cut": "cut", "cuts": "cut", "slashes": "cut", "slash": "cut",
    "dies": "die", "died": "die", "dead": "die", "death": "die",
    "kills": "kill", "killed": "kill", "killing": "kill",
    "wins": "win", "won": "win", "victory": "win",
    "federal": "fed",
    "u.s": "us", "usa": "us", "america": "us", "american": "us",
    "uk": "britain", "british": "britain",
    "pm": "minister",
    "lay": "layoff", "lays": "layoff", "laid": "layoff", "redundancies": "layoff", "job": "layoff",
    # Announcement verbs are interchangeable in headlines.
    "reveal": "announce", "reveals": "announce", "revealed": "announce",
    "confirm": "announce", "confirms": "announce", "confirmed": "announce",
    "unveil": "announce", "unveils": "announce", "unveiled": "announce",
    "announces": "announce", "announced": "announce", "announcement": "announce",
    "delay": "delay", "delays": "delay", "delayed": "delay", "postponed": "delay", "pushed": "delay",
    "ps5": "playstation", "ps": "playstation",
}


def _stem(tok: str) -> str:
    """Very small suffix stripper: enough to merge plurals / simple tenses."""
    if tok.isdigit() or len(tok) <= 3:
        return tok
    for suf in ("ings", "ing", "ies", "ied", "es", "ed", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[: -len(suf)]
            if suf in ("ies", "ied"):
                base += "y"
            tok = base
            break
    # Drop a trailing 'e' so "price"/"prices" and "announce"/"announced" agree.
    if len(tok) >= 4 and tok.endswith("e"):
        tok = tok[:-1]
    return tok


def tokenize(title: str) -> frozenset[str]:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    text = text.lower().replace("'s", "").replace("’s", "")
    out = set()
    for raw in _TOKEN_RE.findall(text):
        if raw in STOPWORDS:
            continue
        tok = SYNONYMS.get(raw, raw)
        tok = _stem(tok)
        tok = SYNONYMS.get(tok, tok)
        if tok in STOPWORDS or len(tok) < 2:
            continue
        out.add(tok)
    return frozenset(out)


@dataclass
class Item:
    id: int
    feed_id: int
    title: str
    link: str = ""
    published: float = 0.0  # unix timestamp
    feed_title: str = ""
    tokens: frozenset[str] = field(default_factory=frozenset)


@dataclass
class Story:
    items: list[Item]

    @property
    def feed_ids(self) -> set[int]:
        return {i.feed_id for i in self.items}

    @property
    def coverage(self) -> int:
        return len(self.feed_ids)

    @property
    def latest(self) -> float:
        return max(i.published for i in self.items)

    @property
    def earliest(self) -> float:
        return min(i.published for i in self.items)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _weighted_jaccard(idf: dict[str, float], a: frozenset[str], b: frozenset[str]) -> float:
    return sum(idf[t] for t in a & b) / sum(idf[t] for t in a | b)


def _weighted_cosine(idf: dict[str, float], a: frozenset[str], b: frozenset[str]) -> float:
    """Cosine similarity of IDF-weighted binary token vectors."""
    return sum(idf[t] ** 2 for t in a & b) / math.sqrt(sum(idf[t] ** 2 for t in a) * sum(idf[t] ** 2 for t in b))


# Chosen with eval_matching.py against Overworld's curated story groups (1,171 stories):
# cosine + average linkage @ 0.15 gave F1 0.83 vs 0.36 for the original jaccard + single link @ 0.40.
SIMILARITY = _weighted_cosine
DEFAULT_THRESHOLD = 0.15


def lexical_similarities(items: list[Item], min_shared: int = 2) -> dict[tuple[int, int], float]:
    """Sparse pairwise similarity {(i, j): sim} (i < j) for items sharing >= min_shared tokens."""
    for it in items:
        if not it.tokens:
            it.tokens = tokenize(it.title)
    n = len(items)

    # IDF over the window. Smoothed so a token in every doc still has weight > 0.
    df: dict[str, int] = defaultdict(int)
    for it in items:
        for t in it.tokens:
            df[t] += 1
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    # Inverted index so we only compare items that share at least one token.
    index: dict[str, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        for t in it.tokens:
            index[t].append(i)

    sims: dict[tuple[int, int], float] = {}
    for i, a in enumerate(items):
        shared_counts: dict[int, int] = defaultdict(int)
        for t in a.tokens:
            for j in index[t]:
                if j > i:
                    shared_counts[j] += 1
        for j, shared in shared_counts.items():
            if shared >= min_shared and items[j].feed_id != a.feed_id:
                sims[(i, j)] = SIMILARITY(idf, a.tokens, items[j].tokens)
    return sims


def agglomerate(items: list[Item], sims: dict[tuple[int, int], float], threshold: float,
                linkage: str = "average") -> list[Story]:
    """Merge items into stories, strongest pairs first.

    linkage="single": any one pair above threshold links two groups (prone to chaining:
    A~B and B~C pulls unrelated A and C together). linkage="average": two groups merge only if
    the mean similarity over all their cross-feed pairs is >= threshold (missing pairs count as 0).
    """
    n = len(items)
    uf = _UnionFind(n)
    members = {i: [i] for i in range(n)}
    for (i, j), s in sorted(sims.items(), key=lambda kv: -kv[1]):
        if s < threshold:
            break
        ri, rj = uf.find(i), uf.find(j)
        if ri == rj:
            continue
        if linkage == "average":
            total = count = 0
            for a in members[ri]:
                for b in members[rj]:
                    if items[a].feed_id == items[b].feed_id:
                        continue
                    count += 1
                    total += sims.get((a, b) if a < b else (b, a), 0.0)
            if not count or total / count < threshold:
                continue
        uf.union(ri, rj)  # parent becomes ri
        members[ri].extend(members.pop(rj))
    groups: dict[int, list[Item]] = defaultdict(list)
    for i, it in enumerate(items):
        groups[uf.find(i)].append(it)
    return [Story(items=g) for g in groups.values()]


def cluster(items: list[Item], threshold: float = DEFAULT_THRESHOLD, min_shared: int = 2,
            linkage: str = "average") -> list[Story]:
    """Cluster items into stories using lexical similarity. Only items from different feeds are linked."""
    if not items:
        return []
    return agglomerate(items, lexical_similarities(items, min_shared), threshold, linkage)


def representative_title(story: Story) -> str:
    """Pick the headline that shares the most tokens with the rest of the cluster."""
    if len(story.items) == 1:
        return story.items[0].title
    best, best_score = story.items[0], -1.0
    for a in story.items:
        score = sum(len(a.tokens & b.tokens) for b in story.items if b is not a)
        # Prefer shorter headlines on ties (less fluff).
        score -= len(a.title) / 10000
        if score > best_score:
            best, best_score = a, score
    return best.title
