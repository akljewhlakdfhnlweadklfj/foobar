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
    if (parts[2] === "feeds") return reply({ feeds: SNAP.feeds[v], refreshing: false, last_run: SNAP.taken, scrape: SNAP.scrape });
    if (parts[2] === "phrases") return reply({ phrases: SNAP.phrases[v] });
    if (parts[2] === "stories") {
      const q = u.searchParams;
      const minFeeds = Math.max(2, parseInt(q.get("min_feeds"), 10) || 2);
      const minScore = Math.max(0, parseFloat(q.get("min_score")) || 0);
      const onlyWatched = q.get("only_watched") === "1", watchedAny = q.get("watched_any") === "1";
      const required = new Set(SNAP.feeds[v].filter((f) => f.required === 1).map((f) => f.name || f.title || f.url));
      const requireAll = q.get("require_all") === "1";
      const minWeight = Math.max(0, parseFloat(q.get("min_weight")) || 0);
      const recount = (s) => {  // apply "only count outlets weighted at least" to the embedded story
        const sources = s.sources.map((x) => ({ ...x, counted: x.weight >= minWeight }));
        const counted = sources.filter((x) => x.counted);
        const base = counted.reduce((t, x) => t + x.weight, 0);
        return { ...s, sources, coverage: counted.length, base_score: base, score: base + s.bonus, outlets: s.sources.length };
      };
      const stories = SNAP.stories[v].map(recount).filter((s) => s.coverage > 0).filter((s) => {
        if (required.size) {
          const have = s.sources.filter((x) => required.has(x.feed)).length;
          if (!have || (requireAll && have < required.size)) return false;
        }
        if (onlyWatched && !s.watched.length) return false;
        if (s.watched.length && watchedAny) return true;
        return s.coverage >= minFeeds && s.score >= minScore;
      });
      stories.sort((a, b) => b.score - a.score || b.coverage - a.coverage || b.latest - a.latest);
      return reply({ stories, items_considered: SNAP.items[v] });
    }
    return reply({ error: "Not available in this snapshot" }, 404);
  };
})();
</script>
"""

HIDE_CSS = """
  /* Read-only snapshot: hide the controls that change data. */
  #add_form, #watch_form, #refresh, #stop, .scrape .auto, .feed-tools button, ul.feeds .switch,
  ul.feeds button[data-del], ul.phrases button, ul.feeds .req, #clear_required { display: none !important; }
  input.weight, ul.phrases input { pointer-events: none; border-color: transparent; background: transparent; }
  .snapnote { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
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
        "feeds": {}, "phrases": {}, "stories": {}, "items": {},
    }
    for v in app.VERTICALS:
        st = stores[v.key]
        snap["feeds"][v.key] = [{k: f.get(k) for k in FEED_FIELDS} for f in st.list_feeds()]
        snap["phrases"][v.key] = st.list_phrases()
        res = app.build_stories(st, 2, 48, matching.DEFAULT_THRESHOLD, watched_any_coverage=True)
        snap["stories"][v.key] = res["stories"]
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
    html = html.replace('id="hours">', 'id="hours" disabled title="Fixed at 48 hours in this snapshot">', 1)
    html = re.sub(r'(<input type="range" id="threshold"[^>]*?)>', r'\1 disabled title="Fixed in this snapshot">', html, count=1)
    taken = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%b %-d, %Y %H:%M UTC")
    html = html.replace('<div class="msg" id="add_msg"></div>',
                        f'<p class="snapnote">Snapshot taken {taken}. Read-only.</p>\n      <div class="msg" id="add_msg"></div>', 1)
    html = html.replace('<div class="msg" id="watch_msg"></div>',
                        '<p class="snapnote">Phrases are added in the running app; this snapshot shows any that were '
                        'watched when it was taken.</p>\n      <div class="msg" id="watch_msg"></div>', 1)
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
