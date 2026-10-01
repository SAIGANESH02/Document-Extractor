"""
Scorer — §8.

Reports four numbers, because plain precision/recall is actively misleading
here. The answer keys are sparse spot-checks (§2.5): Doc 1 lists 2 valves where
the sheet shows 14+. An extractor that correctly finds all 14 scores 14%
precision; one that finds only the 2 listed scores 100%. That metric rewards
the worse system.

  recall           — of the key's values, how many did we find? SOUND, and the
                     headline number, because the key is a subset of truth.
  coverage         — of the regions the independent detector (Layer C) found,
                     how many did the pipeline account for? A recall signal
                     against the PIXELS rather than against a sparse sample.
                     Paired with a false-positive count, because raw region
                     count alone is misleading (the Layer C benchmark: a faster detector reported more regions and was worse).
  key_precision    — standard precision, a LOWER BOUND depressed by key
                     sparsity. Track it for regressions; never read it as an
                     absolute.
  extra            — values we emitted that the key does not list. These are
                     NOT errors; they are the manual-adjudication queue.
"""

from __future__ import annotations

import json
import re

from pipeline.paths import ANSWER_KEY as KEY_PATH


def _norm(s: str) -> str:
    """Compare on alphanumerics only — the key writes PID-1-SS_LR20519 where
    the title block prints PID-1-SS-LR20519 (§Layer B finding 1)."""
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def load_key(document_id: int) -> dict | None:
    if not KEY_PATH.exists():
        return None
    for d in json.load(open(KEY_PATH)).get("documents", []):
        if d.get("document_id") == document_id:
            return d.get("answers", {})
    return None


def document_for_sheet(sheet_id: str | None) -> int | None:
    """Map a sheet's own drawing number to a document_id in the key.

    Page order is not identity. Doc 4 arrives unseen and is in no key at all,
    and a key keyed by page number would silently score it against whatever
    document happened to sit at that index. Matching on the sheet's printed
    PID means an unknown sheet scores as `has_key: false` — which is the
    honest answer — instead of against the wrong key.
    """
    if not sheet_id or not KEY_PATH.exists():
        return None
    want = _norm(sheet_id)
    for d in json.load(open(KEY_PATH)).get("documents", []):
        if _norm(d.get("answers", {}).get("pid", "")) == want:
            return d.get("document_id")
    return None


def score(answers: dict, document_id: int, layer_c_regions: list | None = None,
          observations: list | None = None) -> dict:
    key = load_key(document_id)
    out: dict = {"has_key": key is not None, "categories": {}, "totals": {}}

    if key:
        tp = fn = extra_n = cleanup_n = 0
        for cat, expected in key.items():
            exp = [expected] if isinstance(expected, str) else list(expected)
            got_raw = answers.get(cat, [])
            got = [got_raw] if isinstance(got_raw, str) else list(got_raw)
            gset = {_norm(g) for g in got}
            found = [e for e in exp if _norm(e) in gset]
            missed = [e for e in exp if _norm(e) not in gset]
            eset = {_norm(e) for e in exp}
            extra = [g for g in got if _norm(g) not in eset]
            # A match that only holds after dropping punctuation/spaces is still
            # a match, but a strict grader would miss it — so count it apart.
            raw = {str(g).strip() for g in got}
            cleanup_only = [e for e in found if str(e).strip() not in raw]
            tp += len(found); fn += len(missed); extra_n += len(extra)
            cleanup_n += len(cleanup_only)
            out["categories"][cat] = {
                "expected": exp, "got": got, "found": found,
                "missed": missed, "extra": extra, "cleanup_only": cleanup_only,
                "recall": round(len(found) / len(exp), 3) if exp else None,
            }
        for cat in answers:
            if cat not in key:
                v = answers[cat]
                out["categories"][cat] = {
                    "expected": [], "got": [v] if isinstance(v, str) else v,
                    "found": [], "missed": [], "cleanup_only": [],
                    "extra": [v] if isinstance(v, str) else v, "recall": None,
                }
        out["totals"] = {
            "recall": round(tp / (tp + fn), 3) if (tp + fn) else None,
            "key_precision": round(tp / (tp + extra_n), 3) if (tp + extra_n) else None,
            "matched": tp, "missed": fn, "extra": extra_n,
            "exact_matched": tp - cleanup_n, "cleanup_only_matched": cleanup_n,
            "note": ("key_precision is a LOWER BOUND — the answer key is a sparse "
                     "spot-check, so a correct extraction absent from the key "
                     "counts against it. See §8."),
        }

    # coverage: Layer C regions accounted for by Layer D observations
    if layer_c_regions is not None and observations is not None:
        def iou(a, b):
            ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
            ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
            inter = max(0, ix1-ix0) * max(0, iy1-iy0)
            if not inter:
                return 0.0
            ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
            return inter / ua if ua else 0.0

        obb = [o["bbox"] for o in observations]
        matched = sum(1 for r in layer_c_regions if any(iou(r["bbox"], o) > 0.3 for o in obb))
        n = len(layer_c_regions)
        unmatched_obs = sum(1 for o in obb
                            if not any(iou(r["bbox"], o) > 0.3 for r in layer_c_regions))
        out["coverage"] = {
            "detector_regions": n,
            "observations": len(obb),
            "matched": matched,
            "coverage": round(matched / n, 3) if n else None,
            "observations_without_detection": unmatched_obs,
            "note": ("Low coverage = Layer D missed regions the detector saw. "
                     "High observations_without_detection = Layer D saw things the "
                     "detector didn't; check a sample for hallucination."),
        }
    return out
