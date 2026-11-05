"""News aggregator: shows only stories reported by at least N of your feeds.

Stories are split into verticals (Games, Movies & TV, Books, General News). Each vertical has
its own seed list (feeds/<vertical>.txt) and its own self-contained SQLite file
(data/<vertical>.db), so a vertical can be moved by copying one file.

Run:  python3 app.py [--port 8000] [--interval 15] [--backfill DAYS]
Then open http://localhost:8000
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import feeds
import matching
import seedcache
from scraper import Scraper, _from_feedly, fetch_with_fallback  # noqa: F401 (re-exported)
from store import (DEFAULT_BONUS, MAX_BONUS, MAX_WEIGHT, RETENTION_DAYS, SCHEMA_VERSION,  # noqa: F401
                   SeedFeed, Store, read_feed_list)

ROOT = Path(__file__).parent
STATIC_DIR = ROOT / "static"
FEEDS_DIR = ROOT / "feeds"
MAX_BODY = 64 * 1024
CACHE_SECONDS = 60
MAX_PHRASE_LEN = 100


@dataclass(frozen=True)
class Vertical:
    key: str
    label: str
    default_min_feeds: int


# Order = tab order. default_min_feeds: Overworld (games) never shows a story with < 4 sources.
VERTICALS = [
    Vertical("games", "Games", 4),
    Vertical("movies", "Movies & TV", 3),
    Vertical("books", "Books", 2),
    Vertical("news", "General News", 4),
    Vertical("anime", "Anime", 3),
    Vertical("manga", "Manga", 2),
]
VERTICALS_BY_KEY = {v.key: v for v in VERTICALS}


# --- per-vertical settings -------------------------------------------------------------------

# name: (type, min, max). Stored per vertical (store.settings); query parameters override them.
SETTING_SPECS = {
    "min_feeds": (int, 1, 100),
    "hours": (float, 1, 24 * RETENTION_DAYS),
    "threshold": (float, 0.02, 1.0),
    "min_score": (float, 0, 10_000),
    "min_weight": (float, 0, MAX_WEIGHT),
    "only_watched": (bool, None, None),
    "watched_any": (bool, None, None),
    "require_all": (bool, None, None),
}


def default_settings(v: Vertical) -> dict:
    return {"min_feeds": v.default_min_feeds, "hours": 48.0, "threshold": matching.DEFAULT_THRESHOLD,
            "min_score": 0.0, "min_weight": 0.0, "only_watched": False, "watched_any": False, "require_all": False}


def coerce_setting(name: str, value, strict: bool = True):
    """Validate one setting. strict=True (JSON bodies) rejects wrong types; strict=False (query
    strings) parses text and clamps numbers into range."""
    typ, lo, hi = SETTING_SPECS[name]
    if typ is bool:
        if isinstance(value, bool):
            return value
        if not strict and isinstance(value, str) and value.lower() in ("1", "true", "0", "false"):
            return value.lower() in ("1", "true")
        raise HTTPError(400, f"{name} must be true or false")
    if isinstance(value, bool) or (strict and not isinstance(value, (int, float))):
        raise HTTPError(400, f"{name} must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{name} must be a number")
    if num != num:
        raise HTTPError(400, f"{name} must be a number")
    if not lo <= num <= hi:
        if strict:
            raise HTTPError(400, f"{name} must be between {lo:g} and {hi:g}")
        num = min(hi, max(lo, num))
    return int(num) if typ is int else num


def effective_settings(v: Vertical, store: Store, overrides: dict | None = None) -> dict:
    """Defaults, then the vertical's saved settings, then any query-string overrides."""
    out = default_settings(v)
    for k, val in store.get_settings().items():
        if k in SETTING_SPECS:
            try:
                out[k] = coerce_setting(k, val)
            except HTTPError:
                pass  # ignore a bad stored value
    for k, val in (overrides or {}).items():
        if k in SETTING_SPECS:
            out[k] = coerce_setting(k, val, strict=False)
    return out


# --- stories ---------------------------------------------------------------------------------

def phrase_regex(phrase: str) -> re.Pattern:
    """Case-insensitive whole-word match; any run of whitespace between words."""
    words = phrase.split()
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)", re.I)


def _snippet(text: str, m: re.Match, before: int = 70, after: int = 110) -> str:
    """The part of a description around a phrase match, cut on word boundaries."""
    start, end = max(0, m.start() - before), min(len(text), m.end() + after)
    if start > 0:
        start = text.find(" ", start) + 1 or start
    if end < len(text):
        end = text.rfind(" ", m.end(), end) if text.rfind(" ", m.end(), end) > 0 else end
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


_cache: dict[tuple, tuple] = {}
_cache_lock = threading.Lock()


def _clusters(store: Store, hours: float, threshold: float) -> tuple[list[matching.Story], int]:
    """Clustered stories for the window, cached until the data changes or CACHE_SECONDS pass."""
    key = (store.path, hours, threshold)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == store.version and time.time() - hit[1] < CACHE_SECONDS:
            return hit[2], hit[3]
    items = store.items_since(time.time() - hours * 3600)
    stories = matching.cluster(items, threshold=threshold)
    with _cache_lock:
        _cache[key] = (store.version, time.time(), stories, len(items))
    return stories, len(items)


MIN_HEADLINE_WORDS = 4
# Headlines labelled as a format rather than the news itself.
_LABELLED = re.compile(r"^\s*(opinion|op-ed|watch|video|videos|listen|podcast|live|live updates|analysis|editorial|"
                       r"explainer|review|reviews|preview|quiz|letters?|photos?|in pictures|the latest|perspective|"
                       r"column|commentary|interview|recap)\b\s*[:|\-–—]", re.I)


def headline_item(story: matching.Story, min_weight: float = 0) -> matching.Item:
    """The headline a story is shown with: the shortest one among its counted outlets.
    Fragments under MIN_HEADLINE_WORDS words ("Sweet Pea", "Review") are skipped when a fuller
    headline exists; ties go to the higher-weighted outlet."""
    pool = [i for i in story.items if i.weight >= min_weight] or story.items
    full = [i for i in pool if len(i.title.split()) >= MIN_HEADLINE_WORDS] or pool
    plain = [i for i in full if not _LABELLED.match(i.title)] or full  # skip "Opinion | ...", "WATCH: ..."
    return min(plain, key=lambda i: (len(i.title), -i.weight, i.published))


def build_stories(store: Store, min_feeds: int, hours: float, threshold: float, min_score: float = 0,
                  only_watched: bool = False, watched_any_coverage: bool = False,
                  require_all: bool = False, min_weight: float = 0) -> dict:
    """Stories for the API.

    score = prestige (sum of distinct outlets' weights) + bonus of every watched phrase found in
    any of the story's titles or descriptions. only_watched keeps just stories with a watched
    phrase; watched_any_coverage shows watched stories even below min_feeds / min_score.
    Required outlets (feeds.required): when any are set, a story must include at least one of
    them (require_all: every one of them). This applies to every story, watched ones included.
    min_weight: outlets weighted below it still help group headlines and are listed, but don't count
    toward coverage (the "reported by N" number) or the prestige score.
    """
    stories, considered = _clusters(store, hours, threshold)
    phrases = [(p, phrase_regex(p["phrase"])) for p in store.list_phrases()]
    required = store.required_feed_ids()
    out = []
    for s in stories:
        if required:
            have = s.feed_ids & required
            if not have or (require_all and have != required):
                continue
        hits, watched_items, snippets = [], set(), {}
        for p, rx in phrases:
            found = []
            for i in s.items:
                if rx.search(i.title):
                    found.append(i.id)
                elif m := rx.search(i.description):
                    found.append(i.id)
                    snippets.setdefault(i.id, _snippet(i.description, m))
            if found:
                hits.append(p)
                watched_items.update(found)
        if only_watched and not hits:
            continue
        counted = {i.feed_id: i.weight for i in s.items if i.weight >= min_weight}
        coverage, prestige = len(counted), sum(counted.values())
        if not counted:
            continue
        bonus = sum(p["bonus"] for p in hits)
        score = prestige + bonus
        if not (hits and watched_any_coverage) and (coverage < min_feeds or score < min_score):
            continue
        # One entry per feed (a feed may have several near-duplicate items; keep the newest).
        per_feed: dict[int, matching.Item] = {}
        for it in sorted(s.items, key=lambda i: i.published):
            per_feed[it.feed_id] = it
        lead = headline_item(s, min_weight)
        per_feed[lead.feed_id] = lead  # show the chosen headline for its outlet, not that outlet's newest item
        best = sorted(s.items, key=lambda i: (-i.weight, -i.published))
        description = lead.description or next((i.description for i in best if i.description), "")
        image = lead.image or next((i.image for i in best if i.image), "")
        out.append({
            "title": lead.title,
            "lead": {"feed": lead.feed_title, "link": lead.link, "weight": lead.weight,
                     "description": description[:400], "image": image},
            "coverage": coverage,
            "base_score": prestige,
            "outlets": s.coverage,  # including outlets below min_weight
            "bonus": bonus,
            "score": score,
            "watched": [{"id": p["id"], "phrase": p["phrase"], "bonus": p["bonus"]} for p in hits],
            "first_seen": s.earliest,
            "latest": s.latest,
            "sources": [
                {"feed": it.feed_title, "title": it.title, "link": it.link, "published": it.published,
                 "weight": it.weight, "watched": it.id in watched_items, "snippet": snippets.get(it.id, ""),
                 "image": it.image,
                 "counted": it.weight >= min_weight}
                for it in sorted(per_feed.values(), key=lambda i: (-i.weight, i.published))
            ],
        })
    out.sort(key=lambda s: (-s["score"], -s["coverage"], -s["latest"]))
    return {"stories": out, "items_considered": considered, "terms": story_terms(out)}


# --- prominent terms (word cloud) ---------------------------------------------------------------

# Headline filler that would otherwise dominate the cloud without saying what the news is about.
GENERIC_TERMS = frozenset("""
    trailer trailers review reviews release released releases releasing date dates first season seasons
    episode episodes game games gameplay update updates launch launches launched launching coming comes
    year years time times day days week weeks month best top official officially full get gets got getting
    now here look looks looking set sets make makes making made take takes taking back big one two three
    part story stories show shows showing star stars film films movie movies book books series anime manga
    announce announces announced announcement reveal reveals revealed confirm confirms confirmed
    want wants need needs could would might may will just like also still even every way ways thing things
    people fans fan report reports reported reportedly rumor rumors leak leaks leaked deal deals sale sales
    free price prices next last end ends final finally after before start starts starting open opens
    win wins won world know says said tell tells talk talks call calls go goes going see seen watch new
    major huge massive including includes include available live early late latest today tonight
    against during off long short high low good great bad better worse every many much more most
""".split())
_GENERIC_STEMS = frozenset(matching._stem(w) for w in GENERIC_TERMS)
MIN_SHARE = 0.4
MAX_RUN_WORDS = 4
STORY_KEYWORDS = 6
NAME_BOOST, RUN_BOOST = 1.5, 1.25   # ranking boost for names / multi-word runs over single words

# Headline verbs and adjectives that shouldn't lead a story ("delayed", "blames", "Black", "record").
# No part-of-speech tagger in the standard library, so: this list plus verb/adjective endings.
NOT_NOUNS = frozenset(matching._stem(w) for w in """
    blame blames win wins won lose loses lost beat beats join joins reunite reunites slam slams sue sues hit
    cut cuts raise raises quit quits fire fires fired claim claims warn warns vow vows urge urges push pushes
    plan plans seek seeks eye eyes pull pulls drop drops add adds ban bans block blocks kill kills die dies
    sign signs buy buys sell sells lead leads name names return returns leave leaves face faces take takes
    make makes give gives help helps hire hires lay lays boost boosts slash slashes hike hikes flip flips
    sweep sweeps defeat defeats oust ousts elect elects pass passes reject rejects approve approves stop stops
    exit exits break breaks hold holds keep keeps miss misses rise rises fall falls soar soars sink sinks
    jump jumps climb climbs top tops tease teases debut debuts premiere premieres stream streams air airs drop
    black white red blue green new old big small first last final major minor record female male young
    former latest early late dark bright huge massive key main real true false full free live dead open close
    official top best worst little great high low long short fresh rare global local national federal
    possible likely unlikely second third
""".split())
_NOT_NOUN_ENDINGS = ("ed", "ing", "ly", "ous", "ive", "able", "ible", "ful", "less", "ish")


def _noun_like(key: tuple, display: str, proper_evidence: bool) -> bool:
    """Rough noun test for a keyword (see NOT_NOUNS)."""
    if key[0] in ("name", "pair"):
        return True                                # names and multi-word phrases
    word = display.lower()
    if key[0] in NOT_NOUNS or matching._stem(word) in NOT_NOUNS:
        return False                               # checked first: "Black women" capitalises an adjective
    if proper_evidence or display.isupper() or any(c.isdigit() for c in display):
        return True                                # a capitalised name, acronym or model ("Ubisoft", "PS5")
    return not word.endswith(_NOT_NOUN_ENDINGS)
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’+\-]*")


def _term_key(word: str) -> str | None:
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    w = w.strip("'-")
    if len(w) < 3 or w.isdigit() or w in matching.STOPWORDS or w in GENERIC_TERMS:
        return None
    mapped = matching.SYNONYMS.get(w, w)
    if mapped in GENERIC_TERMS:                   # "unveils" -> "announce"
        return None
    key = matching._stem(mapped)
    return None if key in GENERIC_TERMS or key in matching.STOPWORDS or key in _GENERIC_STEMS else key


# Small words allowed inside a name ("God of War", "Lord of the Rings", "Dungeons & Dragons").
NAME_CONNECTORS = frozenset({"of", "the", "&", "de", "la", "le", "du", "del", "von", "van", "vs"})
# Punctuation that ends a name: phrases never span "Zelda: Ocarina" or "Switch 2, PS5".
_SEGMENT_RE = re.compile(r"[:|;,!?()\[\]{}\"“”«»]|\s[-–—]\s|\.\s|(?<!\w)[‘’']|[‘’'](?!\w)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’+\-.]*[A-Za-z0-9+]|[A-Za-z0-9]|&")
MAX_NAME_WORDS = 6


def _strip_possessive(w: str) -> str:
    return w[:-2] if w.lower().endswith(("'s", "’s")) else w


def _is_cap(w: str) -> bool:
    """Looks like part of a name: Capitalised, ALLCAPS, or contains a digit ("6", "PS5", "VI")."""
    return w[:1].isupper() or any(c.isdigit() for c in w)


def _name_candidates(title: str) -> dict[tuple, str]:
    """Proper-noun phrases in a headline: runs of capitalised words/numbers, possibly with connectors
    inside ("God of War Laufey", "South of Midnight", "Grand Theft Auto 6", "Xbox Series X").
    Returns {lowercased word tuple: surface text}.

    In Title Case headlines every word is capitalised, so a run only counts there if it has a
    connector, a number or an acronym in it; plain capitalised pairs ("File Size") need a
    sentence-case headline, where capitals actually mark names. Pairs like "Elder Scrolls" are also
    found by the adjacent-pair stage."""
    words_all = _WORD_RE.findall(title)
    caps_all = sum(w[0].isupper() for w in words_all)
    title_case = len(words_all) > 3 and caps_all / len(words_all) >= 0.6
    out = {}
    for segment in _SEGMENT_RE.split(title):
        raw = _TOKEN_RE.findall(segment)
        words = [_strip_possessive(w) for w in raw]
        n = len(words)
        for i in range(n):
            first = words[i]
            if not _is_cap(first) or first.lower() in NAME_CONNECTORS or first.lower() in matching.STOPWORDS \
                    or first.isdigit():
                continue
            for j in range(i + 1, min(n, i + MAX_NAME_WORDS)):
                w = words[j]
                if w.lower() in NAME_CONNECTORS:
                    continue                      # connector: allowed inside, never at the end
                if not _is_cap(w):
                    break                         # an ordinary lowercase word ends the name
                span = words[i:j + 1]
                names = [x for x in span if x.lower() not in NAME_CONNECTORS]
                has_conn = len(names) < len(span)
                special = any(any(c.isdigit() for c in x) or (x.isupper() and x not in ("A", "I")) for x in names)
                last = span[-1]
                if len(names) < 2:
                    continue
                if not (_term_key(last) or any(c.isdigit() for c in last) or last.isupper() or has_conn):
                    continue                      # don't end on filler ("Switch Official"), unless clearly a name ("Ocarina of Time")
                if not (_term_key(first) or has_conn or special or first.isupper()):
                    continue                      # don't start on filler, unless it's clearly a name ("Call of Duty")
                if not any(_term_key(x) or any(c.isdigit() for c in x) or x.isupper() for x in names):
                    continue
                if title_case and not has_conn:
                    # Every word is capitalised here, so only trust runs anchored by a number/acronym
                    # at an end ("GTA 6", "Xbox Series X"), not whole headlines ("Presses Back AI Service").
                    is_special = lambda x: any(c.isdigit() for c in x) or (x.isupper() and x not in ("A", "I"))
                    if not (is_special(names[0]) or is_special(names[-1])) or len(span) > 4:
                        continue
                # Key without possessives (so "Dragon's Dogma" = "Dragon Dogma"); display keeps an inner
                # possessive ("Dragon's Dogma 2") but drops a trailing one ("Nintendo's" -> "Nintendo").
                shown = raw[i:j] + [words[j]]
                out[tuple(x.lower() for x in span)] = " ".join(shown)
    return out


def _display_name(forms: Counter) -> str:
    """Most common surface form, with connectors lowercased after the first word."""
    words = forms.most_common(1)[0][0].split()
    return " ".join(w if i == 0 or w.lower() not in NAME_CONNECTORS else w.lower() for i, w in enumerate(words))


def _contains(longer: tuple, shorter: tuple) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


def story_terms(stories: list[dict], limit: int = 40) -> list[dict]:
    """The most prominent names, phrases and words in these stories' headlines.

    Three kinds of term:
      - names: proper-noun phrases with connectors ("God of War Laufey", "Grand Theft Auto 6");
      - pairs: two adjacent words that recur together ("box office", "Elder Scrolls");
      - single words.
    A term counts for a story only if it's in at least MIN_SHARE of that story's headlines (what the
    story is about, not one outlet's wording), and adds score x share. Longer terms replace the
    shorter ones they contain when they account for most of their uses. Returns
    [{"term", "weight", "stories", "forms"}], heaviest first, and sets each story's "keywords":
    up to STORY_KEYWORDS of its own terms, most prominent first (share x distinctiveness).
    """
    uni_w, uni_n = defaultdict(float), defaultdict(int)
    multi_w, multi_n = defaultdict(float), defaultdict(int)   # names and pairs, keyed by tuple
    forms: dict = defaultdict(Counter)            # single-word key -> surface forms
    multi_forms: dict = defaultdict(Counter)      # multi-word key -> surface forms
    name_best_share: dict = defaultdict(float)    # multi-word key -> highest share in any one story
    proper: dict = defaultdict(int)               # key -> capitalised mid-headline in sentence-case headlines
    plain: dict = defaultdict(int)                # key -> lowercase mid-headline in sentence-case headlines
    per_story = []                                # (story, {key: share}, {multi key: share})

    for s in stories:
        titles = {x["title"] for x in s["sources"]} | {s["title"]}
        cnt_u, cnt_m = Counter(), Counter()
        for title in titles:
            seen_u, seen_m = set(), {}
            words = _WORD_RE.findall(title)
            caps = sum(w[0].isupper() for w in words)
            sentence_case = len(words) > 3 and caps / len(words) < 0.6
            keys = []
            for i, w in enumerate(words):
                k = _term_key(w)
                keys.append(k)
                if not k:
                    continue
                forms[k][_strip_possessive(w).strip("'’-")] += 1
                if sentence_case and i > 0:
                    proper[k] += 1 if w[0].isupper() else 0
                    plain[k] += 0 if w[0].isupper() else 1
                seen_u.add(k)
            # Runs of 2-4 adjacent content words inside one punctuation segment ("box office",
            # "Grand Theft Auto", "Otaku Vampire Love Bite").
            for seg in _SEGMENT_RE.split(title):
                seg_words = _WORD_RE.findall(seg)
                seg_keys = [_term_key(w) for w in seg_words]
                for w, k in zip(seg_words, seg_keys):
                    if k and not forms[k]:
                        forms[k][_strip_possessive(w).strip("'’-")] += 1
                for i in range(len(seg_keys)):
                    for j in range(i + 2, min(len(seg_keys), i + MAX_RUN_WORDS) + 1):
                        run = seg_keys[i:j]
                        if None in run:
                            break
                        if len(set(run)) == len(run):
                            key = ("pair",) + tuple(run)
                            seen_m.setdefault(key, None)
                            surface = seg_words[i:j - 1] + [_strip_possessive(seg_words[j - 1])]
                            multi_forms[key][" ".join(x.strip("-") for x in surface)] += 1
            for key, text in _name_candidates(title).items():
                seen_m[("name",) + key] = text
                multi_forms[("name",) + key][text] += 1
            cnt_u.update(seen_u)
            cnt_m.update(seen_m.keys())
        need = max(1, MIN_SHARE * len(titles))
        own_u = {k: c / len(titles) for k, c in cnt_u.items() if c >= need}
        own_m = {k: c / len(titles) for k, c in cnt_m.items() if c >= need}
        # Within the story: drop a name contained in a longer name that has most of its uses.
        for kind in ("name", "pair"):
            group = sorted((k for k in own_m if k[0] == kind), key=len, reverse=True)
            for short in group:
                if any(len(long) > len(short) and _contains(long[1:], short[1:]) and own_m[long] >= 0.8 * own_m[short]
                       for long in group if long in own_m):
                    own_m.pop(short, None)
        for k, share in own_u.items():
            uni_w[k] += s["score"] * share; uni_n[k] += 1
        for k, share in own_m.items():
            multi_w[k] += s["score"] * share; multi_n[k] += 1
            name_best_share[k] = max(name_best_share[k], share)
        per_story.append((s, own_u, own_m))

    def display(k: str) -> str:
        """Names, acronyms and model names keep their capitals; ordinary words are lowercased.
        A word is a name when it's mostly capitalised mid-headline in sentence-case headlines; with no
        such headlines to go by, it keeps its most common form."""
        if not forms[k]:
            return k
        form = forms[k].most_common(1)[0][0]
        caps_ = Counter({f: c for f, c in forms[k].items() if f[:1].isupper()})
        if (len(form) > 1 and form.isupper()) or any(c.isdigit() for c in form):
            return form
        if proper[k] + plain[k] == 0:
            return caps_.most_common(1)[0][0] if caps_ else form
        return caps_.most_common(1)[0][0] if caps_ and proper[k] >= plain[k] else form.lower()

    def multi_display(k: tuple) -> str:
        if k[0] == "name":
            return _display_name(multi_forms[k])
        # A run keeps its headline spelling ("Dragon's Dogma"), with ordinary words lowercased.
        if multi_forms[k]:
            words = multi_forms[k].most_common(1)[0][0].split()
            if len(words) == len(k) - 1:
                return " ".join(w if display(key)[:1].isupper() or w.isupper() else w.lower()
                                for w, key in zip(words, k[1:]))
        return " ".join(display(w) for w in k[1:])

    def word_keys(k: tuple) -> set:
        """Single-word keys inside a multi-word term."""
        return {wk for w in k[1:] for wk in [_term_key(w) if k[0] == "name" else w] if wk}

    def multi_forms_list(k: tuple) -> list:
        if k[0] == "name":
            return sorted(multi_forms[k])[:6]
        return sorted({f for w in k[1:] for f in forms[w]})[:6]

    # --- the cloud: multi-word terms first, then single words they don't absorb ---
    terms = {}
    # Names may come from a single story when they define it (in at least half its headlines);
    # other multi-word runs need 2+ stories.
    multi = [k for k, n in multi_n.items()
             if (k[0] == "name" and (n >= 2 or name_best_share[k] >= 0.5))
             or (k[0] == "pair" and n >= 2 and n >= 0.6 * min(uni_n[w] for w in k[1:]))]
    multi.sort(key=lambda k: (-(len(k) - 1), -multi_w[k]))          # longest first
    kept = []
    for k in multi:
        if any(_contains(o[1:], k[1:]) and multi_n[o] >= 0.8 * multi_n[k] for o in kept if o[0] == "name" or k[0] == "pair"):
            continue
        # A pair inside a kept name ("war laufey" in "god of war laufey") is redundant.
        if k[0] == "pair" and any(word_keys(k) <= word_keys(o) and multi_n[o] >= 0.8 * multi_n[k] for o in kept):
            continue
        kept.append(k)
        terms[k] = {"term": multi_display(k), "weight": multi_w[k], "stories": multi_n[k], "forms": multi_forms_list(k)}
    absorbed = Counter()
    for k in kept:
        for wk in word_keys(k):
            absorbed[wk] += multi_n[k]            # "War" counted inside "God of War Laufey", "Call of Duty: ...War..."
    for k, n in uni_n.items():
        if n >= 2 and n > 1.25 * absorbed.get(k, 0):
            terms[k] = {"term": display(k), "weight": uni_w[k], "stories": n, "forms": sorted(forms[k])[:6]}

    # --- per-story keywords ---
    n_stories = max(1, len(stories))
    for s, own_u, own_m in per_story:
        cands, used = [], set()
        chosen: list[tuple] = []                  # (wks, share, cand index, is_name)
        for k, share in sorted(own_m.items(), key=lambda kv: (-kv[1], -(len(kv[0]) - 1))):
            wks = word_keys(k)
            if not wks or any(wks <= c[0] for c in chosen):
                continue                          # "Elder Scrolls VI" after "Elder Scrolls 6"; "GTA" after "GTA 6"
            if k[0] == "pair" and share < 0.8 * max(own_u.get(w, 0) for w in wks):
                continue
            if k[0] == "pair" and any(c[3] and wks & c[0] for c in chosen):
                continue                          # "Midnight director" overlaps the name "South of Midnight"
            # A longer name replaces shorter chosen terms it contains ("War Laufey" -> "God of War Laufey")
            # when it covers most of their headlines.
            replaced = [c for c in chosen if c[0] < wks and share >= 0.75 * c[1]]
            for c in replaced:
                chosen.remove(c)
                cands[c[2]] = None
            chosen.append((wks, share, len(cands), k[0] == "name"))
            boost = NAME_BOOST if k[0] == "name" else RUN_BOOST    # what a story is about is usually a name
            cands.append((boost * share * math.log(1 + n_stories / max(1, multi_n[k])), multi_display(k), multi_forms_list(k), k))
            used |= wks                           # "Johnson" is covered by "Mike Johnson"
        cands = [c for c in cands if c is not None]
        for k, share in own_u.items():
            if k not in used:
                cands.append((share * math.log(1 + n_stories / max(1, uni_n[k])), display(k), sorted(forms[k])[:6], (k,)))
        cands.sort(key=lambda c: -c[0])
        # The lead keyword is the most prominent noun-like term; the rest keep their order.
        lead = next((c for c in cands if _noun_like(c[3], c[1], c[3][0] not in ("name", "pair") and proper[c[3][0]] > plain[c[3][0]])), None)
        if lead is not None:
            cands.remove(lead)
            cands.insert(0, lead)
        top = cands[:STORY_KEYWORDS]
        best = max((c[0] for c in top), default=1) or 1
        s["keywords"] = [{"term": t, "weight": round(w / best, 3), "forms": f} for w, t, f, _ in top] if top else []

    out = sorted(terms.values(), key=lambda t: -t["weight"])[:limit]
    for t in out:
        t["weight"] = round(t["weight"], 2)
    return out


# --- HTTP ------------------------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")
DEFAULT_ALLOWED_SUFFIXES = (".app.github.dev", ".githubpreview.dev")


@dataclass
class Security:
    """Protection for requests that change things (POST/PATCH/DELETE).

    - Without a valid API token: the body must be JSON (blocks cross-site "simple" form/text posts),
      a browser Origin header must match the Host, and the Host must be allowed (blocks DNS
      rebinding). Allowed hosts: localhost, Codespaces forwarding domains, and --allow-host values
      ("*" allows any host).
    - With "Authorization: Bearer <token>" matching --api-token: allowed from anywhere, for
      external orchestrators.
    """
    api_token: str | None = None
    allowed_hosts: tuple[str, ...] = ()

    def host_allowed(self, host: str) -> bool:
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        name = name.lower()
        if "*" in self.allowed_hosts:
            return True
        return (name in DEFAULT_ALLOWED_HOSTS or name in self.allowed_hosts
                or any(name.endswith(s) for s in DEFAULT_ALLOWED_SUFFIXES))

    def token_ok(self, header: str | None) -> bool:
        if not self.api_token or not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.api_token)


def make_handler(stores: dict[str, Store], scraper: Scraper, security: Security | None = None):
    security = security or Security()
    seen_hosts: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        server_version = "NewsAggregator/3.0"
        timeout = 30  # seconds a client may take to send its request

        def log_message(self, fmt, *args):  # quieter logs
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            raw_len = self.headers.get("Content-Length")
            if not raw_len:
                return {}
            try:
                n = int(raw_len)
            except ValueError:
                raise HTTPError(400, "Invalid Content-Length")
            if n < 0:
                raise HTTPError(400, "Invalid Content-Length")
            if n > MAX_BODY:
                raise HTTPError(413, f"Request body larger than {MAX_BODY // 1024} KB")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPError(400, "Body must be JSON")
            if not isinstance(data, dict):
                raise HTTPError(400, "Body must be a JSON object")
            return data

        def _note_host(self):
            host = self.headers.get("Host", "")
            if host not in seen_hosts and len(seen_hosts) < 50:
                seen_hosts.add(host)
                print(f"[http] first request for Host: {host!r}", file=sys.stderr, flush=True)

        def _guard_write(self):
            """Raise HTTPError unless this state-changing request is allowed."""
            if security.token_ok(self.headers.get("Authorization")):
                return
            host = self.headers.get("Host", "")
            if not security.host_allowed(host):
                raise HTTPError(403, f"Changes aren't allowed through host {host!r}. Start the app with "
                                     f"--allow-host {host.rsplit(':', 1)[0]} (or use an API token).")
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != host.lower():
                raise HTTPError(403, "Cross-site request refused")
            # Browsers can send a cross-site POST without asking first (a "simple" request), but only
            # with a form or text content type; requiring JSON blocks that. PATCH/DELETE always need a
            # CORS preflight, which this server never grants, so they don't need the check.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.command == "POST" and ctype != "application/json":
                raise HTTPError(415, "Send changes as Content-Type: application/json")

        def _vertical_key(self) -> str:
            return urlparse(self.path).path.strip("/").split("/")[1]

        def _route(self) -> tuple[list[str], dict, Store | None]:
            """/api/<vertical>/<rest...> -> (rest parts, query, store)."""
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            parts = u.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "api" and parts[1] in stores:
                return parts[2:], q, stores[parts[1]]
            return parts, q, None

        def _handle(self, method: str):
            self._note_host()
            try:
                if method != "GET":
                    self._guard_write()
                getattr(self, "_" + method)()
            except HTTPError as e:
                self._json({"error": str(e)}, e.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - answer instead of dropping the connection
                print(f"[http] {method} {self.path}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                self._json({"error": "Internal error"}, 500)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        # --- GET ---

        def _GET(self):
            rest, q, store = self._route()
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                body = (STATIC_DIR / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/verticals":
                self._json({
                    "verticals": [{"key": v.key, "label": v.label, "default_min_feeds": v.default_min_feeds}
                                  for v in VERTICALS if v.key in stores],
                    "default_threshold": matching.DEFAULT_THRESHOLD,
                    "default_weight": matching.DEFAULT_WEIGHT,
                    "max_weight": MAX_WEIGHT,
                    "default_bonus": DEFAULT_BONUS,
                    "max_bonus": MAX_BONUS,
                })
            elif path == "/api/scrape/status":
                self._json(scraper.status())
            elif store and rest == ["feeds"]:
                st = scraper.status()
                last = st["last_run"] or {}
                self._json({"feeds": store.list_feeds(), "refreshing": st["state"] != "idle",
                            "last_run": last.get("finished_at"), "scrape": st})
            elif store and rest == ["phrases"]:
                self._json({"phrases": store.list_phrases()})
            elif store and rest == ["settings"]:
                v = VERTICALS_BY_KEY[self._vertical_key()]
                self._json({"settings": effective_settings(v, store), "defaults": default_settings(v)})
            elif store and rest == ["stories"]:
                v = VERTICALS_BY_KEY[self._vertical_key()]
                cfg = effective_settings(v, store, {k: val for k, val in q.items() if val != ""})
                res = build_stories(store, cfg["min_feeds"], cfg["hours"], cfg["threshold"], cfg["min_score"],
                                    only_watched=cfg["only_watched"], watched_any_coverage=cfg["watched_any"],
                                    require_all=cfg["require_all"], min_weight=cfg["min_weight"])
                self._json({**res, "settings": cfg})
            else:
                raise HTTPError(404, "Not found")

        # --- POST ---

        def _POST(self):
            rest, _, store = self._route()
            path = urlparse(self.path).path
            body = self._body()
            if path in ("/api/scrape/start", "/api/refresh"):
                kind = body.get("kind", "refresh")
                days = body.get("days")
                verticals = body.get("verticals")
                if verticals is not None and (not isinstance(verticals, list)
                                              or not all(isinstance(v, str) for v in verticals)):
                    raise HTTPError(400, "verticals must be a list of vertical keys")
                if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float))):
                    raise HTTPError(400, "days must be a number")
                ok, res = scraper.start(kind, verticals, days, resume=body.get("resume", True) is not False)
                if not ok:
                    raise HTTPError(409 if "already running" in res else 400, res)
                return self._json(res, 202)
            if path == "/api/scrape/stop":
                ok, res = scraper.stop()
                return self._json(res if ok else {**scraper.status(), "message": res}, 202 if ok else 200)
            if path == "/api/scrape/schedule":
                auto, interval = body.get("auto"), body.get("interval_minutes")
                if auto is not None and not isinstance(auto, bool):
                    raise HTTPError(400, "auto must be true or false")
                if interval is not None and (isinstance(interval, bool) or not isinstance(interval, (int, float))
                                             or not 1 <= interval <= 24 * 60):
                    raise HTTPError(400, "interval_minutes must be between 1 and 1440")
                return self._json(scraper.set_schedule(auto, interval))
            if store and rest == ["feeds"]:
                url = str(body.get("url", "")).strip()
                if not url.lower().startswith(("http://", "https://")):
                    raise HTTPError(400, "URL must start with http:// or https://")
                try:
                    parsed, via, note = fetch_with_fallback(url)  # validate before saving
                except feeds.FeedError as e:
                    raise HTTPError(400, str(e))
                try:
                    feed_id = store.add_feed(url)
                except sqlite3.IntegrityError:
                    raise HTTPError(409, "That feed is already added")
                store.save_fetch(feed_id, parsed, note, via)
                return self._json({"feed": store.get_feed(feed_id)}, 201)
            if store and rest == ["phrases"]:
                phrase = " ".join(str(body.get("phrase", "")).split())
                if not phrase or len(phrase) > MAX_PHRASE_LEN:
                    raise HTTPError(400, f"phrase must be 1 to {MAX_PHRASE_LEN} characters")
                bonus = self._number(body.get("bonus", DEFAULT_BONUS), "bonus", 0, MAX_BONUS)
                try:
                    pid = store.add_phrase(phrase, bonus)
                except sqlite3.IntegrityError:
                    raise HTTPError(409, "That phrase is already watched")
                return self._json({"phrase": store.get_phrase(pid)}, 201)
            raise HTTPError(404, "Not found")

        @staticmethod
        def _number(value, name: str, lo: float, hi: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
                raise HTTPError(400, f"{name} must be a number")
            if not lo <= value <= hi:
                raise HTTPError(400, f"{name} must be between {lo:g} and {hi:g}")
            return float(value)

        # --- PATCH ---

        def _PATCH(self):
            """/api/<v>/feeds/<id> {"weight": 0..10, "enabled": bool}  - change one feed.
            /api/<v>/feeds/<id> {"required": bool}                     - stories must include this outlet.
            /api/<v>/feeds/<id> {"only": "manga, light novel"}         - keyword filter ("" clears).
            /api/<v>/feeds {"enabled": bool} / {"required": false}     - all feeds on/off; clear required.
            /api/<v>/phrases/<id> {"bonus": 0..100}                    - change a phrase's bonus."""
            rest, _, store = self._route()
            body = self._body()
            if store and rest == ["settings"]:
                if body.get("reset") is True:
                    store.reset_settings()
                else:
                    unknown = [k for k in body if k not in SETTING_SPECS]
                    if unknown:
                        raise HTTPError(400, f"unknown setting(s): {', '.join(unknown)}")
                    store.set_settings({k: coerce_setting(k, val) for k, val in body.items()})
                v = VERTICALS_BY_KEY[self._vertical_key()]
                return self._json({"settings": effective_settings(v, store), "defaults": default_settings(v)})
            if store and len(rest) == 2 and rest[0] == "phrases" and rest[1].isdigit():
                bonus = self._number(body.get("bonus"), "bonus", 0, MAX_BONUS)
                if store.update_phrase(int(rest[1]), bonus):
                    return self._json({"phrase": store.get_phrase(int(rest[1]))})
                raise HTTPError(404, "Not found")
            enabled, required, only = body.get("enabled"), body.get("required"), body.get("only")
            if only is not None and (not isinstance(only, str) or len(only) > 500):
                raise HTTPError(400, "only must be text: comma-separated keywords (empty to clear)")
            for name, val in (("enabled", enabled), ("required", required)):
                if val is not None and not isinstance(val, bool):
                    raise HTTPError(400, f"{name} must be true or false")
            if store and rest == ["feeds"]:
                if enabled is None and required is not False:
                    raise HTTPError(400, 'send {"enabled": true|false} or {"required": false}')
                updated = store.set_all_enabled(enabled) if enabled is not None else 0
                if required is False:
                    updated = max(updated, store.clear_required())
                return self._json({"updated": updated})
            if store and len(rest) == 2 and rest[0] == "feeds" and rest[1].isdigit():
                weight = self._number(body["weight"], "weight", 0, MAX_WEIGHT) if "weight" in body else None
                if weight is None and enabled is None and required is None and only is None:
                    raise HTTPError(400, "send weight, enabled, required and/or only")
                if store.update_feed(int(rest[1]), weight=weight, enabled=enabled, required=required, only=only):
                    return self._json({"feed": store.get_feed(int(rest[1]))})
            raise HTTPError(404, "Not found")

        # --- DELETE ---

        def _DELETE(self):
            rest, _, store = self._route()
            if store and len(rest) == 2 and rest[1].isdigit():
                if rest[0] == "feeds" and store.delete_feed(int(rest[1])):
                    return self._json({"ok": True})
                if rest[0] == "phrases" and store.delete_phrase(int(rest[1])):
                    return self._json({"ok": True})
            raise HTTPError(404, "Not found")

    return Handler


# --- setup -----------------------------------------------------------------------------------

def open_stores(data_dir: Path, seed: bool = True) -> dict[str, Store]:
    """One Store per vertical. A brand-new database is seeded from feeds/<key>.txt; after that,
    seed files only update display names (and weights of feeds that have none). A vertical with no
    headlines yet loads seed_cache/<key>.jsonl.gz if there is one (see seedcache.py)."""
    stores = {}
    for v in VERTICALS:
        store = Store(data_dir / f"{v.key}.db")
        seed_file = FEEDS_DIR / f"{v.key}.txt"
        if seed and seed_file.exists():
            seed_feeds = read_feed_list(seed_file)
            if store.is_new:
                n = store.add_feeds(seed_feeds)
                print(f"Seeded {v.label}: {n} feeds from {seed_file.relative_to(ROOT)}")
            store.sync_seed(seed_feeds)
            cache = seedcache.cache_path(v.key)
            if cache.exists() and seedcache.store_is_empty(store):
                n, touched = seedcache.load(store, cache)
                print(f"Loaded {v.label}: {n} cached headlines for {touched} feeds from {cache.relative_to(ROOT)}")
        stores[v.key] = store
    return stores


def backfill(stores: dict[str, Store], days: float, scraper: Scraper | None = None) -> dict:
    """Run a backfill to completion (or until stopped/rate-limited), printing progress."""
    scraper = scraper or Scraper(stores, auto=False, log=print)
    print(f"Backfilling {days:g} days for {sum(len(s.list_feeds()) for s in stores.values())} feeds from Feedly...")
    ok, res = scraper.start("backfill", list(stores), days, trigger="cli", wait=True)
    if not ok:
        print(f"Backfill not started: {res}")
        return {}
    run = scraper.status()["run"]
    print(f"Backfill {run['status']}: {run['done']}/{run['total']} feeds, {run['failed']} failed.")
    if run["status"] != "completed":
        print(f"{run.get('error') or 'Stopped early.'} Run --backfill {days:g} again to resume; "
              "finished feeds are skipped and nothing is lost.")
    return run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=str(ROOT / "data"), help="directory holding one <vertical>.db per vertical")
    ap.add_argument("--interval", type=float, default=15, help="minutes between automatic refreshes")
    ap.add_argument("--no-auto", action="store_true",
                    help="don't refresh on a schedule (an external orchestrator calls /api/scrape/start)")
    ap.add_argument("--no-resume", action="store_true", help="don't resume an interrupted scrape on startup")
    ap.add_argument("--backfill", type=float, metavar="DAYS",
                    help=f"load the last DAYS (max {RETENTION_DAYS}) of history for every feed from Feedly first")
    ap.add_argument("--import", dest="import_file", metavar="FILE", help="add feed URLs from FILE to --vertical")
    ap.add_argument("--vertical", choices=list(VERTICALS_BY_KEY), default="games", help="vertical for --import")
    ap.add_argument("--no-serve", action="store_true", help="exit after seeding/--import/--backfill")
    ap.add_argument("--api-token", default=os.environ.get("NEWS_API_TOKEN"),
                    help="token that lets other machines call the API (Authorization: Bearer <token>); "
                         "default: $NEWS_API_TOKEN")
    ap.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                    help="extra hostname allowed to make changes through the UI/API ('*' = any)")
    ap.add_argument("--allow-private-feeds", action="store_true",
                    help="allow feed URLs on localhost/private networks (off by default to prevent SSRF)")
    args = ap.parse_args()

    feeds.ALLOW_PRIVATE = args.allow_private_feeds
    data_dir = Path(args.data)
    for old in {Path.cwd() / "news.db", ROOT / "news.db"}:
        if old.exists():
            print(f"Note: {old} is a database from the first version and is no longer used. To keep feeds you "
                  f"added there, re-add them in the UI or with --import.", file=sys.stderr)
    stores = open_stores(data_dir)
    if args.import_file:
        n = stores[args.vertical].add_feeds(read_feed_list(Path(args.import_file)))
        print(f"Imported {n} new feed(s) into {args.vertical}")

    scraper = Scraper(stores, data_dir / "scrape_state.json", interval_minutes=args.interval,
                      auto=not args.no_auto and args.interval > 0, log=print)
    if args.backfill:
        backfill(stores, args.backfill, scraper)
    if args.no_serve:
        return

    if not args.no_resume:
        scraper.resume_interrupted()
    threading.Thread(target=scraper.loop, daemon=True, name="scheduler").start()
    security = Security(api_token=args.api_token, allowed_hosts=tuple(h.lower() for h in args.allow_host)
                        + ((args.host.lower(),) if args.host not in ("0.0.0.0", "::") else ()))
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(stores, scraper, security))
    print(f"News aggregator running at http://{args.host}:{args.port}  (data: {args.data})")
    st = scraper.status()
    print(f"Scraping: {'every %g min' % st['interval_minutes'] if st['auto'] else 'manual (POST /api/scrape/start)'}"
          f"{'; API token enabled' if args.api_token else ''}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
