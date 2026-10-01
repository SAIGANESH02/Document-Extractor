"""
Layer D cascade — two cheap readers, one expensive adjudicator.

THE IDEA. Two light models from DIFFERENT providers read every tile. Where
they agree, the reading is accepted. Where they disagree, the tile is re-read
by a flagship model, whose answer wins.

WHY DISAGREEMENT AND NOT CONFIDENCE. The obvious design routes on the model's
own confidence score. Measured on page 1, that does not work: values the
answer key confirms CORRECT averaged 0.895 confidence, values not in the key
averaged 0.864, and the observations we know are junk scored 0.85-0.88 —

    conf=0.88  "11.CONTINUATIONCONNECTION,SEEFP-74002."
    conf=0.88  "INDICATESSYSTEMBOUNDARYINTE"

Only 2 of 707 observations fell below the 0.35 floor. Models bluff at 0.88, so
a confidence threshold escalates almost nothing and never the wrong answers.

Cross-model disagreement carries real information because the errors are not
correlated. On page 1, Opus scored 2/2 on `filters` and 0/1 on `sample_sinks`;
GPT-5.5 scored 0/2 and 1/1 — disjoint blind spots, and the union of the two
outscored either alone (0.879 vs 0.848 / 0.758).

WHAT ESCALATION COSTS, AND WHY IT IS TARGETED. Measured on page 1 (Opus vs
GPT-5.5): the two agreed on only 50.4% of readings, and EVERY ONE of the 15
tiles held at least one dispute. Re-reading whole tiles blind would therefore
escalate 100% of the page and cost strictly more than a single flagship pass.

Two things make it cheap instead:

  1. Only disputes that could change an ANSWER are escalated — text outside
     the note/legend blocks, short enough to be a tag rather than prose.
     Disagreements inside a notes block ("2. PIPING IS IN SCOPE FOR...") are
     suppressed by Layer E regardless, so adjudicating them buys nothing.

     The length bound is 40 characters, not 12. A tighter "tag-shaped" filter
     excluded real answer-key values — '1-SS-FLEX HOSE NO. 3' (20 chars),
     'PRIMARY SAMPLE PANEL CP-166A' (28) — and those were exactly the
     categories that regressed when this ran the first time.

  2. The flagship is asked ONLY about those disputes, one call per affected
     tile. Output tokens are ~90% of the bill, so a tile re-read that returns
     a handful of adjudications instead of ~90 fresh observations costs a
     fraction of a blind re-read, while the model still sees full tile context.

The disputes this surfaces on page 1 are exactly the answer-key values recall
was failing on — CP-166A vs CP-166B, PCV2847 vs FCV2847, V835 vs V838,
PI 2793 vs "compound gauge".
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import time
from typing import Callable

from PIL import Image

from pipeline.observe import observe_page
from pipeline.models import cost, provider_of, validate_pair

# Two observations are "the same thing seen twice" if their boxes overlap this
# much. Deliberately loose: different models draw different boxes around one
# tag (one boxes the glyphs, one boxes the symbol too), and a tight threshold
# would call every shared reading a disagreement.
SAME_THING_IOU = 0.20

# The longest value in the answer keys is 28 characters; 40 leaves headroom
# without letting a paragraph of note prose through.
MAX_ANSWER_LEN = 40


def is_taglike(text: str) -> bool:
    """Could this string plausibly BE an answer value?

    Deliberately permissive. An over-tight rule here does not merely waste a
    call, it silently removes the value from the output — measured: a 12-char
    bound dropped four real answer-key values and cost three categories.
    """
    t = str(text).replace("\n", " ").strip()
    return bool(t) and len(t) <= MAX_ANSWER_LEN and any(c.isalnum() for c in t)


def in_suppressed(bbox, ctx: dict) -> bool:
    for z in ctx.get("suppress_regions", []):
        b = z["bbox"]
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            return True
    return False


def worth_escalating(o: dict, ctx: dict) -> bool:
    return is_taglike(o.get("text", "")) and not in_suppressed(o["bbox"], ctx)


ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {"rulings": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "the dispute id you are ruling on"},
            "text": {"type": "string", "description": "the correct reading, or empty if nothing is there"},
            "present": {"type": "boolean", "description": "false if no such text/symbol exists at that location"},
            "kind": {"type": "string"},
            "symbol": {"type": "string"},
            "attached_to": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["id", "text", "present", "kind", "symbol", "attached_to", "confidence"],
        "additionalProperties": False}}},
    "required": ["rulings"],
    "additionalProperties": False,
}

ADJUDICATE_SYSTEM = """You settle disagreements between two readers of a scanned P&ID.

For each numbered dispute you are given a location in this tile and what each
reader claimed. Look at the pixels yourself and rule.

- Report what is ACTUALLY drawn there, not a compromise between the claims.
- Both readers can be wrong. If neither matches, give your own reading.
- If nothing is at that location, set present=false. One reader inventing a tag
  is exactly what this step exists to catch.
- This is a single-stroke CAD font: V/Y, 0/O, 5/S, 8/B, 2/Z and 1/I confuse
  easily. That is usually what the disagreement is about. Look closely."""


def _norm(t: str) -> str:
    """Compare readings, not formatting. Models differ on whitespace and
    punctuation inside a tag without disagreeing about what it says."""
    return re.sub(r"[^A-Z0-9]", "", str(t).upper())


def _iou(a, b) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if not inter:
        return 0.0
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / ua if ua else 0.0


def compare(obs_a: list[dict], obs_b: list[dict]) -> dict:
    """Align two models' observations and classify every one of them.

    agree     — both read the same text at the same place
    conflict  — both saw something there, read it differently  (escalate)
    solo_a/b  — only one model reported it at all               (escalate)
    """
    used_b: set[int] = set()
    agree, conflict, solo_a = [], [], []

    for oa in obs_a:
        best, best_i = 0.0, None
        for i, ob in enumerate(obs_b):
            if i in used_b:
                continue
            v = _iou(oa["bbox"], ob["bbox"])
            if v > best:
                best, best_i = v, i
        if best_i is not None and best >= SAME_THING_IOU:
            ob = obs_b[best_i]
            used_b.add(best_i)
            if _norm(oa.get("text", "")) == _norm(ob.get("text", "")):
                agree.append({**oa, "_agreed_with": ob.get("text"), "_iou": round(best, 3)})
            else:
                conflict.append({"a": oa, "b": ob, "_iou": round(best, 3)})
        else:
            solo_a.append(oa)

    solo_b = [ob for i, ob in enumerate(obs_b) if i not in used_b]
    return {"agree": agree, "conflict": conflict, "solo_a": solo_a, "solo_b": solo_b}


def _tiles_needing_review(cmp: dict) -> set[str]:
    """Which tiles hold at least one unsettled reading."""
    out: set[str] = set()
    for c in cmp["conflict"]:
        out.add(c["a"].get("tile_id", ""))
    for o in cmp["solo_a"] + cmp["solo_b"]:
        out.add(o.get("tile_id", ""))
    return {t for t in out if t}


def _disputes(cmp: dict, ctx: dict) -> list[dict]:
    """Flatten the comparison into items. EVERY dispute is listed; `escalate`
    marks the ones worth paying a flagship to settle. The rest still have to
    survive the merge — see `run`."""
    out = []
    for c in cmp["conflict"]:
        a, b = c["a"], c["b"]
        esc = ((worth_escalating(a, ctx) or worth_escalating(b, ctx)))
        out.append({"kind": "conflict", "bbox": a["bbox"], "tile_id": a.get("tile_id"),
                    "claim_a": a.get("text"), "claim_b": b.get("text"),
                    "obs": a, "alt": b, "escalate": esc})
    for key in ("solo_a", "solo_b"):
        for o in cmp[key]:
            out.append({"kind": key, "bbox": o["bbox"], "tile_id": o.get("tile_id"),
                        "claim_a": o.get("text") if key == "solo_a" else None,
                        "claim_b": o.get("text") if key == "solo_b" else None,
                        "obs": o, "alt": None,
                        "escalate": worth_escalating(o, ctx)})
    return out


# One call cannot rule on an unbounded list: a tile with 76 disputes overran a
# 16k budget and every ruling on it was lost. Chunking bounds the output.
MAX_DISPUTES_PER_CALL = 30
ADJUDICATE_MAX_TOKENS = 32000


def _adjudicate_chunk(tile_img, box, ctx, chunk, flagship):
    """Rule on one chunk of disputes from one tile.

    Returns {global_index: ruling_or_None}; a key present with None means the
    flagship positively ruled NOTHING is there. An index missing entirely means
    it was never settled, which the merge treats very differently.
    """
    from pipeline.llm import ask_json

    from pipeline.llm import box_scale
    # Speak the flagship's coordinate language: Gemini thinks in 0-1000, so a
    # pixel location would send it looking at the wrong spot.
    sx, sy = box_scale(flagship, tile_img.width, tile_img.height)
    unit = "on a 0-1000 scale of" if (sx, sy) != (1.0, 1.0) else "in pixels of"
    lines = [f"Tile covering page pixels x{box[0]}-{box[2]}, y{box[1]}-{box[3]}.", "",
             f"Two readers disagree about these locations. Coordinates are {unit} "
             "THIS tile image.", ""]
    for local, (gi, it) in enumerate(chunk):
        b = it["bbox"]
        loc = [int((b[0] - box[0]) / sx), int((b[1] - box[1]) / sy),
               int((b[2] - box[0]) / sx), int((b[3] - box[1]) / sy)]
        if it["kind"] == "conflict":
            lines.append(f"[{local}] at {loc}: reader A reads {it['claim_a']!r}, "
                         f"reader B reads {it['claim_b']!r}. Which is right?")
        else:
            who = "A" if it["kind"] == "solo_a" else "B"
            claim = it["claim_a"] or it["claim_b"]
            lines.append(f"[{local}] at {loc}: only reader {who} reported anything "
                         f"here ({claim!r}). Is it really there?")
    lines += ["", "Rule on every numbered dispute, reusing the same id."]

    data, usage = ask_json(tile_img, "\n".join(lines), ADJUDICATE_SCHEMA,
                           max_tokens=ADJUDICATE_MAX_TOKENS, effort="xhigh",
                           system=ADJUDICATE_SYSTEM, model=flagship,
                           tag={"layer": "D", "role": "flagship"})
    out: dict[int, dict | None] = {}
    for r in data.get("rulings", []):
        local = r.get("id")
        if not isinstance(local, int) or not (0 <= local < len(chunk)):
            continue
        gi, it = chunk[local]
        if not r.get("present"):
            out[gi] = None                      # positively ruled absent
            continue
        base = {k: v for k, v in it["obs"].items() if k not in ("text", "model")}
        out[gi] = {**base, "text": r["text"],
                   "kind": r.get("kind") or it["obs"].get("kind"),
                   "symbol": r.get("symbol") or it["obs"].get("symbol"),
                   "attached_to": r.get("attached_to") or it["obs"].get("attached_to"),
                   "confidence": r.get("confidence", 0.9), "model": flagship,
                   "_dispute": it["kind"], "_claim_a": it["claim_a"],
                   "_claim_b": it["claim_b"]}
    return out, usage


def run(
    page: Image.Image,
    tiles: list[tuple[int, int, int, int]],
    ctx: dict,
    light_a: str,
    light_b: str,
    flagship: str,
    on_progress: Callable[[str, int, int, int], None] | None = None,
) -> tuple[list[dict], dict]:
    """Returns (observations, report)."""
    report: dict = {"light_a": light_a, "light_b": light_b, "flagship": flagship,
                    "pairing_warning": validate_pair(light_a, light_b),
                    "stages": [], "cost": {}, "escalation": {}}

    def prog(stage):
        return (lambda d, n, o: on_progress(stage, d, n, o)) if on_progress else None

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(observe_page, page, tiles, ctx, prog(light_a), light_a, None, "light A")
        fb = ex.submit(observe_page, page, tiles, ctx, prog(light_b), light_b, None, "light B")
        obs_a, usage_a = fa.result()
        obs_b, usage_b = fb.result()
    report["cost"][light_a] = cost(light_a, usage_a)
    report["cost"][light_b] = cost(light_b, usage_b)
    report["stages"].append({"stage": "light", "seconds": round(time.time() - t0, 1),
                             "models": [light_a, light_b],
                             "observations": {light_a: len(obs_a), light_b: len(obs_b)},
                             "usage": {light_a: usage_a, light_b: usage_b}})

    cmp = compare(obs_a, obs_b)
    items = _disputes(cmp, ctx)
    esc = [(i, it) for i, it in enumerate(items) if it["escalate"] and it["tile_id"]]

    chunks: list[tuple[str, list]] = []
    per_tile: dict[str, list] = {}
    for gi, it in esc:
        per_tile.setdefault(it["tile_id"], []).append((gi, it))
    for tid, lst in per_tile.items():
        for k in range(0, len(lst), MAX_DISPUTES_PER_CALL):
            chunks.append((tid, lst[k:k + MAX_DISPUTES_PER_CALL]))

    total = sum(len(cmp[k]) for k in ("agree", "conflict", "solo_a", "solo_b"))
    report["escalation"] = {
        "agree": len(cmp["agree"]), "conflict": len(cmp["conflict"]),
        "solo_a": len(cmp["solo_a"]), "solo_b": len(cmp["solo_b"]),
        "agreement_rate": round(len(cmp["agree"]) / total, 3) if total else None,
        "disputes_all": len(items), "disputes_escalated": len(esc),
        "flagship_calls": len(chunks),
        "tiles_escalated": len(per_tile), "tiles_total": len(tiles),
    }

    rulings: dict[int, dict | None] = {}
    if chunks:
        t1 = time.time()
        page.load()
        tot = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "errors": 0,
               "error_messages": []}
        crops = {tid: page.crop(tiles[int(tid[1:])]) for tid, _ in chunks
                 if tid[1:].isdigit() and int(tid[1:]) < len(tiles)}
        done = 0
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_adjudicate_chunk, crops[tid], tiles[int(tid[1:])],
                              ctx, ch, flagship): tid
                    for tid, ch in chunks if tid in crops}
            for fut in cf.as_completed(futs):
                done += 1
                try:
                    got, u = fut.result()
                    rulings.update(got)
                    tot["input_tokens"] += u["input_tokens"]
                    tot["output_tokens"] += u["output_tokens"]
                    tot["calls"] += 1
                except Exception as exc:
                    tot["errors"] += 1
                    m = f"{type(exc).__name__}: {exc}"[:200]
                    if m not in tot["error_messages"]:
                        tot["error_messages"].append(m)
                if on_progress:
                    on_progress(flagship, done, len(futs), len(rulings))
        report["cost"][flagship] = cost(flagship, tot)
        report["stages"].append({"stage": "adjudicate",
                                 "seconds": round(time.time() - t1, 1),
                                 "models": [flagship], "calls": len(chunks),
                                 "rulings": len(rulings), "usage": {flagship: tot},
                                 "failed_calls": tot["errors"],
                                 "errors": tot["error_messages"]})

    # ---- merge --------------------------------------------------------------
    # Agreed readings stand. For every dispute there are exactly three cases,
    # and the third is the one that broke this the first time:
    #   settled + present  -> the flagship's reading replaces both claims
    #   settled + absent   -> dropped; this is how a hallucination is removed
    #   NOT settled        -> KEEP the light model's reading unchanged
    # Never-settled covers a dispute we chose not to escalate AND one whose
    # adjudication call failed. Dropping those silently deleted real values
    # ('1-SS-FLEX HOSE NO. 3', 'PRIMARY SAMPLE PANEL') and cost three
    # categories of recall. Unsettled means unknown, and unknown must not mean
    # deleted.
    merged = [{**o, "source": "agreed"} for o in cmp["agree"]]
    kept_unsettled = dropped_absent = adjudicated = 0
    for gi, it in enumerate(items):
        if gi in rulings:
            r = rulings[gi]
            if r is None:
                dropped_absent += 1
            else:
                merged.append({**r, "source": "adjudicated"})
                adjudicated += 1
        else:
            # For an unsettled CONFLICT take the more confident of the two
            # readings; for a solo, take the one model that saw it.
            o = it["obs"]
            alt = it.get("alt")
            if alt and float(alt.get("confidence", 0)) > float(o.get("confidence", 0)):
                o = alt
            merged.append({**o, "source": "unsettled", "_dispute": it["kind"],
                           "_claim_a": it["claim_a"], "_claim_b": it["claim_b"]})
            kept_unsettled += 1

    costs = [c for c in report["cost"].values() if c is not None]
    report["cost"]["total"] = round(sum(costs), 4) if costs else None
    report["cost"]["unpriced"] = [m for m, c in report["cost"].items()
                                  if c is None and m not in ("total", "unpriced")]
    report["merge"] = {"agreed": len(cmp["agree"]), "adjudicated": adjudicated,
                       "dropped_as_absent": dropped_absent,
                       "kept_unsettled": kept_unsettled}
    report["observations_out"] = len(merged)
    return merged, report
