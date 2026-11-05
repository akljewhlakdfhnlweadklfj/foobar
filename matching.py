"""Group headlines from different feeds that describe the same story.

Different outlets word the same story differently ("Fed raises rates by 0.25%"
vs "Federal Reserve hikes interest rates a quarter point"), so exact matching
is useless. Instead:

1. Normalise each item's text (its title plus the first sentence of its description, see
   item_text) into a set of content tokens (lower-case, strip punctuation, drop stopwords,
   crude stemming).
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

import bisect
import functools
import heapq
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
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec
    monday tuesday wednesday thursday friday saturday sunday today tonight yesterday tomorrow
    daily weekly week weeks month monthly
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Dotted abbreviations the tokenizer would otherwise split into single letters.
_ABBREV = {"us": "usa", "uk": "britain", "un": "unitednations", "eu": "europeanunion"}
_ABBREV_RE = re.compile(r"\b(u\.s\.?|u\.k\.?|u\.n\.?|e\.u\.?)(?=\W|$)")

# A few common headline synonyms, mapped to one canonical form.
SYNONYMS = {
    "hike": "raise", "hikes": "raise", "hiked": "raise", "raises": "raise", "raised": "raise",
    "cut": "cut", "cuts": "cut", "slashes": "cut", "slash": "cut",
    "dies": "die", "died": "die", "dead": "die", "death": "die",
    "kills": "kill", "killed": "kill", "killing": "kill",
    "wins": "win", "won": "win", "victory": "win",
    "federal": "fed",
    "usa": "usa", "america": "usa", "american": "usa", "americans": "usa",
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


@functools.lru_cache(maxsize=200_000)
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
    text = re.sub(r"\b(US|UK|UN|EU)\b", lambda m: _ABBREV[m.group(1).lower()], text)  # all-caps: the country, not "us"
    text = text.lower().replace("'s", "").replace("’s", "")
    text = _ABBREV_RE.sub(lambda m: _ABBREV[m.group(0).replace(".", "")], text)
    out = set()
    for raw in _TOKEN_RE.findall(text):
        if raw in STOPWORDS:
            continue
        tok = SYNONYMS.get(raw, raw)
        tok = _stem(tok)
        mapped = SYNONYMS.get(tok, tok)
        if mapped != tok:  # the stem matched a synonym: normalise it the same way as direct hits
            tok = _stem(mapped)
        if tok in STOPWORDS or len(tok) < 2:
            continue
        out.add(tok)
    return frozenset(out)


DEFAULT_WEIGHT = 2.0


@dataclass
class Item:
    id: int
    feed_id: int
    title: str
    link: str = ""
    published: float = 0.0  # unix timestamp
    feed_title: str = ""
    weight: float = DEFAULT_WEIGHT  # outlet prestige, 1 (small blog) .. 5 (paper of record)
    description: str = ""
    image: str = ""
    views: int | None = None  # YouTube videos: view count, when known
    kind: str = "rss"         # the source's kind (sources.KINDS): "youtube" items are videos
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
    def score(self) -> float:
        """Prestige-weighted coverage: the sum of each distinct outlet's weight."""
        return sum({i.feed_id: i.weight for i in self.items}.values())

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


# Chosen with two evaluations against Overworld's curated story groups:
# - eval_matching.py (titles only, 1,183 stories): cosine + average linkage gave F1 0.82 vs 0.36 for
#   the original jaccard + single linkage.
# - eval_realistic.py (14 days of full outlet feeds as distractors): title + the description's
#   first sentence at 0.12 gave F1 0.93 (precision 1.00) vs 0.87 for titles alone at 0.15.
DEFAULT_THRESHOLD = 0.12
# A story covers one news cycle: its first and last headline are at most this far apart. Similar
# headlines further apart (next month's Nintendo Direct, another weekend's box office) are separate stories.
MAX_SPAN_HOURS = 24.0


def _drop_feed_boilerplate(items: list[Item], min_items: int = 10, share: float = 0.4) -> None:
    """Remove tokens that appear in a large share of one feed's titles, such as the
    " - Reuters" suffix Google News adds or a "Review:" prefix. They describe the feed, not the story."""
    by_feed: dict[int, list[Item]] = defaultdict(list)
    for it in items:
        by_feed[it.feed_id].append(it)
    for its in by_feed.values():
        if len(its) < min_items:
            continue
        counts: dict[str, int] = defaultdict(int)
        for it in its:
            for t in it.tokens:
                counts[t] += 1
        boiler = {t for t, c in counts.items() if c >= share * len(its)}
        if boiler:
            for it in its:
                it.tokens = it.tokens - boiler


# What text the matcher compares: "title", "title+lead" (title + the description's first
# sentence) or "title+description". Chosen with eval_realistic.py.
TEXT_MODE = "title+lead"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def item_text(it: Item, mode: str | None = None) -> str:
    mode = mode or TEXT_MODE
    if mode == "title" or not it.description:
        return it.title
    if mode == "title+lead":
        return it.title + " " + _SENTENCE_END.split(it.description, 1)[0][:300]
    return it.title + " " + it.description


# cluster() doesn't keep pairs less similar than this (or a quarter of the threshold, if lower): they
# barely move a group's average (realistic-eval F1 unchanged; 99.5% of 30-day stories identical) and
# are most of the pairs on a busy vertical.
MIN_PAIR_SIM = 0.03


def lexical_similarities(items: list[Item], min_shared: int = 2, max_span_hours: float | None = MAX_SPAN_HOURS,
                         min_sim: float = 0.0) -> dict[tuple[int, int], float]:
    """Sparse pairwise similarity {(i, j): sim} (i < j): IDF-weighted cosine of token sets, for items
    from different feeds sharing >= min_shared tokens and published within max_span_hours of each
    other (None: any time apart), and at least min_sim similar.

    Items are swept in time order through an inverted index whose postings are trimmed to the span,
    so pairs further apart are never generated (a 30-day window costs about as much per day as a
    2-day one); shared IDF weight is summed while walking the postings, with norms precomputed."""
    for it in items:
        if not it.tokens:
            it.tokens = tokenize(item_text(it))
    _drop_feed_boilerplate(items)
    n = len(items)

    # IDF over the window. Smoothed so a token in every doc still has weight > 0.
    df: dict[str, int] = defaultdict(int)
    for it in items:
        for t in it.tokens:
            df[t] += 1
    w2 = {t: math.log((n + 1) / (c + 0.5)) ** 2 for t, c in df.items()}     # squared IDF
    norm = [math.sqrt(sum(w2[t] for t in it.tokens)) for it in items]

    span = math.inf if max_span_hours is None else max_span_hours * 3600
    order = sorted(range(n), key=lambda i: items[i].published)
    pub = [items[i].published for i in order]
    index: dict[str, list[int]] = defaultdict(list)     # token -> positions in `order`, oldest first
    sims: dict[tuple[int, int], float] = {}
    for pos, i in enumerate(order):
        a = items[i]
        cut = pub[pos] - span
        acc: dict[int, float] = {}
        cnt: dict[int, int] = {}
        for t in a.tokens:
            postings = index[t]
            if postings and pub[postings[0]] < cut:              # drop postings older than the span
                del postings[:bisect.bisect_left(postings, cut, key=lambda q: pub[q])]
            wt = w2[t]
            for q in postings:
                acc[q] = acc.get(q, 0.0) + wt
                cnt[q] = cnt.get(q, 0) + 1
            postings.append(pos)
        if not norm[i]:
            continue
        for q, c in cnt.items():
            j = order[q]
            if c >= min_shared and items[j].feed_id != a.feed_id and norm[j]:
                sim = acc[q] / (norm[i] * norm[j])
                if sim >= min_sim:
                    sims[(i, j) if i < j else (j, i)] = sim
    return sims


def agglomerate(items: list[Item], sims: dict[tuple[int, int], float], threshold: float,
                linkage: str = "average", max_span_hours: float | None = MAX_SPAN_HOURS) -> list[Story]:
    """Merge items into stories.

    linkage="average" (UPGMA): repeatedly merge the two groups with the highest *average*
    similarity over all their cross-feed item pairs (missing pairs count as 0), while that average
    is >= threshold. Running similarity sums between groups are kept up to date on each merge, so
    the cost is proportional to the number of similar pairs, not to group size squared.
    linkage="single": any one pair above threshold links two groups (prone to chaining:
    A~B and B~C pulls unrelated A and C together). Kept for comparison.
    max_span_hours: groups are only merged while their earliest and latest headline stay within it
    (None = no limit). Pairs further apart than that are never compared.
    """
    n = len(items)
    span = None if max_span_hours is None else max_span_hours * 3600

    def near(i: int, j: int) -> bool:
        return span is None or abs(items[i].published - items[j].published) <= span

    if linkage == "single":
        uf = _UnionFind(n)
        for (i, j), s in sims.items():
            if s >= threshold and near(i, j):
                uf.union(i, j)
        groups: dict[int, list[Item]] = defaultdict(list)
        for i, it in enumerate(items):
            groups[uf.find(i)].append(it)
        return [Story(items=g) for g in groups.values()]

    size = [1] * n
    feeds_in: list[dict[int, int]] = [{it.feed_id: 1} for it in items]   # feed histogram per group
    sums: list[dict[int, float]] = [dict() for _ in range(n)]            # sums[a][b] = total sim a<->b
    for (i, j), s in sims.items():
        if s > 0 and near(i, j):
            sums[i][j] = sums[i].get(j, 0.0) + s
            sums[j][i] = sums[j].get(i, 0.0) + s
    del sims                                # the caller's copy is usually the only other reference
    alive = [True] * n
    version = [0] * n
    members: list[list[int]] = [[i] for i in range(n)]
    lo = [it.published for it in items]
    hi = list(lo)

    def fits(a: int, b: int) -> bool:
        return span is None or max(hi[a], hi[b]) - min(lo[a], lo[b]) <= span

    def average(a: int, b: int) -> float:
        hist_a, hist_b = (feeds_in[a], feeds_in[b]) if len(feeds_in[a]) <= len(feeds_in[b]) else (feeds_in[b], feeds_in[a])
        same = sum(c * hist_b.get(f, 0) for f, c in hist_a.items())
        cross = size[a] * size[b] - same
        return sums[a][b] / cross if cross > 0 else 0.0

    heap = []
    for a in range(n):
        for b, total in sums[a].items():
            if a < b:
                avg = total if items[a].feed_id != items[b].feed_id else 0.0   # single items: the pair's similarity
                if avg >= threshold and fits(a, b):
                    heap.append((-avg, a, b, 0, 0))
    heapq.heapify(heap)
    while heap:
        neg, a, b, va, vb = heapq.heappop(heap)
        if not (alive[a] and alive[b]) or va != version[a] or vb != version[b]:
            continue  # stale entry
        if -neg < threshold:
            break
        if not fits(a, b):
            continue  # together they would span more than one news cycle (spans only grow: never mergeable)
        if size[a] < size[b]:
            a, b = b, a
        # Merge b into a.
        alive[b] = False
        size[a] += size[b]
        for f, c in feeds_in[b].items():
            feeds_in[a][f] = feeds_in[a].get(f, 0) + c
        members[a].extend(members[b])
        lo[a], hi[a] = min(lo[a], lo[b]), max(hi[a], hi[b])
        sums[a].pop(b, None)
        for c, total in sums[b].items():
            if c == a:
                continue
            sums[a][c] = sums[a].get(c, 0.0) + total
            sums[c][a] = sums[a][c]
            sums[c].pop(b, None)
        sums[b] = {}
        version[a] += 1
        for c, total in sums[a].items():
            if total < threshold:
                continue                       # average = total / (cross pairs >= 1) can't reach it
            avg = average(a, c)
            if avg >= threshold and fits(a, c):
                heapq.heappush(heap, (-avg, min(a, c), max(a, c),
                                      version[min(a, c)], version[max(a, c)]))
    return [Story(items=[items[i] for i in members[r]]) for r in range(n) if alive[r]]


def cluster(items: list[Item], threshold: float = DEFAULT_THRESHOLD, min_shared: int = 2,
            linkage: str = "average", max_span_hours: float | None = MAX_SPAN_HOURS) -> list[Story]:
    """Cluster items into stories using lexical similarity. Only items from different feeds are linked,
    and a story's headlines are published within max_span_hours of each other."""
    if not items:
        return []
    floor = min(MIN_PAIR_SIM, threshold / 4)
    return agglomerate(items, lexical_similarities(items, min_shared, max_span_hours, floor),
                       threshold, linkage, max_span_hours)


# Headlines labelled as a format rather than the news itself ("Opinion |", "WATCH:", "Review:").
LABELLED = re.compile(r"^\s*(opinion|op-ed|watch|video|videos|listen|podcast|live|live updates|analysis|editorial|"
                      r"explainer|review|reviews|preview|quiz|letters?|photos?|in pictures|the latest|perspective|"
                      r"column|commentary|interview|recap)\b\s*[:|\-–—]", re.I)


def pick_headline(items: list, title=lambda x: x.title, weight=lambda x: x.weight, published=lambda x: x.published):
    """The headline to show: the shortest one that isn't a labelled format piece (any headline when all
    are); ties go to the higher-weighted outlet, then the earlier one. Used per outlet (digest.py) and
    per story (groups.py)."""
    plain = [x for x in items if not LABELLED.match(title(x))] or items
    return min(plain, key=lambda x: (len(title(x)), -weight(x), published(x)))
