"""
Local viewer for the extraction pipeline.

    python web/server.py     →  http://127.0.0.1:5001

Serves a single-page viewer that runs a document through the pipeline and
renders each stage as it completes. The point of the thing is the inspect
loop: click any detected region and see the native-resolution pixels it came
from, next to what each layer read there. That is the answer to "where is it
right, where is it wrong, and why".
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.paths import BENCH, CACHE, RENDERS, RUNS  # noqa: E402
from pipeline.paths import DATA  # noqa: E402
from pipeline.render import DOC_TYPES, ingest  # noqa: E402
from pipeline import rule_assistant, rules_store  # noqa: E402
from pipeline.resolve import resolve  # noqa: E402
from pipeline.run import RunConfig, list_pages, run  # noqa: E402
from pipeline.score import load_key  # noqa: E402
from pipeline.models import CATALOG, DEFAULT_FLAGSHIP, DEFAULT_LIGHT_PAIR, validate_pair  # noqa: E402
from pipeline.review import (apply_decisions, build_queue, load_decisions,  # noqa: E402
                       save_decision, summarise)

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(HERE / "static"))

# Display rasters are generated once and reused; the native page stays on disk.
DISPLAY_MAX = 2200
_display_cache: dict[int, bytes] = {}


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/documents")
def documents():
    return jsonify(list_pages())


@app.post("/api/upload")
def upload():
    """Accept a document (the unseen Doc 4): a PDF, scanned or digital, or a
    PNG/JPG/TIFF image. Each page becomes a new page in the viewer."""
    f = request.files.get("pdf") or request.files.get("file")
    if f is None or Path(f.filename).suffix.lower() not in DOC_TYPES:
        return jsonify({"error": f"upload one of: {', '.join(sorted(DOC_TYPES))}"}), 400
    uploads = DATA / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / Path(f.filename).name
    f.save(dest)
    try:
        return jsonify({"pages": ingest(dest)})
    except Exception as exc:  # noqa: BLE001 — an unreadable file must say why
        return jsonify({"error": f"could not read {f.filename}: {exc}"}), 400


@app.get("/api/page/<int:page>.png")
def page_png(page: int):
    """Downscaled raster for on-screen display only. Never used for extraction."""
    if page not in _display_cache:
        im = Image.open(RENDERS / f"page{page}_full.png").convert("RGB")
        s = min(1.0, DISPLAY_MAX / max(im.size))
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        _display_cache[page] = buf.getvalue()
    return Response(_display_cache[page], mimetype="image/jpeg")


@app.get("/api/crop")
def crop():
    """Native-resolution crop around a bbox — the 'why' view.

    This deliberately reads the full-resolution page, not the display raster,
    so what you inspect is exactly what the detector saw.
    """
    pg = int(request.args["page"])
    x0, y0, x1, y1 = (int(request.args[k]) for k in ("x0", "y0", "x1", "y1"))
    pad = int(request.args.get("pad", 60))
    zoom = float(request.args.get("zoom", 3))
    im = Image.open(RENDERS / f"page{pg}_full.png").convert("RGB")
    box = (max(0, x0 - pad), max(0, y0 - pad), min(im.width, x1 + pad), min(im.height, y1 + pad))
    c = im.crop(box)
    c = c.resize((int(c.width * zoom), int(c.height * zoom)), Image.LANCZOS)
    buf = io.BytesIO()
    c.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.get("/api/models")
def models():
    """The model catalogue, so the UI never hardcodes a model list."""
    return jsonify({
        "catalog": CATALOG,
        "default_light": list(DEFAULT_LIGHT_PAIR),
        "default_flagship": DEFAULT_FLAGSHIP,
        "default_b": RunConfig().model_b,
        "default_d": RunConfig().model_d,
        "default_t": RunConfig().model_t,
    })


@app.get("/api/pair-check")
def pair_check():
    """Warn about a weak light-model pair before a run is spent on it."""
    a, b = request.args.get("a", ""), request.args.get("b", "")
    try:
        return jsonify({"warning": validate_pair(a, b)})
    except KeyError as e:
        return jsonify({"warning": str(e)}), 400


def _load_run(page: int):
    f = RUNS / f"page{page}_latest.json"
    if not f.exists():
        return None, None, None, None
    summary = json.load(open(f))
    # Newer run records name their cache files; older ones only name the model.
    model = summary.get("model") or "claude-opus-5"
    slug = "".join(c if c.isalnum() or c in "-." else "-" for c in model)
    obs_f = CACHE / (summary.get("obs_cache") or f"page{page}_observations__{slug}.json")
    obs = []
    if obs_f.exists():
        blob = json.load(open(obs_f))
        obs = blob.get("observations", blob) if isinstance(blob, dict) else blob
    reg_f = CACHE / f"page{page}_regions.json"
    if not reg_f.exists():
        reg_f = BENCH / f"page{page}_regions.json"
    regions = json.load(open(reg_f)) if reg_f.exists() else None
    ctx_f = CACHE / (summary.get("ctx_cache") or f"page{page}_context.json")
    ctx = json.load(open(ctx_f)) if ctx_f.exists() else {}
    return summary, obs, regions, ctx


@app.get("/api/review/<int:page>")
def review_get(page: int):
    """The queue of things a person still has to decide."""
    summary, obs, regions, ctx = _load_run(page)
    if summary is None:
        return jsonify({"error": f"no saved run for page {page} — run it first"}), 404
    q = build_queue(summary, obs, regions, ctx)
    answers = summary.get("answers", {})
    corrected, applied = apply_decisions(answers, q)
    return jsonify({"page": page, "queue": q, "summary": summarise(q),
                    "answers": answers, "corrected": corrected, "applied": applied,
                    "model": summary.get("model")})


@app.post("/api/review/<int:page>")
def review_post(page: int):
    """Record one decision. Written straight to disk so a reviewer never loses
    work to a refresh or a re-run."""
    d = request.get_json(force=True) or {}
    iid, action = d.get("id"), d.get("action")
    if not iid or action not in ("accept", "reject", "correct"):
        return jsonify({"error": "need id and action=accept|reject|correct"}), 400
    if action == "correct" and not (d.get("corrected") or "").strip():
        return jsonify({"error": "correct requires a corrected value"}), 400
    saved = save_decision(page, iid, action, d.get("corrected"), d.get("note"))
    return jsonify({"ok": True, "id": iid, "decision": saved})


@app.get("/api/review/<int:page>/export")
def review_export(page: int):
    """The reviewed answers — what you would actually hand over."""
    summary, obs, regions, ctx = _load_run(page)
    if summary is None:
        return jsonify({"error": "no run"}), 404
    q = build_queue(summary, obs, regions, ctx)
    corrected, applied = apply_decisions(summary.get("answers", {}), q)
    return jsonify({"page": page, "sheet_id": summary.get("sheet_id"),
                    "document_id": summary.get("document_id"),
                    "model": summary.get("model"), "reviewed": applied,
                    "open_items": summarise(q)["open"], "answers": corrected})


# ---- rules: current rules, AI-proposed changes, accept / reject ----------------

@app.get("/api/rules")
def rules_get():
    page = request.args.get("page", type=int)
    doc = rules_store.load()
    return jsonify({"rules": doc.get("rules", []),
                    "settings": {k: v for k, v in doc.items() if k != "rules"},
                    "changelog": rules_store.changelog()[::-1],
                    "proposals": rule_assistant.load(page) if page else None})


@app.post("/api/rules/propose")
def rules_propose():
    """Ask the model what rules this sheet needs. Uses the page's last run."""
    d = request.get_json(force=True) or {}
    page = int(d.get("page", 0))
    summary, obs, _, ctx = _load_run(page)
    if summary is None or not obs:
        return jsonify({"error": f"run page {page} first — the check needs its observations"}), 400
    cats = [c.strip() for c in (d.get("categories") or []) if c and c.strip()]
    if not cats and summary.get("document_id") is not None:
        cats = list((load_key(summary["document_id"]) or {}).keys())
    unclaimed = resolve(obs, ctx)["unclaimed"]
    try:
        blob = rule_assistant.propose(page, ctx, obs, unclaimed, cats)
    except Exception as exc:  # noqa: BLE001 — surface it in the UI
        return jsonify({"error": f"{type(exc).__name__}: {exc}"[:300]}), 500
    return jsonify(blob)


@app.post("/api/rules/decide")
def rules_decide():
    d = request.get_json(force=True) or {}
    if d.get("action") not in ("accept", "reject"):
        return jsonify({"error": "action must be accept or reject"}), 400
    try:
        p = rule_assistant.decide(int(d["page"]), int(d["id"]), d["action"])
    except (ValueError, KeyError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "proposal": p})


@app.get("/api/tables/<int:page>/<int:idx>.csv")
def table_csv(page: int, idx: int):
    f = RUNS / f"page{page}_tables" / f"table{idx}.csv"
    if not f.exists():
        return jsonify({"error": "no such table"}), 404
    return send_file(f, mimetype="text/csv", as_attachment=True,
                     download_name=f"page{page}_table{idx}.csv")


@app.get("/api/run")
def api_run():
    """Server-sent events so stages light up as they finish."""
    page = int(request.args.get("page", 1))
    force = request.args.get("force") == "1"
    a = request.args
    cfg = RunConfig(model_b=a.get("model_b", ""), d_mode=a.get("d_mode", "single"),
                    model_d=a.get("model_d", ""),
                    light_a=a.get("light_a") or DEFAULT_LIGHT_PAIR[0],
                    light_b=a.get("light_b") or DEFAULT_LIGHT_PAIR[1],
                    flagship=a.get("flagship") or DEFAULT_FLAGSHIP,
                    model_t=a.get("model_t", ""),
                    cache_only=a.get("cache_only") == "1", model_v=a.get("model_v", ""))

    def stream():
        try:
            for ev in run(page, force=force, cfg=cfg):
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:  # surface failures in the UI, don't 500 silently
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5001))
    print(f"\n  Pipeline viewer →  http://127.0.0.1:{port}\n")
    app.run(port=port, debug=False, threaded=True)
