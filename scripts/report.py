"""
Compare saved runs against the answer key.

    python scripts/report.py            # every page with a saved run
    python scripts/report.py 1 2        # only these pages

Reads data/runs/page{N}_latest.json (written by every finished pipeline run) and
grades it against answer_key.json. Nothing here re-runs the pipeline or calls
the API, so it is safe to run as often as you like.

WHY A SEPARATE TOOL. The scores already stream to the viewer, but they vanish
with the page. Answering "did that RULES.yaml change actually help, or did it
just move errors around?" needs the runs side by side — which needs them on
disk. `--history` does exactly that comparison.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.paths import RUNS  # noqa: E402

BOLD, DIM, OK, BAD, WARN, END = "\033[1m", "\033[2m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _pct(v) -> str:
    return "  —  " if v is None else f"{v * 100:5.1f}%"


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def load_runs(pages: list[int] | None = None) -> list[dict]:
    out = []
    for f in sorted(RUNS.glob("page*_latest.json")):
        r = json.load(open(f))
        if pages and r.get("page") not in pages:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r.get("page", 0))


def report_page(run: dict) -> None:
    pg, doc = run.get("page"), run.get("document_id")
    print(f"\n{BOLD}{'=' * 78}{END}")
    print(f"{BOLD}PAGE {pg}{END}  sheet {run.get('sheet_id')}  "
          f"type {run.get('sheet_type')}  run {run.get('run_at')}")

    meta = run.get("layer_d_meta") or {}
    if meta.get("tiles_failed"):
        print(f"{BAD}  ⚠ Layer D was PARTIAL: {meta['tiles_failed']}/{meta.get('tiles_total')} "
              f"tiles failed. Numbers below understate the pipeline.{END}")

    sc = run.get("score") or {}
    if not sc.get("has_key"):
        print(f"{WARN}  No answer key for this sheet — nothing to grade against.{END}")
        print(f"  Pipeline still produced {len(run.get('answers', {}))} categories "
              f"from {run.get('observations')} observations.")
        _pipeline_health(run)
        return

    t = sc.get("totals") or {}
    print(f"\n  {BOLD}recall {_pct(t.get('recall'))}{END}   "
          f"matched {t.get('matched')}   missed {t.get('missed')}   "
          f"not-in-key {t.get('extra')}   key-precision {_pct(t.get('key_precision'))}")
    _pipeline_health(run)

    print(f"\n  {BOLD}{'category':40} {'recall':>7}  detail{END}")
    for cat, c in sorted((sc.get("categories") or {}).items()):
        if not c.get("expected"):
            continue                       # emitted but not in the key; listed below
        rec = c.get("recall")
        col = OK if rec == 1.0 else (BAD if not rec else WARN)
        print(f"  {cat[:40]:40} {col}{_pct(rec)}{END}", end="")
        if c.get("missed"):
            print(f"  missed {BAD}{', '.join(map(str, c['missed']))[:60]}{END}")
        else:
            print("  ✓")

    noise = {c: v for c, v in (sc.get("categories") or {}).items()
             if not v.get("expected") and v.get("got")}
    if noise:
        print(f"\n  {DIM}emitted but absent from the key (adjudication queue, not errors):{END}")
        for cat, v in sorted(noise.items()):
            print(f"    {cat[:40]:40} {len(_as_list(v['got']))} values")


def _pipeline_health(run: dict) -> None:
    sc = run.get("score") or {}
    cov = sc.get("coverage") or {}
    a = run.get("audit") or {}
    print(f"  {DIM}observations {run.get('observations')} → kept {a.get('resolved')} "
          f"({a.get('suppressed')} suppressed, {a.get('duplicates')} deduped) · "
          f"detector regions {cov.get('detector_regions')} · "
          f"seen-by-both {cov.get('matched')}{END}")


def history(page: int) -> None:
    runs = sorted(RUNS.glob(f"page{page}_2*.json"))
    if len(runs) < 2:
        print(f"\nPage {page}: need at least 2 saved runs to compare (have {len(runs)}).")
        return
    print(f"\n{BOLD}PAGE {page} — run history{END}")
    print(f"  {'run':18} {'recall':>8} {'matched':>8} {'missed':>7} {'extra':>7} {'obs':>6}")
    prev = None
    for f in runs:
        r = json.load(open(f))
        t = (r.get("score") or {}).get("totals") or {}
        rec = t.get("recall")
        arrow = ""
        if prev is not None and rec is not None:
            d = rec - prev
            arrow = f"  {OK}▲{d:+.3f}{END}" if d > 0 else (f"  {BAD}▼{d:+.3f}{END}" if d < 0 else "  ·")
        print(f"  {r.get('run_at','?'):18} {_pct(rec):>8} {str(t.get('matched')):>8} "
              f"{str(t.get('missed')):>7} {str(t.get('extra')):>7} "
              f"{str(r.get('observations')):>6}{arrow}")
        if rec is not None:
            prev = rec


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    pages = [int(a) for a in args] if args else None
    runs = load_runs(pages)
    if not runs:
        print(f"No saved runs in {RUNS}. Run a page in the viewer first.")
        return
    for r in runs:
        report_page(r)

    graded = [r for r in runs if (r.get("score") or {}).get("has_key")]
    if graded:
        print(f"\n{BOLD}{'=' * 78}{END}\n{BOLD}ACROSS ALL GRADED SHEETS{END}")
        m = sum((r["score"]["totals"].get("matched") or 0) for r in graded)
        mi = sum((r["score"]["totals"].get("missed") or 0) for r in graded)
        ex = sum((r["score"]["totals"].get("extra") or 0) for r in graded)
        print(f"  recall {_pct(m / (m + mi) if (m + mi) else None)}  "
              f"({m} matched, {mi} missed, {ex} not-in-key) over {len(graded)} sheets")
    if "--history" in sys.argv:
        for r in runs:
            history(r["page"])


if __name__ == "__main__":
    main()
