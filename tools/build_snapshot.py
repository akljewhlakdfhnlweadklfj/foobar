"""Build a static, self-contained, read-only snapshot of the UI with current stories embedded.

    python3 tools/build_snapshot.py DATA_DIR OUT.html

The page is static/index.html with the filter groups' story lists embedded (what the server serves at
/api/groups/<id>/stories) and a small script answering the page's API calls from them. Stories (one tab
per group) and Settings › Watched phrases work as in the app, as do "last seen" and phrases, which live
in the viewer's browser; the group editor, catalog and scraping pages are hidden. Groups come from
DATA_DIR/groups.db, or one per vertical from its saved settings when there's none.
It's written for publishing as a claude.ai Artifact, whose wrapper supplies <html>/<head>/<body>.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import groups as groups_mod  # noqa: E402
import matching  # noqa: E402

SHIM = r"""
<script>
(() => {
  // Answers the page's API calls from the embedded group views (the same files the server serves).
  const SNAP = JSON.parse(document.getElementById("snapshot-data").textContent);
  const reply = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
  window.fetch = async (path, opts = {}) => {
    const u = new URL(path, "https://snapshot.invalid/");
    const parts = u.pathname.split("/").filter(Boolean);
    if ((opts.method || "GET") !== "GET") return reply({ error: "This is a read-only snapshot." }, 403);
    if (u.pathname === "/api/verticals") return reply(SNAP.meta);
    if (u.pathname === "/api/scrape/status") return reply(SNAP.scrape);
    if (u.pathname === "/api/groups") return reply({ groups: SNAP.groups, ai: SNAP.meta.ai });
    if (parts[1] === "groups" && parts[3] === "stories") {
      const v = SNAP.views[parts[2]];
      return v ? reply(v) : reply({ error: "No such group" }, 404);
    }
    return reply({ error: "Not available in this snapshot" }, 404);
  };
})();
</script>
"""

HIDE_CSS = """
  /* Snapshot: no scraping controls. */
  .scrape button { display: none !important; }
  /* Artifact pages can't load images from other sites. */
  .thumb, .vthumb { display: none !important; }
"""


def _swap(html: str, old: str, new: str) -> str:
    if old not in html:
        raise SystemExit(f"build_snapshot: the page no longer contains {old!r}; update tools/build_snapshot.py")
    return html.replace(old, new, 1)


def build(data_dir: Path, out: Path) -> dict:
    # Only the verticals that have a database (opening one doesn't create it here).
    stores = {v.key: app.Store(data_dir / f"{v.key}.db") for v in app.VERTICALS if (data_dir / f"{v.key}.db").exists()}
    digests = app.make_digests(stores)       # in memory only
    now = time.time()
    if (data_dir / "groups.db").exists():
        group_store = groups_mod.GroupStore(data_dir / "groups.db")
    else:
        group_store = groups_mod.GroupStore(Path(tempfile.mkdtemp()) / "groups.db")
        app.seed_groups(group_store, stores)
    views = groups_mod.Views(group_store, digests, stores)
    meta = {"verticals": [{"key": v.key, "label": v.label} for v in app.VERTICALS if v.key in stores],
            "default_weight": matching.DEFAULT_WEIGHT, "max_weight": app.MAX_WEIGHT,
            "default_bonus": app.DEFAULT_BONUS, "max_bonus": app.MAX_BONUS, "retention_days": app.RETENTION_DAYS,
            "ai": {"available": False, "reason": "not in a snapshot"}}
    snap = {"meta": meta,
            "scrape": {"state": "idle", "run": None, "resumable": False, "auto": False, "interval_minutes": 0,
                       "last_run": {"finished_at": now}},
            "groups": group_store.list(), "views": {}}
    counts = {}
    for g in snap["groups"]:
        view = json.loads(gzip.decompress(views.get(g["id"])["gz"]))
        snap["views"][str(g["id"])] = view
        counts[g["name"]] = sum(1 for st in view["stories"] if "x" not in st)

    html = (ROOT / "static" / "index.html").read_text()
    # The Artifact wrapper supplies the document skeleton.
    html = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset=\"utf-8\">\s*<meta name=\"viewport\"[^>]*>\s*",
                  "", html, flags=re.I)
    html = html.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
    html = html.replace("<title>Consensus News</title>",
                        '<title>Consensus News</title>\n<meta name="claude:viewport-fit-sides" content="cover">', 1)
    html = html.replace("</style>", HIDE_CSS + "</style>", 1)   # (the page itself adapts to being a snapshot)
    data = json.dumps(snap, separators=(",", ":")).replace("</", "<\\/")
    html = _swap(html, "\n<script>\n", f'\n<script type="application/json" id="snapshot-data">{data}</script>\n{SHIM}\n<script>\n')
    out.write_text(html)
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    counts = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"wrote {sys.argv[2]} ({Path(sys.argv[2]).stat().st_size / 1e6:.2f} MB); stories: {counts}")
