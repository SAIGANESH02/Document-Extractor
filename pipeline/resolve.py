"""
Layer E — resolve.

Deterministic. No model, no network, runs in milliseconds. Turns Layer D's
generic observations into the answer-key shaped JSON by:

  1. dropping observations below a confidence floor
  2. suppressing anything inside a Layer-B prose region (§9 risk #7)
  3. de-duplicating across tile seams (bbox IoU + normalised-text match)
  4. applying RULES.yaml to map observation -> category
  5. normalising and sorting

Keeping this stage model-free is the whole point. It is auditable line by
line, it re-runs instantly on cached observations, and every emitted value
carries provenance back to the observation and bbox it came from — which is
what the viewer's inspector reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from pipeline.paths import RULES_PATH


def load_rules(path: Path | None = None) -> dict:
    return yaml.safe_load(open(path or RULES_PATH))


def _norm(s: str, cfg: dict) -> str:
    if cfg.get("strip", True):
        s = s.strip()
    if cfg.get("collapse_whitespace", False):
        s = re.sub(r"\s+", "", s)
    else:
        s = re.sub(r"\s+", " ", s)
    if cfg.get("join_tag_prefix", True):
        # "PCV 2848" -> "PCV2848", without gluing multi-word names together.
        s = re.sub(r"^([A-Z]{1,4})\s+(\d)", r"\1\2", s)
    if cfg.get("uppercase"):
        s = s.upper()
    return s


def _match(obs: dict, conds: dict) -> bool:
    for field, pat in (conds or {}).items():
        if not re.search(pat, str(obs.get(field, "")), re.I):
            return False
    return True


def _any_match(obs: dict, conds: dict) -> bool:
    for field, pat in (conds or {}).items():
        if re.search(pat, str(obs.get(field, "")), re.I):
            return True
    return False


def _iou(a, b) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if not inter:
        return 0.0
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / ua if ua else 0.0


def _inside(bbox, region) -> bool:
    cx, cy = (bbox[0]+bbox[2]) / 2, (bbox[1]+bbox[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def _connector_id(text: str) -> str | None:
    t = " ".join(str(text).split()).upper()
    m = re.search(r"(?<![A-Z0-9-])([A-Z])\s+(\d{4,6})\b", t)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"\b(\d{4,6})\s+([A-Z])(?![A-Z0-9])", t)
    if m:
        return m.group(2) + m.group(1)
    m = re.search(r"\b(\d{4,6})\b", t)
    return m.group(1) if m else None


def resolve(observations: list[dict], ctx: dict, rules: dict | None = None,
            verdicts: dict | None = None) -> dict:
    """`verdicts` are Layer V's visual yes/no answers for rules with a `verify`
    block; without them those rules fall back to their wording test."""
    from pipeline import verify as V
    rules = rules or load_rules()
    legend_codes = {str(p.get("code", "")).strip().upper()
                    for p in ctx.get("system_prefixes", []) if p.get("code")}
    ncfg = rules.get("normalise", {})
    floor = rules.get("min_confidence", 0.0)
    kinds = set(rules.get("suppress_region_kinds", []))

    audit = {"input": len(observations), "low_confidence": 0, "suppressed": 0, "duplicates": 0}

    # 1. confidence floor
    kept = [o for o in observations if float(o.get("confidence", 1)) >= floor]
    audit["low_confidence"] = len(observations) - len(kept)

    # 2. suppress prose regions
    zones = [r["bbox"] for r in ctx.get("suppress_regions", []) if r.get("kind") in kinds]
    after = [o for o in kept if not any(_inside(o["bbox"], z) for z in zones)]
    audit["suppressed"] = len(kept) - len(after)
    kept = after

    # 3. dedupe across tile seams
    uniq: list[dict] = []
    for o in sorted(kept, key=lambda x: -float(x.get("confidence", 0))):
        t = _norm(o.get("text", ""), ncfg)
        if any(_norm(u.get("text", ""), ncfg) == t and _iou(o["bbox"], u["bbox"]) > 0.25
               for u in uniq):
            continue
        uniq.append(o)
    audit["duplicates"] = len(kept) - len(uniq)
    audit["resolved"] = len(uniq)

    # 4. apply rules
    answers: dict[str, list[str]] = {}
    provenance: dict[str, list[dict]] = {}

    def add(cat: str, val: str, obs: dict, rule_idx: int, check: dict | None = None):
        val = _norm(val, ncfg)
        if not val:
            return
        answers.setdefault(cat, [])
        if val not in answers[cat]:
            answers[cat].append(val)
            provenance.setdefault(cat, []).append(
                {"value": val, "bbox": obs["bbox"], "text": obs.get("text"),
                 "symbol": obs.get("symbol"), "confidence": obs.get("confidence"),
                 "tile_id": obs.get("tile_id"), "rule": rule_idx, "visual_check": check}
            )

    claimed: set[int] = set()
    for i, rule in enumerate(rules.get("rules", [])):
        cat = rule["category"]
        for oi, o in enumerate(uniq):
            if float(o.get("confidence", 1)) < rule.get("min_conf", 0):
                continue
            if not _match(o, rule.get("when")):
                continue
            if rule.get("unless") and _any_match(o, rule["unless"]):
                continue
            seen = V.passes(rule, o, verdicts)
            if seen is False or (seen is None and rule.get("verify") and not V.fallback(rule, o)):
                continue
            claimed.add(oi)
            mode, text = rule.get("emit", "text"), o.get("text", "")
            check = (verdicts or {}).get(V._key(cat, text)) if rule.get("verify") else None
            if check:
                check = {"answer": check.get("answer"), "evidence": check.get("evidence"),
                         "model": check.get("model")}
            if mode == "text":
                add(cat, text, o, i, check)
            elif mode == "const":
                add(cat, rule["value"], o, i)
            elif mode == "split":
                # Whitespace separates too: a split diamond is often read as
                # "DM SS" rather than "DM|SS".
                for part in re.split(r"[/|,\s]+", text):
                    # keep_legend_codes: only tokens this sheet's own legend
                    # defines (Layer B), so legend prose split into words
                    # ("INDICATES SYSTEM BOUNDARY") is not emitted as codes.
                    if rule.get("keep_legend_codes") and legend_codes \
                            and part.strip().upper() not in legend_codes:
                        continue
                    add(cat, part, o, i)
            elif mode == "capture":
                m = re.search(rule["pattern"], text, re.I)
                if m:
                    add(cat, m.group(1), o, i)
            elif mode == "connector_id":
                # Off-page connector id = flag letter + line number. The letter
                # may be read before ("L 20846") or after ("20845 J") the
                # number; with no letter read, the number alone.
                cid = _connector_id(text)
                if cid:
                    add(cat, cid, o, i, check)
            elif mode == "template":
                # Builds a value from two fields, e.g. an equipment name the
                # model reported in `attached_to` plus the tag in `text`.
                src = str(o.get(rule.get("pattern_field", "text"), ""))
                m = re.search(rule["pattern"], src, re.I)
                if m:
                    add(cat, rule["template"].format(name=m.group(1).upper(), text=text), o, i)

    if ctx.get("sheet_id"):
        answers["pid"] = _norm(ctx["sheet_id"], ncfg)

    for k, v in answers.items():
        if isinstance(v, list):
            v.sort()

    # Readings no rule claimed — the rule assistant's raw material: if a new
    # sheet carries a kind of thing our rules do not know, it shows up here.
    unclaimed = [{k: o.get(k) for k in ("text", "kind", "symbol", "attached_to", "bbox", "tile_id")}
                 for oi, o in enumerate(uniq) if oi not in claimed and str(o.get("text", "")).strip()]
    audit["unclaimed"] = len(unclaimed)
    return {"answers": answers, "provenance": provenance, "audit": audit,
            "unclaimed": unclaimed, "unclaimed_count": len(unclaimed)}
