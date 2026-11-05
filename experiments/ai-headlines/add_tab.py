"""One-time test: add an "AI headlines" tab to a built snapshot (not part of the app).

    python3 experiments/ai-headlines/add_tab.py SNAPSHOT.html experiments/ai-headlines
"""
import html, json, sys
snap, d = sys.argv[1], sys.argv[2]
st = json.load(open(f"{d}/stories.json")); out = json.load(open(f"{d}/output.json"))
LABEL = {"games": "Games", "movies": "Movies & TV", "books": "Books", "news": "General News", "anime": "Anime", "manga": "Manga"}
e = html.escape
rows = "".join(f"""<details class="story ai"><summary><span class="aih">{e(out[str(s['n'])])}</span></summary>
<ul>{''.join(f'<li>{e(t)}</li>' for t in s['headlines'])}</ul></details>""" for s in st)
section = f"""
  <section id="view_ai" hidden>
    <div class="stories">{rows}</div>
  </section>
"""
css = """
  .aiintro { max-width: 900px; font-size: 13px; color: var(--muted); margin: 0 0 12px; line-height: 1.5; }
  .story.ai summary { list-style: none; cursor: pointer; display: grid; gap: 2px; }
  .story.ai summary::-webkit-details-marker { display: none; }
  .aitag { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .03em; }
  .aih { font-weight: 600; color: var(--text); }
  .aiold { font-size: 13px; color: var(--muted); }
  .story.ai ul { margin: 8px 0 2px; padding-left: 18px; font-size: 13px; color: var(--muted); }
"""
h = open(snap).read()
def sw(a, b):
    global h
    assert h.count(a) >= 1, a
    h = h.replace(a, b, 1)
sw('<a href="#stories/" data-top="stories">Stories</a>', '<a href="#stories/" data-top="stories">Stories</a>\n    <a href="#ai" data-top="ai">AI headlines</a>')
sw("</style>", css + "</style>")
sw("  <!-- ===== Stories (one tab per filter group) ===== -->", section + "  <!-- ===== Stories (one tab per filter group) ===== -->")
sw('  const [top, sub, extra] = location.hash.replace(/^#/, "").split("/");', '  const [top, sub, extra] = location.hash.replace(/^#/, "").split("/");\n  if (top === "ai") return { top, sub: "" };')
sw('  const subs = route.top === "stories"', '  $("view_ai").hidden = route.top !== "ai";\n  if (route.top === "ai") { $("sub_tabs").innerHTML = ""; SECTIONS.forEach((id) => { $(id).hidden = true; }); document.title = "AI headlines · Consensus News"; return; }\n  const subs = route.top === "stories"')
open(snap, "w").write(h)
print("ok")
