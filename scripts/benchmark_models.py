"""
Model benchmark — which model (or cascade) should run each AI layer?

    python scripts/benchmark_models.py d       # Layer D on the answer-keyed pages
    python scripts/benchmark_models.py b       # Layer B: legend / note-box quality, via downstream recall
    python scripts/benchmark_models.py t       # Layer T: table cells vs a checked reference
    python scripts/benchmark_models.py report  # markdown tables from saved results

Layer D results are written to the same cache files the pipeline uses, so any
benchmarked model can then be opened in the viewer without paying again.

Every Layer D configuration is scored with the SAME rules and the SAME Layer B
context, so the only thing that differs is the reader. Cost comes from each
run's own token counts and the prices in pipeline/models.py.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.detect import tile_grid  # noqa: E402
from pipeline.models import cost, provider_of  # noqa: E402
from pipeline.paths import BENCH, CACHE, RENDERS  # noqa: E402
from pipeline.resolve import resolve  # noqa: E402
from pipeline.run import RunConfig, _ctx_path, _obs_path  # noqa: E402
from pipeline.score import document_for_sheet, score  # noqa: E402

KEYED_PAGES = [1, 2]

D_CONFIGS = [
    {"name": "Claude Opus 5", "d_mode": "single", "model_d": "claude-opus-5"},
    {"name": "Claude Sonnet 5", "d_mode": "single", "model_d": "claude-sonnet-5"},
    {"name": "Claude Haiku 4.5", "d_mode": "single", "model_d": "claude-haiku-4-5"},
    {"name": "GPT-5.5", "d_mode": "single", "model_d": "gpt-5.5"},
    {"name": "GPT-5.4 mini", "d_mode": "single", "model_d": "gpt-5.4-mini"},
    {"name": "Gemini 3.1 Pro", "d_mode": "single", "model_d": "gemini-3.1-pro-preview"},
    {"name": "Gemini 3.8 Flash", "d_mode": "single", "model_d": "gemini-3.8-flash"},
    {"name": "Cascade: Sonnet 5 + Gemini Flash → Opus 5", "d_mode": "cascade",
     "light_a": "claude-sonnet-5", "light_b": "gemini-3.8-flash", "flagship": "claude-opus-5"},
    # No Anthropic spend. Same-family readers: agreement is a weaker signal
    # than across providers, which the results must be read with.
    {"name": "Cascade (Gemini only): Flash + 3.1 Pro → 3.1 Pro", "d_mode": "cascade",
     "light_a": "gemini-3.8-flash", "light_b": "gemini-3.1-pro-preview",
     "flagship": "gemini-3.1-pro-preview"},
]


def _cfg(c: dict) -> RunConfig:
    return RunConfig(**{k: v for k, v in c.items() if k != "name"})


def _ctx(page: int) -> dict:
    return json.load(open(_ctx_path(page, "claude-opus-5")))


def _regions(page: int) -> list:
    return json.load(open(CACHE / f"page{page}_regions.json"))


def _score(page: int, obs: list, ctx: dict) -> dict:
    doc = document_for_sheet(ctx.get("sheet_id"))
    r = resolve(obs, ctx)
    s = score(r["answers"], doc, layer_c_regions=_regions(page), observations=obs)
    t = s["totals"]
    return {"recall": t["recall"], "matched": t["matched"], "missed": t["missed"],
            "exact": t["exact_matched"], "extra": t["extra"],
            "coverage": (s.get("coverage") or {}).get("coverage"),
            "per_category": {c: v["recall"] for c, v in s["categories"].items() if v["expected"]},
            "missed_values": {c: v["missed"] for c, v in s["categories"].items() if v["missed"]}}


def _run_d(conf: dict, page: int) -> dict:
    cfg = _cfg(conf)
    path = _obs_path(page, cfg)
    ctx = _ctx(page)
    t0 = time.time()
    if path.exists():
        blob = json.load(open(path))
        obs, meta, reused = blob["observations"], blob["meta"], True
    else:
        img = Image.open(RENDERS / f"page{page}_full.png")
        img.load()
        tiles = tile_grid(*img.size)
        if cfg.d_mode == "cascade":
            from pipeline.cascade import run as cascade_run
            obs, rep = cascade_run(img, tiles, ctx, cfg.light_a, cfg.light_b, cfg.flagship)
            meta = {"mode": "cascade", "tiles_total": len(tiles), "tiles_failed": 0,
                    "cascade": {k: rep.get(k) for k in ("escalation", "merge", "cost", "stages")},
                    "cost_total": rep["cost"].get("total"), "unpriced": rep["cost"].get("unpriced")}
        else:
            from pipeline.observe import observe_page
            obs, u = observe_page(img, tiles, ctx, model=cfg.model_d)
            meta = {"tiles_total": len(tiles), "tiles_ok": u["calls"], "tiles_failed": u["errors"],
                    "error_messages": u["error_messages"],
                    "input_tokens": u["input_tokens"], "output_tokens": u["output_tokens"]}
        meta["model"] = cfg.d_label()
        meta["seconds"] = round(time.time() - t0, 1)
        if obs:
            json.dump({"observations": obs, "meta": meta}, open(path, "w"), indent=1)
        reused = False
    c = meta.get("cost_total") if cfg.d_mode == "cascade" else cost(cfg.model_d, meta)
    return {"config": conf["name"], "page": page, "observations": len(obs),
            "tiles_failed": meta.get("tiles_failed", 0), "errors": meta.get("error_messages", [])[:2],
            "cost": c, "seconds": meta.get("seconds"), "reused_cache": reused,
            "cascade": meta.get("cascade", {}).get("escalation") if cfg.d_mode == "cascade" else None,
            **(_score(page, obs, ctx) if obs else {"recall": None})}


def rescore_d() -> list[dict]:
    """Score every saved Layer D result with the CURRENT rules, keeping the cost
    and time measured when it ran — so a rule fix is reflected for every model."""
    f = BENCH / "models_layer_d.json"
    prev = {(r["config"], r["page"]): r for r in json.load(open(f))} if f.exists() else {}
    out = []
    for c in D_CONFIGS:
        for p in KEYED_PAGES:
            path = _obs_path(p, _cfg(c))
            old = prev.get((c["name"], p), {})
            if not path.exists():
                if old:
                    out.append(old)
                continue
            blob = json.load(open(path))
            meta = blob["meta"]
            cfg = _cfg(c)
            # cost and time come from the run's own stored meta, so a stopped
            # benchmark loses nothing that finished
            cst = meta.get("cost_total") if cfg.d_mode == "cascade" else cost(cfg.model_d, meta)
            out.append({**old, "config": c["name"], "page": p,
                        "observations": len(blob["observations"]),
                        "cost": cst, "seconds": meta.get("seconds", old.get("seconds")),
                        "tiles_failed": meta.get("tiles_failed", 0),
                        "cascade": meta.get("cascade", {}).get("escalation") if cfg.d_mode == "cascade" else None,
                        **_score(p, blob["observations"], _ctx(p))})
    json.dump(out, open(BENCH / "models_layer_d.json", "w"), indent=1)
    return out


def bench_d(only: str | None = None) -> list[dict]:
    """Providers run side by side; within one provider, one config at a time
    (so rate limits are not shared between two of our own runs)."""
    groups: dict[str, list] = {}
    for c in D_CONFIGS:
        if only and only.lower() not in c["name"].lower():
            continue
        key = "cascade" if c["d_mode"] == "cascade" else provider_of(c["model_d"])
        groups.setdefault(key, []).append(c)

    results: list[dict] = []

    def work(confs):
        out = []
        for c in confs:
            for p in KEYED_PAGES:
                print(f"  start {c['name']} · page {p}", flush=True)
                try:
                    r = _run_d(c, p)
                except Exception as exc:  # noqa: BLE001 — record and move on
                    r = {"config": c["name"], "page": p, "recall": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
                print(f"  done  {c['name']} · page {p} · recall {r.get('recall')} · "
                      f"${(r.get('cost') or 0):.2f} · {r.get('seconds')}s", flush=True)
                out.append(r)
        return out

    with cf.ThreadPoolExecutor(max_workers=len(groups)) as ex:
        for got in ex.map(work, groups.values()):
            results.extend(got)
    if only:      # merge into the existing table rather than replacing it
        f = BENCH / "models_layer_d.json"
        keep = [r for r in (json.load(open(f)) if f.exists() else [])
                if (r["config"], r["page"]) not in {(x["config"], x["page"]) for x in results}]
        results = keep + results
    json.dump(results, open(BENCH / "models_layer_d.json", "w"), indent=1)
    return results


# ---- Layer B -------------------------------------------------------------------

B_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "gpt-5.5", "gemini-3.1-pro-preview"]


def bench_b() -> list[dict]:
    """Each model reads the legend of every page. Quality is judged where it
    matters: the sheet id, and recall when Layer E uses that model's context on
    the same (Opus) observations — bad note boxes or legend codes show up there."""
    from pipeline import stats
    from pipeline.sheet_context import sheet_context
    from pipeline.run import _slug
    out = []
    prev_f = BENCH / "models_layer_b.json"
    prev = {(r["model"], r["page"]): r for r in json.load(open(prev_f))} if prev_f.exists() else {}
    for m in B_MODELS:
        for p in (1, 2, 3):
            path = CACHE / f"page{p}_context__{_slug(m)}.json"
            stats.reset()
            t0 = time.time()
            try:
                if path.exists():          # re-score from cache; keep what the read cost
                    ctx = json.load(open(path))
                    c, secs = prev.get((m, p), {}).get("cost"), prev.get((m, p), {}).get("seconds")
                else:
                    img = Image.open(RENDERS / f"page{p}_full.png")
                    ctx, u = sheet_context(img, m)
                    json.dump(ctx, open(path, "w"), indent=1)
                    c, secs = cost(m, u), round(time.time() - t0, 1)
            except Exception as exc:  # noqa: BLE001
                out.append({"model": m, "page": p, "error": str(exc)[:200]})
                continue
            row = {"model": m, "page": p, "sheet_id": ctx.get("sheet_id"),
                   "symbols": len(ctx.get("symbols", [])), "codes": [x["code"] for x in ctx.get("system_prefixes", [])],
                   "suppress_zones": len(ctx.get("suppress_regions", [])), "cost": c, "seconds": secs}
            doc = document_for_sheet(ctx.get("sheet_id"))
            row["sheet_id_matches_key"] = doc is not None
            if p in KEYED_PAGES:
                obs_f = _obs_path(p, RunConfig(model_d="claude-opus-5"))
                if obs_f.exists() and doc is not None:
                    obs = json.load(open(obs_f))["observations"]
                    row["recall_with_this_context"] = score(resolve(obs, ctx)["answers"], doc)["totals"]["recall"]
            out.append(row)
            print(f"  B {m} page {p}: {row.get('sheet_id')} · recall {row.get('recall_with_this_context')}", flush=True)
    json.dump(out, open(BENCH / "models_layer_b.json", "w"), indent=1)
    return out


# ---- Layer T -------------------------------------------------------------------

T_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "gpt-5.5", "gemini-3.1-pro-preview",
            "gemini-3.8-flash"]


def _cell(c) -> str:
    return " ".join(str(c).split()).upper()


def _align_score(gold: dict, got_rows: list) -> dict:
    """Cell-exact score that survives an inserted blank row or a dropped row:
    each gold row is matched to the model row sharing the most cells (each
    model row used once). Header and data cells are counted separately, and a
    V<->Y swap is counted on its own because it is the known glyph confusion."""
    rows = [[_cell(c) for c in r] for r in got_rows if any(str(c).strip() for c in r)]
    used, out = set(), {"data_cells": 0, "data_exact": 0, "header_cells": 0, "header_exact": 0, "v_y_swaps": 0}
    for gi, grow in enumerate(gold["rows"]):
        g = [_cell(c) for c in grow]
        best, bi = -1, None
        for ri, r in enumerate(rows):
            if ri in used:
                continue
            # Pair rows on NON-EMPTY cells only: two rows that merely share
            # blanks are not the same row (a split header once stole a data row).
            hits = sum(1 for k, c in enumerate(g) if c and k < len(r) and r[k] == c)
            if hits > best and hits > 0:
                best, bi = hits, ri
        if bi is not None:
            used.add(bi)
        r = rows[bi] if bi is not None else []
        kind = "header" if gi < gold["header_rows"] else "data"
        for k, c in enumerate(g):
            out[f"{kind}_cells"] += 1
            got = r[k] if k < len(r) else None
            out[f"{kind}_exact"] += got == c
            if got and got != c and got.replace("Y", "V") == c.replace("Y", "V"):
                out["v_y_swaps"] += 1
    return out


T_RUNS = 2       # read every table twice per model: is the same model consistent?


def bench_t(rescore_only: bool = False) -> list[dict]:
    """Every model reads the same three page-3 table crops (boxes fixed, so only
    the reading differs), twice. Scored against a hand-checked reference
    (data/bench/page3_tables_gold.json), cell by cell with row alignment."""
    from pipeline import stats, tables as T
    gold = json.load(open(BENCH / "page3_tables_gold.json"))["tables"]
    boxes = json.load(open(CACHE / "page3_tables__claude-opus-5.json"))["tables"]
    img = Image.open(RENDERS / "page3_full.png")
    img.load()
    outdir = BENCH / "layer_t_outputs"
    outdir.mkdir(exist_ok=True)
    results = []
    for m in T_MODELS:
        stats.reset()
        t0 = time.time()
        runs = []
        prev = next((r for r in json.load(open(BENCH / "models_layer_t.json")) if r["model"] == m), {}) \
            if rescore_only and (BENCH / "models_layer_t.json").exists() else {}
        if rescore_only:
            runs = json.load(open(outdir / f"{m}.json"))
        for run_i in range(0 if rescore_only else T_RUNS):
            got = []
            for tb in boxes:
                try:
                    got.append(T.read_table(img, {"title": tb["title"], "bbox": tb["bbox"]}, tb["index"], m))
                except Exception as exc:  # noqa: BLE001
                    got.append({"index": tb["index"], "rows": [], "error": str(exc)[:160]})
            runs.append(got)
        if not rescore_only:
            json.dump(runs, open(outdir / f"{m}.json", "w"), indent=1)
        per_run = []
        for got in runs:
            tot: dict = {}
            for g, o in zip(gold, got):
                for k, v in _align_score(g, o["rows"]).items():
                    tot[k] = tot.get(k, 0) + v
            per_run.append(tot)
        # consistency: does run 2 reproduce run 1, cell for cell (data rows)?
        same = cells = 0
        for g, a, b in zip(gold, runs[0], runs[1]):
            ref = {"rows": [r for r in a["rows"] if any(str(c).strip() for c in r)], "header_rows": 0}
            sc = _align_score(ref, b["rows"]) if ref["rows"] else {"data_cells": 0, "data_exact": 0}
            same += sc["data_exact"]
            cells += sc["data_cells"]
        st = stats.snapshot()
        row = {"model": m, "runs": per_run,
               "data_exact": [r["data_exact"] for r in per_run], "data_cells": per_run[0]["data_cells"],
               "header_exact": [r["header_exact"] for r in per_run], "header_cells": per_run[0]["header_cells"],
               "v_y_swaps": [r["v_y_swaps"] for r in per_run],
               "run_to_run_agreement": round(same / cells, 3) if cells else None,
               "cost_per_run": prev.get("cost_per_run") if rescore_only else round(st["total_cost"] / T_RUNS, 4),
               "unpriced_calls": prev.get("unpriced_calls", 0) if rescore_only else st["unpriced_calls"],
               "seconds_per_run": prev.get("seconds_per_run") if rescore_only else round((time.time() - t0) / T_RUNS, 1),
               "errors": [o.get("error") for got in runs for o in got if o.get("error")]}
        results.append(row)
        print(f"  T {m}: data cells exact {row['data_exact']} of {row['data_cells']} · "
              f"V/Y swaps {row['v_y_swaps']} · run-to-run {row['run_to_run_agreement']} · "
              f"${row['cost_per_run']}/run", flush=True)
    json.dump(results, open(BENCH / "models_layer_t.json", "w"), indent=1)
    return results


# ---- report -----------------------------------------------------------------------

def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def report() -> str:
    lines = ["# Model benchmark", ""]
    f = BENCH / "models_layer_d.json"
    if f.exists():
        rows = json.load(open(f))
        by: dict[str, dict] = {}
        for r in rows:
            by.setdefault(r["config"], {})[r["page"]] = r
        lines += ["## Layer D (reader) — Docs 1–2, same rules, same Layer B context", "",
                  "| Setup | Doc 1 recall | Doc 2 recall | Both (matched/total) | Exact matches | Coverage (Doc 1) | Cost (both pages) | Time (both pages) | Failed tiles |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for name, pr in by.items():
            m = sum(p.get("matched", 0) or 0 for p in pr.values())
            tot = sum((p.get("matched", 0) or 0) + (p.get("missed", 0) or 0) for p in pr.values())
            costs = [p.get("cost") for p in pr.values()]
            cst = "unpriced" if any(c is None for c in costs) else f"${sum(costs):.2f}"
            secs = [p.get("seconds") for p in pr.values() if p.get("seconds")]
            lines.append(f"| {name} | {_pct(pr.get(1, {}).get('recall'))} | {_pct(pr.get(2, {}).get('recall'))} | "
                         f"{m}/{tot} | {sum(p.get('exact', 0) or 0 for p in pr.values())} | "
                         f"{_pct(pr.get(1, {}).get('coverage'))} | {cst} | "
                         f"{(str(round(sum(secs))) + 's') if secs else '—'} | "
                         f"{sum(p.get('tiles_failed', 0) or 0 for p in pr.values())} |")
        lines.append("")
    f = BENCH / "models_layer_b.json"
    if f.exists():
        rows = json.load(open(f))
        lines += ["## Layer B (sheet context)", "",
                  "| Model | Sheet ids right (3 pages) | Doc 1 recall with its context | Doc 2 recall with its context | Cost (3 pages) |",
                  "| --- | --- | --- | --- | --- |"]
        by = {}
        for r in rows:
            by.setdefault(r["model"], []).append(r)
        for m, rs in by.items():
            ok = sum(1 for r in rs if r.get("sheet_id_matches_key") or (r["page"] == 3 and "LR20794" in str(r.get("sheet_id"))))
            rec = {r["page"]: r.get("recall_with_this_context") for r in rs}
            cs = [r.get("cost") for r in rs]
            cst = "—" if all(c is None for c in cs) else f"${sum(c or 0 for c in cs):.2f}"
            lines.append(f"| {m} | {ok}/3 | {_pct(rec.get(1))} | {_pct(rec.get(2))} | {cst} |")
        lines.append("")
    f = BENCH / "models_layer_t.json"
    if f.exists():
        rows = json.load(open(f))
        lines += ["## Layer T (tables) — page 3, 3 tables, read twice per model",
                  "", "Scored against a hand-checked reference; rows aligned so one inserted blank row does not shift everything.", "",
                  "| Model | Data cells exact (run 1 / run 2, of 62) | Header cells exact | V↔Y swaps | Same answer both runs | Cost per run | Time per run |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in rows:
            cst = "unpriced" if r.get("unpriced_calls") else f"${r['cost_per_run']:.3f}"
            lines.append(f"| {r['model']} | {' / '.join(map(str, r['data_exact']))} | "
                         f"{' / '.join(map(str, r['header_exact']))} of {r['header_cells']} | "
                         f"{' / '.join(map(str, r['v_y_swaps']))} | {_pct(r['run_to_run_agreement'])} | {cst} | {r['seconds_per_run']}s |")
        lines.append("")
    text = "\n".join(lines)
    (BENCH / "benchmark.md").write_text(text)
    return text


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"d": lambda: bench_d(sys.argv[2] if len(sys.argv) > 2 else None), "d-rescore": rescore_d, "b": bench_b, "t": bench_t,
     "t-rescore": lambda: bench_t(rescore_only=True)}.get(what, lambda: None)()
    print(report())
