"""Build a static, self-contained, read-only snapshot of the UI with current stories embedded.

    python3 tools/build_snapshot.py DATA_DIR OUT.html

The page is static/index.html with the shared story digests embedded (the files the server
publishes; windows up to 7 days) and a small script answering the page's API calls from them. Since
all personal filtering already happens in the browser, Stories and the per-vertical Settings work as
in the app (choices are kept in the viewer's browser); the shared catalog, admin controls and
scraping are hidden.
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
import digest as digest_mod  # noqa: E402
import matching  # noqa: E402

SHIM = r"""
<script>
(() => {
  // Answers the page's API calls from the embedded digests (the same files the server publishes).
  const SNAP = JSON.parse(document.getElementById("snapshot-data").textContent);
  const reply = (obj, status = 200) => new Response(typeof obj === "string" ? obj : JSON.stringify(obj),
    { status, headers: { "Content-Type": "application/json" } });
  window.fetch = async (path, opts = {}) => {
    const u = new URL(path, "https://snapshot.invalid/");
    const parts = u.pathname.split("/").filter(Boolean);
    if ((opts.method || "GET") !== "GET") return reply({ error: "This is a read-only snapshot." }, 403);
    if (u.pathname === "/api/verticals") return reply(SNAP.meta);
    if (u.pathname === "/api/scrape/status") return reply(SNAP.scrape);
    if (parts[2] === "digest") {
      const d = SNAP.digests[parts[1]] || {};
      const h = Number(u.searchParams.get("hours")) || 0;
      const w = SNAP.meta.windows.find((x) => h <= x) ?? SNAP.meta.windows[SNAP.meta.windows.length - 1];
      return d[w] ? reply(d[w]) : reply({ error: "Not in this snapshot" }, 404);
    }
    return reply({ error: "Not available in this snapshot" }, 404);
  };
})();
</script>
"""

HIDE_CSS = """
  /* Snapshot: stories plus personal settings (kept in this browser); no catalog, admin or scraping. */
  .scrape button, #admin_panel, details.cat, #sub_tabs a[href="#settings/scraping"] { display: none !important; }
  /* Artifact pages can't load images from other sites. */
  .thumb { display: none !important; }
  .snapnote { font-size: 12px; color: var(--muted); }
"""


def _swap(html: str, old: str, new: str) -> str:
    if old not in html:
        raise SystemExit(f"build_snapshot: the page no longer contains {old!r}; update tools/build_snapshot.py")
    return html.replace(old, new, 1)


def build(data_dir: Path, out: Path, max_hours: int = 168) -> dict:
    stores = app.open_stores(data_dir, seed=False)
    digests = app.make_digests(stores)       # in memory only
    now = time.time()
    windows = [w for w in digest_mod.WINDOWS if w <= max_hours] or [digest_mod.WINDOWS[0]]
    meta = {"verticals": [{"key": v.key, "label": v.label, "default_min_feeds": v.default_min_feeds}
                          for v in app.VERTICALS if v.key in stores],
            "default_threshold": matching.DEFAULT_THRESHOLD, "default_weight": matching.DEFAULT_WEIGHT,
            "max_weight": app.MAX_WEIGHT, "default_bonus": app.DEFAULT_BONUS, "max_bonus": app.MAX_BONUS,
            "windows": windows, "retention_days": windows[-1] / 24}
    snap = {"meta": meta,
            "scrape": {"state": "idle", "run": None, "resumable": False, "auto": False, "interval_minutes": 0,
                       "last_run": {"finished_at": now}},
            "digests": {}}
    counts = {}
    for v in meta["verticals"]:
        snap["digests"][v["key"]] = {}
        for w in windows:
            d = json.loads(digests.get(v["key"], w)["json"])
            snap["digests"][v["key"]][str(w)] = d
            counts[f"{v['key']}-{w}h"] = len(d["stories"])

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
    html = html.replace(' “Catalog” under an outlet changes it for everyone (admin).', '', 1)
    html = _swap(html, '<span id="status" aria-live="polite"></span>',
                 f'<span class="snapnote">Read-only snapshot · {taken}</span><span id="status" hidden></span>')
    # Settings open on the first vertical (the Scraping page isn't part of the snapshot), and the time
    # window only offers what's embedded.
    first = meta["verticals"][0]["key"]
    html = _swap(html, '<a href="#settings/scraping" data-top="settings">', f'<a href="#settings/{first}" data-top="settings">')
    html = _swap(html, 'keys.includes(sub) ? sub : "scraping" }', f'keys.includes(sub) ? sub : "{first}" }}')
    html = _swap(html, 'if (!location.hash) history.replaceState(null, "", "#stories/all");',
                 'if (!location.hash || location.hash.startsWith("#settings/scraping")) history.replaceState(null, "", "#stories/all");')
    html = re.sub(r'\s*<option value="(\d+)"[^>]*>[^<]*</option>',
                  lambda m: "" if float(m.group(1)) > windows[-1] else m.group(0), html)
    data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/")
    html = _swap(html, "\n<script>\n", f'\n<script type="application/json" id="snapshot-data">{data}</script>\n{SHIM}\n<script>\n')
    out.write_text(html)
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    counts = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"wrote {sys.argv[2]} ({Path(sys.argv[2]).stat().st_size / 1e6:.2f} MB); stories: {counts}")
