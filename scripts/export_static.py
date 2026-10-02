"""
Export the viewer for one page as a single, self-contained, read-only HTML file.

    python scripts/export_static.py --page 7 --out exports/LR20218_viewer.html

Everything the live viewer shows for that page is recorded from the running
server (cache-only, so no model is called) and embedded: every stage event,
readings, detector boxes, answers with provenance, tables, review queue,
rules, live-cost stats. The real viewer code runs on top of it — a small shim
answers its API requests from the embedded data and replays the stage events —
so the snapshot looks and behaves like the viewer: click any box or value and
the inspector shows a crop cut, in the browser, from an embedded full-resolution
copy of the scan. Run, upload and accept/reject controls are hidden.

Needs the viewer running (python web/server.py).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline.paths import RENDERS  # noqa: E402

STATIC = ROOT / "web" / "static"


def _get(server: str, path: str):
    with urllib.request.urlopen(server + path, timeout=120) as r:
        return r.read()


def _events(server: str, page: int) -> list[dict]:
    raw = _get(server, f"/api/run?page={page}&cache_only=1").decode()
    return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]


def _jpeg_b64(img: Image.Image, quality: int) -> str:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


SHIM = r"""
<script>
/* Read-only snapshot: answer the viewer's API calls from embedded data. */
(function () {
  const D = window.__STATIC__ = JSON.parse(document.getElementById('snapshot-data').textContent);
  const full = new Image(); full.src = D.full;
  window.__pageURL = () => D.display;
  window.__cropURL = q => {
    const p = Object.fromEntries(new URLSearchParams(q));
    const pad = +(p.pad || 60), zoom = +(p.zoom || 3);
    if (!full.complete || !full.naturalWidth) return D.display;
    const x0 = Math.max(0, +p.x0 - pad), y0 = Math.max(0, +p.y0 - pad);
    const x1 = Math.min(full.naturalWidth, +p.x1 + pad), y1 = Math.min(full.naturalHeight, +p.y1 + pad);
    const c = document.createElement('canvas');
    c.width = Math.max(1, (x1 - x0) * zoom); c.height = Math.max(1, (y1 - y0) * zoom);
    const g = c.getContext('2d'); g.imageSmoothingQuality = 'high';
    g.drawImage(full, x0, y0, x1 - x0, y1 - y0, 0, 0, c.width, c.height);
    return c.toDataURL('image/jpeg', 0.92);
  };
  const json = x => new Response(JSON.stringify(x), {headers: {'Content-Type': 'application/json'}});
  const RO = {error: 'This is a read-only snapshot of the run; changes are made in the live viewer.'};
  window.fetch = async (url, opts) => {
    const u = String(url), post = opts && opts.method && opts.method !== 'GET';
    if (u.startsWith('/api/documents')) return json(D.documents);
    if (u.startsWith('/api/models')) return json(D.models);
    if (u.startsWith('/api/pair-check')) return json({warning: null});
    if (u.startsWith('/api/rules/')) return json(RO);
    if (u.startsWith('/api/rules')) return json(D.rules);
    if (/\/api\/review\/\d+\/export/.test(u)) return json(D.export);
    if (u.startsWith('/api/review/')) return json(post ? RO : D.review);
    return json(RO);
  };
  // Replay the recorded stage events in one pass once the handler is attached.
  // A chain of timers would crawl in a background tab (browsers throttle them),
  // and a snapshot has no live progress to animate.
  window.EventSource = class {
    constructor() {
      this.closed = false;
      this.timer = setTimeout(() => {
        for (const ev of D.events) {
          if (this.closed) break;
          if (this.onmessage) this.onmessage({data: JSON.stringify(ev)});
        }
      }, 0);
    }
    close() { this.closed = true; clearTimeout(this.timer); }
  };
})();
</script>
"""

STATIC_CSS = """
/* snapshot: read-only controls hidden */
#run, #rerun, label.btn, #ruleCheck, #ruleCats, .acts, .q .acts, a[href^="/api/tables"],
header label:has(#useCascade) { display: none !important }
.snapbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; font-size: 12px;
  color: var(--dim); border: 1px solid var(--line); border-radius: 7px; padding: 4px 10px; background: var(--panel2) }
.snapbar b { color: var(--fg) }
.snapbar a { color: var(--accent) }
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--server", default="http://127.0.0.1:5001")
    ap.add_argument("--summary-link", default="summary.html",
                    help="relative link shown in the banner ('' to omit)")
    a = ap.parse_args()
    s, pg = a.server.rstrip("/"), a.page

    docs = [d for d in json.loads(_get(s, "/api/documents")) if d["page"] == pg]
    if not docs:
        raise SystemExit(f"page {pg} is not in the viewer")
    events = _events(s, pg)
    done = next((e["summary"] for e in events if e["type"] == "done"), {})
    full = Image.open(RENDERS / f"page{pg}_full.png")
    display = full.copy()
    display.thumbnail((2200, 2200))
    data = {
        "documents": docs,
        "models": json.loads(_get(s, "/api/models")),
        "events": events,
        "review": json.loads(_get(s, f"/api/review/{pg}")),
        "rules": json.loads(_get(s, f"/api/rules?page={pg}")),
        "export": {"page": pg, "sheet_id": done.get("sheet_id"), "answers": done.get("answers", {}),
                   "note": "from the read-only snapshot"},
        "display": "data:image/jpeg;base64," + _jpeg_b64(display, 85),
        "full": "data:image/jpeg;base64," + _jpeg_b64(full, 80),
    }

    page_html = (STATIC / "index.html").read_text()
    body = re.search(r"<body>(.*)</body>", page_html, re.S).group(1)
    body = re.sub(r'<script src="/static/app.js"></script>', "", body)
    when = dt.date.today().strftime("%-d %b %Y")
    banner = (f'<div class="snapbar"><b>Read-only snapshot</b><span>{done.get("sheet_id") or ""} · '
              f'saved results, recorded {when}</span>'
              + (f'<a href="{a.summary_link}">Results summary</a>' if a.summary_link else "") + "</div>")
    body = body.replace("</h1>", "</h1>" + banner, 1)
    title = f"{done.get('sheet_id') or f'Page {pg}'} Viewer"

    payload = json.dumps(data).replace("</", "<\\/")      # keep "</script>" out of the JSON
    css, js = (STATIC / "style.css").read_text(), (STATIC / "app.js").read_text()
    out = ('<meta charset="utf-8">\n'
           f"<title>{title}</title>\n<style>{css}{STATIC_CSS}</style>\n{body}\n"
           f'<script type="application/json" id="snapshot-data">{payload}</script>\n'
           f"{SHIM}\n<script>{js}</script>\n")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(out)
    print(f"wrote {a.out}: {len(out) / 1e6:.1f} MB · {len(events)} stage events · "
          f"{len(done.get('answers', {}))} categories")


if __name__ == "__main__":
    main()
