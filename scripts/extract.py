"""
The pipeline in one command: document in, answer-key-shaped JSON out.

    python scripts/extract.py doc4.pdf                 # render the PDF, run every page
    python scripts/extract.py sheet.png                # an image works too (PNG / JPG / TIFF)
    python scripts/extract.py --page 3                 # an already-rendered page
    python scripts/extract.py --page 1 --fresh         # ignore all cached results
    python scripts/extract.py --page 1 --d-model claude-sonnet-5
    python scripts/extract.py --page 1 --cascade       # Layer D as two light models + flagship

Writes, per page, to out/page{N}/:
    answers.json    {"document_id": ..., "answers": {category: [values]}}  — the deliverable
    unclassified.json  tag-like readings no rule claimed — where an unknown category shows up
    tables/         one CSV per table + tables.md (any sheet with tables)
    score.json      recall etc. when the sheet matches an answer key
    run.json        stage timings, model calls, cost
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import tables as T  # noqa: E402
from pipeline.models import DEFAULT_FLAGSHIP, DEFAULT_LIGHT_PAIR  # noqa: E402
from pipeline.render import ingest  # noqa: E402
from pipeline.run import RunConfig, run  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "out"


def run_page(page: int, cfg: RunConfig, fresh: bool, document_id: int | None, quiet: bool) -> dict:
    t0, summary, last_note = time.time(), None, {}
    for ev in run(page, force=fresh, cfg=cfg):
        if ev["type"] == "stage" and not quiet:
            s = ev["stage"]
            line = f"{s['status']:8} {s['note']}"
            if last_note.get(s["id"]) != line:
                last_note[s["id"]] = line
                print(f"  [{time.time() - t0:6.1f}s] {s['id']} {line}", flush=True)
        elif ev["type"] == "done":
            summary = ev["summary"]
    out = OUT / f"page{page}"
    (out / "tables").mkdir(parents=True, exist_ok=True)
    doc_id = document_id if document_id is not None else summary.get("document_id")
    answers = {"document_id": doc_id if doc_id is not None else f"page{page}",
               "answers": summary.get("answers", {})}
    json.dump(answers, open(out / "answers.json", "w"), indent=2)
    json.dump(summary.get("unclassified", []), open(out / "unclassified.json", "w"), indent=1)
    for tb in summary.get("tables", []):
        (out / "tables" / f"table{tb['index']}.csv").write_text(T.to_csv(tb))
    if summary.get("tables"):
        (out / "tables" / "tables.md").write_text("\n".join(T.to_markdown(tb) for tb in summary["tables"]))
    if (summary.get("score") or {}).get("has_key"):
        json.dump(summary["score"], open(out / "score.json", "w"), indent=1)
    st = summary.get("stats", {})
    json.dump({"config": summary.get("config"), "sheet_id": summary.get("sheet_id"),
               "observations": summary.get("observations"), "audit": summary.get("audit"),
               "coverage": summary.get("coverage"),
               "stats": {k: v for k, v in st.items() if k != "calls"}},
              open(out / "run.json", "w"), indent=1)
    t = (summary.get("score") or {}).get("totals") or {}
    print(f"page {page}: {summary.get('sheet_id')} · {len(answers['answers'])} categories · "
          f"{len(summary.get('tables', []))} table(s) · "
          + (f"recall {t.get('recall')} " if t else "no answer key · ")
          + f"· ${st.get('total_cost', 0):.2f} in {time.time() - t0:.0f}s → {out}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="?", help="PDF or PNG/JPG/TIFF to ingest and run (every page)")
    ap.add_argument("--page", type=int, nargs="+", help="already-rendered page number(s)")
    ap.add_argument("--fresh", action="store_true", help="ignore cached results for every layer")
    ap.add_argument("--b-model", default="")
    ap.add_argument("--d-model", default="")
    ap.add_argument("--t-model", default="")
    ap.add_argument("--cascade", action="store_true")
    ap.add_argument("--light", nargs=2, default=list(DEFAULT_LIGHT_PAIR), metavar=("A", "B"))
    ap.add_argument("--flagship", default=DEFAULT_FLAGSHIP)
    ap.add_argument("--document-id", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    pages = list(a.page or [])
    if a.pdf:
        for m in ingest(a.pdf):
            print(f"rendered page{m['page']}: {m['width']}×{m['height']} ({m['method']}), "
                  f"text layer {m['text_layer_chars']} chars")
            pages.append(m["page"])
    if not pages:
        ap.error("give a PDF or --page N")
    cfg = RunConfig(model_b=a.b_model, model_d=a.d_model, model_t=a.t_model,
                    d_mode="cascade" if a.cascade else "single",
                    light_a=a.light[0], light_b=a.light[1], flagship=a.flagship)
    for pg in pages:
        run_page(pg, cfg, a.fresh, a.document_id, a.quiet)


if __name__ == "__main__":
    main()
