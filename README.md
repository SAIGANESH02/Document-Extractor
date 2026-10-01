# Xmentium — structured extraction from scanned engineering drawings

Takes a scanned P&ID page and emits answer-key-shaped JSON
(`{document_id, answers: {category: [values]}}`), with every value traceable
to the pixels and the rule that produced it.

## Pipeline

| Layer | File | What it does |
| --- | --- | --- |
| A — Ingest | `pipeline/render.py`, `pipeline/detect.py:tile_grid` | PDF → native-resolution page image (text-layer probe recorded); page → 15 overlapping 1250 px tiles |
| B — Sheet context | `pipeline/sheet_context.py` | One vision call on the whole sheet: drawing number, legend symbols, system codes, note/legend/title boxes |
| C — Detection | `pipeline/detect.py` | PaddleOCR text detector on CPU — finds every text region; used to measure what the AI missed |
| D — Observe | `pipeline/observe.py` | One vision call per tile: text, symbol shape, what it is attached to, box. Never categories |
| D (alt) — Cascade | `pipeline/cascade.py` | Two light models from different providers; a flagship settles their disagreements |
| T — Tables | `pipeline/tables.py` | Any sheet: find data tables, read each cell by cell from a full-resolution crop → CSV + Markdown |
| E — Resolve | `pipeline/resolve.py` + `pipeline/RULES.yaml` | Deterministic: confidence floor, drop text in note boxes, dedupe seams, map observations to categories by rule |
| Score | `pipeline/score.py` | Recall vs key (headline), key precision (lower bound), coverage vs Layer C, exact vs after-cleanup matches |
| Review | `pipeline/review.py` | Human queue: contested → solo → suppressed → unread → not in key |
| Rules assistant | `pipeline/rule_assistant.py`, `rules_store.py` | AI proposes rule changes for a new sheet; each is test-run on the keyed sheets; a person accepts or rejects; versioned |

C, D and T run in parallel after B. See `WRITEUP.md` for the approach, results and open questions.

All model calls go through `pipeline/llm.py:ask_json` (Anthropic, Gemini or OpenAI).

## Run

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                     # add API keys; LLM_PROVIDER=anthropic|gemini|openai
.venv/bin/python web/server.py           # http://127.0.0.1:5001  (PORT=... to change)
```

In the viewer: **Upload PDF** to add a document, **Run** to process a page
(cached stages are reused and labelled with when they were made),
**Re-run fresh** to ignore all caches.

From the command line — the whole pipeline in one command:

```bash
.venv/bin/python scripts/extract.py doc4.pdf        # render + run every page → out/page{N}/answers.json
.venv/bin/python scripts/extract.py --page 1 --fresh --d-model claude-sonnet-5
.venv/bin/python scripts/extract.py --page 1 --cascade
.venv/bin/python scripts/benchmark_models.py d|b|t|report   # model comparison per layer
.venv/bin/python scripts/render_pdf.py doc.pdf     # PDF → data/renders/page{N}_full.png
.venv/bin/python scripts/report.py --history       # per-category recall, run-by-run trend
.venv/bin/python scripts/benchmark.py --pages 1 2 3 --read   # Layer C benchmark
```

## Adapting to a new document

Category mapping lives in `pipeline/RULES.yaml`. Layer D's observations are
cached, so a rule edit re-runs only Layer E — milliseconds, no model calls.

## Layout

```
pipeline/   the layers, one file each, plus RULES.yaml and paths.py
web/        Flask server + static front end (index.html, app.js, style.css)
scripts/    render_pdf.py, report.py, benchmark.py
data/       generated: renders, caches, run records, uploads (gitignored)
```
