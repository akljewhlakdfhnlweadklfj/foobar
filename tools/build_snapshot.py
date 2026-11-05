"""Build a static, self-contained, read-only snapshot of the UI with current stories embedded.

    python3 tools/build_snapshot.py DATA_DIR OUT.html

The page is static/index.html with:
  - a small script that answers the page's API calls from embedded JSON instead of the server
    (tabs, "reported by at least N", minimum score and the watched-phrase filters keep working);
  - editing controls hidden (adding/removing feeds and phrases, weights, outlet switches, scraping);
  - the time window and match strictness fixed (changing them would need the server).
It's written for publishing as a claude.ai Artifact, whose wrapper supplies <html>/<head>/<body>.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import matching  # noqa: E402

FEED_FIELDS = ("id", "url", "title", "name", "item_count", "last_fetched", "via", "weight", "enabled", "required")

SHIM = r"""
<script>
(() => {
  const SNAP = JSON.parse(document.getElementById("snapshot-data").textContent);
  const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  window.fetch = async (path, opts = {}) => {
    const u = new URL(path, "https://snapshot.invalid/");
    const parts = u.pathname.split("/").filter(Boolean);
    if ((opts.method || "GET") !== "GET") return reply({ error: "This is a read-only snapshot." }, 403);
    if (u.pathname === "/api/verticals") return reply(SNAP.meta);
    if (u.pathname === "/api/scrape/status") return reply(SNAP.scrape);
    const v = parts[1];
    if (parts[2] === "settings") return reply({ settings: SNAP.settings[v], defaults: SNAP.settings[v] });
    if (parts[2] === "feeds") return reply({ feeds: SNAP.feeds[v], refreshing: false, scrape: SNAP.scrape });
    if (parts[2] === "phrases") return reply({ phrases: SNAP.phrases[v] });
    if (parts[2] === "stories") {
      // The viewer asks without parameters: apply the vertical's saved settings, as the server does.
      const c = SNAP.settings[v];
      const required = new Set(SNAP.feeds[v].filter((f) => f.required === 1).map((f) => f.name || f.title || f.url));
      const stories = SNAP.stories[v].map((s) => {
        const sources = s.sources.map((x) => ({ ...x, counted: x.weight >= c.min_weight }));
        const counted = sources.filter((x) => x.counted);
        const base = counted.reduce((t, x) => t + x.weight, 0);
        return { ...s, sources, coverage: counted.length, base_score: base, score: base + s.bonus, outlets: s.sources.length };
      }).filter((s) => {
        if (!s.coverage) return false;
        if (required.size) {
          const have = s.sources.filter((x) => required.has(x.feed)).length;
          if (!have || (c.require_all && have < required.size)) return false;
        }
        if (c.only_watched && !s.watched.length) return false;
        if (s.watched.length && c.watched_any) return true;
        return s.coverage >= c.min_feeds && s.score >= c.min_score;
      });
      return reply({ stories, items_considered: SNAP.items[v], settings: c, terms: SNAP.terms[v] });
    }
    return reply({ error: "Not available in this snapshot" }, 404);
  };
})();
</script>
"""

HIDE_CSS = """
  /* Read-only snapshot: just the Stories viewer. */
  #top_tabs, .scrape button { display: none !important; }
  /* Artifact pages can't load images from other sites. */
  .thumb { display: none !important; }
  .snapnote { font-size: 12px; color: var(--muted); }
"""


def build(data_dir: Path, out: Path) -> dict:
    stores = app.open_stores(data_dir, seed=False)
    now = time.time()
    snap = {
        "taken": now,
        "meta": {"verticals": [{"key": v.key, "label": v.label, "default_min_feeds": v.default_min_feeds}
                               for v in app.VERTICALS],
                 "default_threshold": matching.DEFAULT_THRESHOLD, "default_weight": matching.DEFAULT_WEIGHT,
                 "max_weight": app.MAX_WEIGHT, "default_bonus": app.DEFAULT_BONUS, "max_bonus": app.MAX_BONUS},
        "scrape": {"state": "idle", "run": None, "resumable": False, "auto": False, "interval_minutes": 0,
                   "last_run": {"finished_at": now}},
        "feeds": {}, "phrases": {}, "stories": {}, "items": {}, "settings": {}, "terms": {},
    }
    for v in app.VERTICALS:
        st = stores[v.key]
        snap["feeds"][v.key] = [{k: f.get(k) for k in FEED_FIELDS} for f in st.list_feeds()]
        snap["phrases"][v.key] = st.list_phrases()
        cfg = app.effective_settings(v, st)
        snap["settings"][v.key] = cfg
        # Stories the viewer may show (the shim applies the saved filters); single-outlet stories are only
        # kept when they're watched or the vertical's minimum is 1, to keep the page small.
        res = app.build_stories(st, min(2, cfg["min_feeds"]), cfg["hours"], cfg["threshold"], 0, watched_any_coverage=True)
        snap["stories"][v.key] = res["stories"]
        shown = app.build_stories(st, cfg["min_feeds"], cfg["hours"], cfg["threshold"], cfg["min_score"],
                                  only_watched=cfg["only_watched"], watched_any_coverage=cfg["watched_any"],
                                  require_all=cfg["require_all"], min_weight=cfg["min_weight"])
        snap["terms"][v.key] = shown["terms"]  # word cloud for the stories the viewer shows
        snap["items"][v.key] = res["items_considered"]

    html = (ROOT / "static" / "index.html").read_text()
    # The Artifact wrapper supplies the document skeleton.
    html = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset=\"utf-8\">\s*<meta name=\"viewport\"[^>]*>\s*",
                  "", html, flags=re.I)
    html = html.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
    html = html.replace("<title>Consensus News</title>",
                        '<title>Consensus News</title>\n<meta name="claude:viewport-fit-sides" content="cover">', 1)
    # Theme: explicit data-theme blocks, as the Artifact viewer can force light/dark.
    m = re.search(r"  @media \(prefers-color-scheme: dark\) \{\n    :root \{(.*?)\}\n  \}\n", html, re.S)
    if m:
        dark = m.group(1)
        html = html.replace(m.group(0),
                            "  @media (prefers-color-scheme: dark) {\n    :root:not([data-theme=\"light\"]) {" + dark
                            + " color-scheme: dark; }\n  }\n  :root[data-theme=\"dark\"] {" + dark + " color-scheme: dark; }\n")
    html = html.replace("</style>", HIDE_CSS + "</style>", 1)
    taken = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%b %-d, %Y %H:%M UTC")
    html = html.replace('<span id="status" aria-live="polite"></span>',
                        f'<span class="snapnote">Read-only snapshot · {taken}</span><span id="status" hidden></span>', 1)
    # Always open on the Stories view (settings aren't editable here).
    html = html.replace('if (!location.hash) history.replaceState(null, "", "#stories/all");',
                        'if (!location.hash.startsWith("#stories")) history.replaceState(null, "", "#stories/all");', 1)
    data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("<script>\nconst $ =",
                        f'<script type="application/json" id="snapshot-data">{data}</script>\n{SHIM}\n<script>\nconst $ =', 1)
    out.write_text(html)
    return {k: len(v) for k, v in snap["stories"].items()}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    counts = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"wrote {sys.argv[2]} ({Path(sys.argv[2]).stat().st_size / 1e6:.2f} MB); stories: {counts}")
