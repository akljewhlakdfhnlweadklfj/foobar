"""Key terms: the most prominent proper nouns and phrases of each story ("Suda51", "God of War Laufey"),
shown before its headline. Two passes: vocabulary across all the stories given, then each story's own
ranking (position in the headline, share of headlines and descriptions, distinctiveness). See README,
"Using it".

story_terms(stories, bodies=...) takes [{"title", "score", "sources": [{"title"}]}] and adds
"keywords": [{"term", "weight", "forms"}] to each story.
"""

from __future__ import annotations

import functools
import re
from collections import Counter, defaultdict

import matching

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
    dies die died dead death aged age obituary
""".split())
_GENERIC_STEMS = frozenset(matching._stem(w) for w in GENERIC_TERMS)
MIN_SHARE = 0.4
MAX_RUN_WORDS = 4
STORY_KEYWORDS = 6
NAME_BOOST, RUN_BOOST = 1.5, 1.25   # ranking boost for multi-word names / other multi-word runs
SINGLE_NAME_BOOST = 1.25            # single-word proper nouns ("Trump", "Texas", "Suda51")
COMMON_WEIGHT = 0.7                 # common words and phrases, relative to names
BACKGROUND_SHARE = 0.15             # a common word in this share of the listed stories is background
PROPER_LEAD_MIN = 0.35              # a proper noun leads if it scores at least this share of the top term
START_WEIGHT = 2.0                  # a term at the start of a headline counts this much vs 1 at the end
BODY_WEIGHT = 0.3                   # a description mention counts this much vs a headline mention

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
    possible likely unlikely second third shut shuts closes closed close
""".split())
# Words that are capitalised as part of a name ("Nagoshi Studio", "Booker Prize", "President Biden") but
# aren't names on their own: never a single-word proper noun, still fine inside a multi-word name.
NAME_PARTS = frozenset(matching._stem(w) for w in """
    studio studios creator creators developer developers publisher publishers series sequel franchise company
    team union president minister senator governor speaker leader chief director actor actress author writer
    prize award awards festival league cup university college school press group network channel museum
    department ministry agency council committee party court house senate congress city state county
    season episode chapter volume edition collection remaster remake trailer showcase direct conference
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
_WORD_RE = re.compile(r"[^\W_][\w'’+\-]*")   # Unicode letters/digits: "Miéville", "Pokémon"


@functools.lru_cache(maxsize=200_000)
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
_TOKEN_RE = re.compile(r"[^\W_][\w'’+\-.]*[^\W_+]|[^\W_]|&")
MAX_NAME_WORDS = 6
_QUOTED_RE = re.compile(r"[‘“\"]([^‘’“”\"]{3,80})[’”\"]|(?<!\w)'([^']{3,80})'(?!\w)")


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
    # Quoted titles are names even in Title Case headlines: "Patrick Stewart, ‘Star Trek’ Captain".
    for m in _QUOTED_RE.finditer(title):
        inner = [_strip_possessive(w) for w in _TOKEN_RE.findall(m.group(1) or m.group(2))]
        names_in = [w for w in inner if w.lower() not in NAME_CONNECTORS]
        if 2 <= len(inner) <= MAX_NAME_WORDS and len(names_in) >= 2 and all(_is_cap(w) for w in names_in) \
                and inner[-1].lower() not in NAME_CONNECTORS:
            out[tuple(w.lower() for w in inner)] = " ".join(inner)
    at_title_start = True
    for segment in _SEGMENT_RE.split(title):
        raw = _TOKEN_RE.findall(segment)
        words = [_strip_possessive(w) for w in raw]
        n = len(words)
        for i in range(n):
            first = words[i]
            mid_sentence = not title_case and not (at_title_start and i == 0)
            if not _is_cap(first) or first.lower() in NAME_CONNECTORS or first.isdigit():
                continue
            if first.lower() in matching.STOPWORDS and not (mid_sentence and first[:1].isupper()):
                continue                          # "No More Heroes" only when "No" is capitalised mid-sentence
            for j in range(i + 1, min(n, i + MAX_NAME_WORDS)):
                w = words[j]
                if w.lower() in NAME_CONNECTORS:
                    continue                      # connector: allowed inside, never at the end
                if not _is_cap(w):
                    break                         # an ordinary lowercase word ends the name
                span = words[i:j + 1]
                names = [x for x in span if x.lower() not in NAME_CONNECTORS]
                has_conn = len(names) < len(span)
                last = span[-1]
                if len(names) < 2:
                    continue
                if not (_term_key(last) or any(c.isdigit() for c in last) or last.isupper() or has_conn):
                    continue                      # don't end on filler ("Switch Official"), unless clearly a name ("Ocarina of Time")
                # In a sentence-case headline the capitals after the first word mark a name, so a
                # run may start with a generic word there ("Star Trek actor…", "No More Heroes").
                if not (_term_key(first) or has_conn or any(c.isdigit() for c in first) or first.isupper()
                        or (mid_sentence and first.lower() in matching.STOPWORDS)
                        or (not title_case and len(names) >= 2 and first.lower() not in matching.STOPWORDS)):
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
        if raw:
            at_title_start = False
    return out


def _display_name(forms: Counter) -> str:
    """Most common surface form, with connectors lowercased after the first word."""
    words = forms.most_common(1)[0][0].split()
    return " ".join(w if i == 0 or w.lower() not in NAME_CONNECTORS else w.lower() for i, w in enumerate(words))


def _contains(longer: tuple, shorter: tuple) -> bool:
    n = len(shorter)
    return any(longer[i:i + n] == shorter for i in range(len(longer) - n + 1))


@functools.lru_cache(maxsize=50_000)
def _whole_word(word: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)")


def _word_at(lowered_title: str, word: str) -> int:
    """Index of `word` as a whole word in a lowercased headline ("no" must not match "now")."""
    m = _whole_word(word.lower()).search(lowered_title)
    return m.start() if m else 0


def _position_weight(rel: float) -> float:
    """Weight of a term by where it first appears in a headline: START_WEIGHT at the start, 1 at the end."""
    return 1 + (START_WEIGHT - 1) * (1 - min(1.0, max(0.0, rel)))


def _body_terms(text: str) -> set:
    """Keys of the single words, word runs and names in a description (for the body bonus)."""
    keys = set()
    for seg in _SEGMENT_RE.split(text):
        seg_keys = [_term_key(w) for w in _WORD_RE.findall(seg)]
        keys.update(k for k in seg_keys if k)
        for i in range(len(seg_keys)):
            for j in range(i + 2, min(len(seg_keys), i + MAX_RUN_WORDS) + 1):
                run = seg_keys[i:j]
                if None in run:
                    break
                keys.add(("pair",) + tuple(run))
    keys.update(("name",) + k for k in _name_candidates(text))
    return keys


def story_terms(stories: list[dict], limit: int = 40, bodies: list[list[str]] | None = None) -> list[dict]:
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
    per_story = []                                # (story, {key: share}, {multi key: share}, position, body)

    for si, s in enumerate(stories):
        titles = {x["title"] for x in s["sources"]} | {s["title"]}
        cnt_u, cnt_m = Counter(), Counter()
        pos_sum: dict = defaultdict(float)        # key -> summed position weight over the headlines with it
        for title in titles:
            seen_u, seen_m = set(), {}
            low = title.lower()
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
                if k not in seen_u:
                    pos_sum[k] += _position_weight(i / max(1, len(words) - 1))
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
                            surface = seg_words[i:j - 1] + [_strip_possessive(seg_words[j - 1])]
                            if key not in seen_m:
                                at = _word_at(low, seg_words[i])
                                pos_sum[key] += _position_weight(at / max(1, len(title)))
                            seen_m.setdefault(key, None)
                            multi_forms[key][" ".join(x.strip("-") for x in surface)] += 1
            for key, text in _name_candidates(title).items():
                if ("name",) + key not in seen_m:
                    at = _word_at(low, text.split()[0])
                    pos_sum[("name",) + key] += _position_weight(at / max(1, len(title)))
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
        position = {k: pos_sum[k] / c for k, c in (cnt_u + cnt_m).items() if c}
        descs = [d for d in (bodies[si] if bodies else []) if d]
        body = Counter()
        for d in descs:
            body.update(_body_terms(d))
        body_share = {k: c / len(descs) for k, c in body.items()} if descs else {}
        per_story.append((s, own_u, own_m, position, body_share))

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

    def run_is_proper(k: tuple) -> bool:
        """A detected name, or a word run with at least one real name in it ("Nagoshi Studio": Nagoshi
        is capitalised mid-sentence in sentence-case headlines). Title Case alone isn't evidence:
        "Additional Cast" from a Title Case outlet is not a name."""
        if k[0] == "name":
            return True
        return all(w[:1].isupper() or w[:1].isdigit() for w in multi_display(k).split()) and \
            any(proper[w] > plain[w] and w not in NAME_PARTS for w in k[1:])   # not "Nintendo team (up)"

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

    # --- per-story keywords (pass 2: local) ---
    # Pass 1 above learned the vocabulary across all stories: spellings, which words are names
    # (capitalisation), and how many stories use each term. Here each story's terms are ranked using only
    # that story: headline share x position + body share, times a weight for the kind of term.
    # The only global input is "background" common words: used by a large share of the stories
    # ("dies", "captain", "election"), which are halved. Names are never demoted for recurring.
    n_stories = max(1, len(stories))

    def background(df: int) -> bool:
        return n_stories >= 5 and df / n_stories >= BACKGROUND_SHARE
    for s, own_u, own_m, position, body_share in per_story:

        def prominence(k, share, df, boost, proper_noun=False):
            """Local score: headline share x position (start counts more) + a smaller body share, times
            the kind-of-term weight; common words that are background across the list are halved."""
            local = share * position.get(k, 1.0) + BODY_WEIGHT * body_share.get(k, 0.0)
            kind = boost if proper_noun else boost * COMMON_WEIGHT * (0.5 if background(df) else 1.0)
            return kind * local

        cands, used = [], set()
        chosen: list[tuple] = []                  # (wks, share, cand index, is_name)
        for k, share in sorted(own_m.items(), key=lambda kv: (-kv[1], -(len(kv[0]) - 1))):
            wks = word_keys(k)
            if not wks or any(wks <= c[0] for c in chosen):
                continue                          # "Elder Scrolls VI" after "Elder Scrolls 6"; "GTA" after "GTA 6"
            # A phrase must cover most uses of its words; names less strictly, since outlets often
            # shorten them ("Nagoshi's studio" for "Nagoshi Studio").
            if k[0] == "pair" and share < (0.5 if run_is_proper(k) else 0.8) * max(own_u.get(w, 0) for w in wks):
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
            text = multi_display(k)
            proper_run = run_is_proper(k)
            # Headlines often use only a surname ("Johnson out as House leader"): credit those to the
            # full name ("Mike Johnson") when the last word is itself a capitalised name.
            last = _term_key(k[-1]) if k[0] == "name" else k[-1]
            if (proper_run and len(k) == 3 and not any(w in NAME_CONNECTORS for w in k[1:])
                    and last and own_u.get(last, 0) > share and proper[last] >= plain[last]):
                share = own_u[last]               # the surname is also used on its own
                position[k] = max(position.get(k, 1.0), position.get(last, 1.0))
            boost = NAME_BOOST if proper_run else RUN_BOOST    # what a story is about is usually a name
            cands.append((prominence(k, share, multi_n[k], boost, proper_run), text, multi_forms_list(k), k))
            used |= wks                           # "Johnson" is covered by "Mike Johnson"
        cands = [c for c in cands if c is not None]
        for k, share in own_u.items():
            if k not in used:
                shown = display(k)
                single_proper = proper[k] > plain[k] or (shown.isupper() and len(shown) > 1) or (
                    any(ch.isdigit() for ch in shown) and any(ch.isalpha() for ch in shown) and not shown[:1].isdigit())
                is_name = single_proper and k not in NOT_NOUNS and k not in NAME_PARTS
                cands.append((prominence(k, share, uni_n[k], SINGLE_NAME_BOOST if is_name else 1.0, is_name),
                              shown, sorted(forms[k])[:6], (k,)))
        cands.sort(key=lambda c: -c[0])
        # The lead keyword is the most prominent proper noun (a name, title, acronym, "Suda51") when one
        # scores at least PROPER_LEAD_MIN of the top term; otherwise the most prominent noun-like term.
        # The rest keep their order.
        def is_proper(c):
            k, text = c[3], c[1]
            if k[0] == "name":
                return True
            words = text.split()
            if any(w[:1].isdigit() for w in words[:1]):
                return False                       # "20th anniversary", "8 million copies"
            if k[0] == "pair":
                return run_is_proper(k)
            return (proper[k[0]] > plain[k[0]] or text.isupper() and len(text) > 1
                    or (any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text))) \
                and k[0] not in NOT_NOUNS and k[0] not in NAME_PARTS
        top_score = cands[0][0] if cands else 0
        lead = next((c for c in cands if is_proper(c) and c[0] >= PROPER_LEAD_MIN * top_score), None)
        if lead is None:
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
