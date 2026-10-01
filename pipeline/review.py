"""
Human-in-the-loop review queue.

WHAT THIS IS FOR. The pipeline ends with values it cannot settle by itself.
Some are model disagreements a flagship already ruled on; some are readings
only one model ever saw; some are correct extractions the sparse answer key
simply does not list. None of these are errors the pipeline can fix on its
own, and all of them are decisions a person can make in seconds while looking
at the pixels.

WHY IT IS PRIORITISED THIS WAY. The queue is ordered by how much a human
decision is worth, not by how the pipeline is structured:

  contested   — two models read the same pixels differently. Measured on
                page 1 these land on exactly the answer-key values recall was
                failing (CP-166A vs CP-166B, PCV2847 vs FCV2847, V835 vs
                V838). Highest information per second of attention.
  solo        — only one reader saw it at all. Either the other model missed
                it or this one invented it; a person settles that instantly.
  suppressed  — dropped for sitting inside a note/legend block. This is in the
                queue because the suppress zones have been wrong before: on
                page 1 Layer B's legend box stopped at x=4509 while the legend
                text ran from x=3962, so real values were being discarded.
  unread      — the independent detector found a text region no model
                reported. The only genuine recall signal Layer C produces.
  not_in_key  — emitted, correct-looking, absent from the key. NOT errors:
                the key lists 2 valves where the sheet has 30+. This is the
                adjudication backlog, and it is last because most entries are
                right.

DECISIONS ARE KEYED BY CONTENT, NOT BY POSITION. A run produces a different
number of observations each time, so an index would silently re-point a saved
decision at a different item after a re-run. The id is a hash of what the item
IS (page, category, value, rounded bbox), so a decision sticks to its item
across runs and survives a RULES.yaml change.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pipeline.paths import RUNS as REVIEWS

PRIORITY = ["contested", "solo", "suppressed", "unread", "not_in_key"]

# What a real tag looks like: 2-12 chars, starts alphanumeric, and contains at
# least one letter or multi-digit run — enough to exclude "0" and "-" while
# keeping "V113", "9A", "1/2\"-T", "F-192".
_TAGLIKE = re.compile(r"^(?=.*(?:[A-Z]|\d{2}))[A-Z0-9][A-Z0-9\-/.\"\' ]{1,11}$", re.I)


def item_id(page: int, kind: str, value: str, bbox, category: str | None = None) -> str:
    """Stable across runs.

    Quantising the bbox does NOT work: rounding to a grid flips whenever a box
    sits near a cell boundary, so a two-pixel shift between runs produces a
    different id and the saved decision detaches from its item. So an item that
    HAS a value is identified by what it is — page, category, normalised value
    — and not by where it sits. "Reject CP-166B in sample_panels" should stay
    attached to that value even if the next run boxes it slightly differently.

    Only items with no value (an `unread` detector region) fall back to
    position, quantised coarsely to 32px on the CENTRE, which is far less
    boundary-sensitive than quantising all four edges.
    """
    v = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if v:
        return hashlib.sha1(f"{page}|{kind}|{category or ''}|{v}".encode()).hexdigest()[:12]
    b = bbox or [0, 0, 0, 0]
    cx, cy = round((b[0] + b[2]) / 64), round((b[1] + b[3]) / 64)
    return hashlib.sha1(f"{page}|{kind}|@{cx},{cy}".encode()).hexdigest()[:12]


def _path(page: int) -> Path:
    return REVIEWS / f"page{page}_review.json"


def load_decisions(page: int) -> dict:
    p = _path(page)
    return json.load(open(p)).get("decisions", {}) if p.exists() else {}


def save_decision(page: int, iid: str, action: str, corrected: str | None = None,
                  note: str | None = None, who: str = "reviewer") -> dict:
    """action: accept | reject | correct"""
    import datetime as dt
    p = _path(page)
    blob = json.load(open(p)) if p.exists() else {"page": page, "decisions": {}}
    blob["decisions"][iid] = {"action": action, "corrected": corrected,
                              "note": note, "by": who,
                              "at": dt.datetime.now().isoformat(timespec="seconds")}
    json.dump(blob, open(p, "w"), indent=1)
    return blob["decisions"][iid]


def build_queue(summary: dict, observations: list[dict],
                regions: list[dict] | None = None, ctx: dict | None = None) -> list[dict]:
    """Assemble the queue from one finished run."""
    page = summary.get("page")
    prov = summary.get("provenance") or {}
    score = summary.get("score") or {}
    cats = score.get("categories") or {}
    decisions = load_decisions(page)
    out: list[dict] = []

    def add(kind, value, bbox, **extra):
        iid = item_id(page, kind, value, bbox, extra.get("category"))
        out.append({"id": iid, "kind": kind, "value": value, "bbox": bbox,
                    "decision": decisions.get(iid), **extra})

    # -- contested + solo: read straight off the observations' cascade tags ---
    by_box = {}
    for o in observations:
        by_box[tuple(o["bbox"])] = o

    for cat, entries in prov.items():
        for p in entries:
            o = by_box.get(tuple(p["bbox"]), {})
            src, disp = o.get("source"), o.get("_dispute")
            if src == "adjudicated" and disp == "conflict":
                add("contested", p["value"], p["bbox"], category=cat,
                    claim_a=o.get("_claim_a"), claim_b=o.get("_claim_b"),
                    ruled=o.get("text"), by=o.get("model"),
                    tile_id=p.get("tile_id"), rule=p.get("rule"))
            elif src == "adjudicated" and disp in ("solo_a", "solo_b"):
                add("solo", p["value"], p["bbox"], category=cat,
                    seen_by=o.get("_claim_a") and "model A" or "model B",
                    claim_a=o.get("_claim_a"), claim_b=o.get("_claim_b"),
                    by=o.get("model"), tile_id=p.get("tile_id"), rule=p.get("rule"))

    # -- suppressed: dropped by a note/legend zone ---------------------------
    if ctx:
        zones = [(z["kind"], z["bbox"]) for z in ctx.get("suppress_regions", [])]
        for o in observations:
            b = o["bbox"]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            for kind, z in zones:
                if z[0] <= cx <= z[2] and z[1] <= cy <= z[3]:
                    t = str(o.get("text", "")).replace("\n", " ").strip()
                    # A suppressed sentence of prose is correctly suppressed, and
                    # a bare "0" from a revision table is not worth anyone's
                    # attention. Only something shaped like a real tag is.
                    if _TAGLIKE.match(t):
                        add("suppressed", t, b, zone=kind, tile_id=o.get("tile_id"))
                    break

    # -- unread: detector saw a region, no model reported anything -----------
    if regions:
        obb = [o["bbox"] for o in observations]

        def hit(r):
            cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
            for o in obb:
                if o[0] <= cx <= o[2] and o[1] <= cy <= o[3]:
                    return True
                ox, oy = (o[0] + o[2]) / 2, (o[1] + o[3]) / 2
                if r[0] <= ox <= r[2] and r[1] <= oy <= r[3]:
                    return True
            return False

        for r in regions:
            if not hit(r["bbox"]):
                add("unread", "", r["bbox"], det_score=round(r.get("score", 0), 3))

    # -- not_in_key: emitted, key does not list it ---------------------------
    for cat, c in cats.items():
        for v in (c.get("extra") or []):
            bbox = next((p["bbox"] for p in prov.get(cat, [])
                         if str(p["value"]) == str(v)), None)
            add("not_in_key", v, bbox, category=cat,
                expected_n=len(c.get("expected") or []))

    out.sort(key=lambda i: (PRIORITY.index(i["kind"]) if i["kind"] in PRIORITY else 99,
                            str(i.get("category") or ""), str(i["value"])))
    return out


def summarise(queue: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for i in queue:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    decided = sum(1 for i in queue if i.get("decision"))
    return {"total": len(queue), "decided": decided,
            "open": len(queue) - decided, "by_kind": counts}


def apply_decisions(answers: dict, queue: list[dict]) -> tuple[dict, dict]:
    """Fold reviewer decisions back into the answers.

    This is what makes it a loop rather than a comment box: a rejection
    removes the value from the output, a correction replaces it, and the
    corrected set is what gets scored and exported.
    """
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in answers.items()}
    applied = {"rejected": 0, "corrected": 0, "accepted": 0}
    for item in queue:
        d = item.get("decision")
        cat = item.get("category")
        if not d or not cat or cat not in out:
            continue
        vals = out[cat]
        if isinstance(vals, str):
            vals = [vals]
        v = str(item["value"])
        if d["action"] == "reject" and v in vals:
            vals.remove(v); applied["rejected"] += 1
        elif d["action"] == "correct" and d.get("corrected"):
            if v in vals:
                vals[vals.index(v)] = d["corrected"]
            elif d["corrected"] not in vals:
                vals.append(d["corrected"])
            applied["corrected"] += 1
        elif d["action"] == "accept":
            applied["accepted"] += 1
        out[cat] = vals
    return out, applied
