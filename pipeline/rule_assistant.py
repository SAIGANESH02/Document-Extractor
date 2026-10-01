"""
Rule assistant — the model PROPOSES rule changes, a person approves them.

Why not let it edit RULES.yaml directly: the brief requires every live change
to be explained, and a rule nobody reviewed cannot be. So the flow is

  propose → preview → accept / reject

  propose  One text-only call. Input: the current rules, this sheet's own
           legend (Layer B), the categories wanted (if known), and a sample of
           readings no rule claimed (Layer E). Output: add/modify proposals,
           each with a reason and where the knowledge came from.
  preview  Layer E is deterministic and instant, so every proposal is
           test-run on cached observations: values it adds or removes on THIS
           sheet, and recall before/after on every sheet that has an answer
           key — the guard against fitting the new sheet by breaking old ones.
  accept   Writes RULES.yaml through rules_store (versioned + changelog).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re

from pipeline import rules_store
from pipeline.llm import ask_json
from pipeline.paths import CACHE, DATA

PROPOSALS = DATA / "rules" / "proposals"
FIELDS = ["text", "kind", "symbol", "attached_to"]
MAX_UNCLAIMED = 150

_COND = {"type": "array", "items": {
    "type": "object",
    "properties": {"field": {"type": "string", "enum": FIELDS},
                   "pattern": {"type": "string", "description": "case-insensitive Python regex, searched"}},
    "required": ["field", "pattern"], "additionalProperties": False}}

SCHEMA = {
    "type": "object",
    "properties": {
        "sheet_summary": {"type": "string", "description": "One sentence: what kind of sheet this is"},
        "proposals": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "modify"]},
                "index": {"type": "integer", "description": "rule index to modify; -1 for add"},
                "category": {"type": "string"},
                "when": _COND,
                "unless": _COND,
                "emit": {"type": "string", "enum": ["text", "capture", "split", "template"]},
                "pattern": {"type": "string", "description": "for capture/template; empty otherwise"},
                "pattern_field": {"type": "string", "enum": FIELDS},
                "template": {"type": "string", "description": "for template, e.g. '{name} {text}'; empty otherwise"},
                "keep_legend_codes": {"type": "boolean"},
                "reason": {"type": "string", "description": "One or two plain sentences: why, citing the evidence"},
                "source": {"type": "string", "enum": ["sheet legend", "requested category",
                                                      "unclaimed readings", "existing rule too narrow",
                                                      "existing rule too broad"]},
                "evidence": {"type": "array", "items": {"type": "string"},
                             "description": "Up to 5 readings (text) this change is based on"},
            },
            "required": ["action", "index", "category", "when", "unless", "emit", "pattern",
                         "pattern_field", "template", "keep_legend_codes", "reason", "source",
                         "evidence"],
            "additionalProperties": False}},
    },
    "required": ["sheet_summary", "proposals"],
    "additionalProperties": False,
}

SYSTEM = """You maintain the rules that map observations from engineering drawings
(P&IDs) to answer categories. Rules are regex conditions over four fields of an
observation: text, kind, symbol, attached_to.

Propose the SMALLEST set of changes that makes the rules fit a new sheet:
- add a rule for a requested category no rule covers;
- add a rule when the sheet's legend defines a symbol class that unclaimed
  readings clearly belong to;
- modify a rule only when it is clearly too narrow or too broad for this sheet.

Hard constraints:
- Keep existing rules working on other sheets. Prefer adding over modifying.
- Condition on the drawn SYMBOL or what a tag is attached to when the category
  is defined by a symbol; a tag prefix alone is weak evidence.
- Never write a rule that matches one specific value only (no '^V113$').
- Never propose rules for `pid`: the drawing number is read from the title
  block by an earlier stage, not by these rules.
- Every regex must be valid Python. Patterns are case-insensitive.
- If nothing needs to change, return no proposals. Fewer, well-founded changes
  beat many speculative ones."""


def _prompt(rules: dict, ctx: dict, unclaimed: list[dict], categories: list[str]) -> str:
    rule_lines = []
    for i, r in enumerate(rules.get("rules", [])):
        compact = {k: r[k] for k in ("category", "when", "unless", "emit", "pattern") if k in r}
        rule_lines.append(f"[{i}] {json.dumps(compact)}")
    legend = [f"- {s['shape']}: {s['meaning']}" for s in ctx.get("symbols", [])]
    codes = [f"- {p['code']} = {p['meaning']}" for p in ctx.get("system_prefixes", [])]
    seen, sample = set(), []
    for o in unclaimed:
        key = (str(o.get("text", "")).strip().upper(), o.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        sample.append({k: o.get(k) for k in FIELDS})
        if len(sample) >= MAX_UNCLAIMED:
            break
    return "\n".join([
        f"Sheet: {ctx.get('sheet_id', '?')} — {ctx.get('title', '')}",
        "", "This sheet's own legend:", *(legend or ["(none read)"]),
        "", "System codes on this sheet:", *(codes or ["(none read)"]),
        "", "Categories wanted for this sheet:",
        *([f"- {c}" for c in categories] or ["(not given — infer only from the legend; do not invent category names freely)"]),
        "", "Current rules:", *rule_lines,
        "", f"Readings no rule claimed ({len(sample)} unique of {len(unclaimed)}):",
        *[json.dumps(s) for s in sample],
    ])


def _to_rule(p: dict) -> dict:
    r: dict = {"category": p["category"],
               "source": f"rule assistant ({p['source']})",
               "note": p["reason"],
               "when": {c["field"]: c["pattern"] for c in p["when"]}}
    if p.get("unless"):
        r["unless"] = {c["field"]: c["pattern"] for c in p["unless"]}
    r["emit"] = p["emit"]
    if p["emit"] in ("capture", "template") and p.get("pattern"):
        r["pattern"] = p["pattern"]
    if p["emit"] == "template":
        r["pattern_field"] = p.get("pattern_field") or "text"
        r["template"] = p.get("template") or "{name} {text}"
    if p["emit"] == "split" and p.get("keep_legend_codes"):
        r["keep_legend_codes"] = True
    return r


def _apply(rules: dict, p: dict) -> dict:
    new = copy.deepcopy(rules)
    if p["action"] == "add":
        new["rules"].append(p["rule"])
    else:
        new["rules"][p["index"]] = p["rule"]
    return new


def _validate(p: dict, rules: dict) -> str | None:
    if p["action"] == "modify" and not (0 <= p["index"] < len(rules["rules"])):
        return f"index {p['index']} does not exist"
    for part in ("when", "unless"):
        for f, pat in (p["rule"].get(part) or {}).items():
            try:
                re.compile(pat)
            except re.error as e:
                return f"invalid regex in {part}.{f}: {e}"
    if p["rule"].get("pattern"):
        try:
            re.compile(p["rule"]["pattern"])
        except re.error as e:
            return f"invalid regex in pattern: {e}"
    if not p["rule"]["when"]:
        return "rule has no conditions — it would match everything"
    return None


def _keyed_sheets() -> list[tuple[int, int, dict, list]]:
    """(page, document_id, ctx, observations) for every cached sheet with a key."""
    from pipeline.score import document_for_sheet
    out = []
    for ctx_f in sorted(CACHE.glob("page*_context*.json")):
        page = int(re.match(r"page(\d+)_", ctx_f.name).group(1))
        if any(o[0] == page for o in out):
            continue
        ctx = json.load(open(ctx_f))
        doc = document_for_sheet(ctx.get("sheet_id"))
        from pipeline.run import RunConfig, _obs_path
        obs_f = _obs_path(page, RunConfig(model_d="claude-opus-5"))     # current prompt
        if not obs_f.exists():
            obs_f = CACHE / f"page{page}_observations__claude-opus-5.json"  # older prompt
        if doc is None or not obs_f.exists():
            continue
        out.append((page, doc, ctx, json.load(open(obs_f))["observations"]))
    return out


def _recall(rules, ctx, obs, doc):
    from pipeline.resolve import resolve
    from pipeline.score import score
    return score(resolve(obs, ctx, rules)["answers"], doc)["totals"].get("recall")


def preview(p: dict, rules: dict, ctx: dict, observations: list) -> dict:
    """What accepting `p` would change, measured, without touching RULES.yaml."""
    from pipeline.resolve import resolve
    new = _apply(rules, p)
    cat = p["rule"]["category"]
    before = set(resolve(observations, ctx, rules)["answers"].get(cat, []) or [])
    after = set(resolve(observations, ctx, new)["answers"].get(cat, []) or [])
    keyed = []
    for page, doc, kctx, kobs in _keyed_sheets():
        b, a = _recall(rules, kctx, kobs, doc), _recall(new, kctx, kobs, doc)
        keyed.append({"page": page, "document_id": doc, "recall_before": b, "recall_after": a})
    return {"adds": sorted(map(str, after - before)), "removes": sorted(map(str, before - after)),
            "keyed_sheets": keyed,
            "regresses": any((k["recall_after"] or 0) < (k["recall_before"] or 0) for k in keyed)}


def _path(page: int):
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    return PROPOSALS / f"page{page}.json"


def load(page: int) -> dict | None:
    f = _path(page)
    return json.load(open(f)) if f.exists() else None


def propose(page: int, ctx: dict, observations: list, unclaimed: list,
            categories: list[str] | None = None, model: str | None = None) -> dict:
    rules = rules_store.load()
    data, _ = ask_json(None, _prompt(rules, ctx, unclaimed, categories or []), SCHEMA,
                       max_tokens=16000, effort="high", system=SYSTEM, model=model,
                       tag={"layer": "R", "role": "rule assistant"})
    items = []
    for i, raw in enumerate(data.get("proposals", [])):
        p = {"id": i, "action": raw["action"], "index": raw["index"],
             "reason": raw["reason"], "source": raw["source"], "evidence": raw.get("evidence", []),
             "rule": _to_rule(raw), "status": "pending"}
        if p["action"] == "modify" and 0 <= p["index"] < len(rules["rules"]):
            old = rules["rules"][p["index"]]
            p["before"] = old
            # keep the old rule's history fields; the new note explains the change
            p["rule"]["source"] = f"{old.get('source', '')}; modified by rule assistant ({raw['source']})"
        p["invalid"] = _validate(p, rules)
        if not p["invalid"]:
            p["preview"] = preview(p, rules, ctx, observations)
        items.append(p)
    blob = {"page": page, "sheet_id": ctx.get("sheet_id"),
            "sheet_summary": data.get("sheet_summary", ""),
            "categories": categories or [],
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "rules_count_at_proposal": len(rules["rules"]),
            "proposals": items}
    json.dump(blob, open(_path(page), "w"), indent=1)
    return blob


def decide(page: int, pid: int, action: str) -> dict:
    blob = load(page)
    if not blob:
        raise ValueError("no proposals for this page")
    p = next((x for x in blob["proposals"] if x["id"] == pid), None)
    if p is None:
        raise ValueError(f"no proposal {pid}")
    if p["status"] != "pending":
        raise ValueError(f"proposal {pid} is already {p['status']}")
    if action == "reject":
        p["status"] = "rejected"
    else:
        if p.get("invalid"):
            raise ValueError(f"cannot accept an invalid proposal: {p['invalid']}")
        rules = rules_store.load()
        if p["action"] == "modify" and rules["rules"][p["index"]] != p.get("before"):
            raise ValueError("that rule changed since this proposal was made — re-run the check")
        prev = rules_store.save(_apply(rules, p), {
            "page": page, "sheet_id": blob.get("sheet_id"), "action": p["action"],
            "category": p["rule"]["category"], "index": p["index"],
            "before": p.get("before"), "after": p["rule"],
            "reason": p["reason"], "source": p["source"], "by": "reviewer via rule assistant"})
        p["status"] = "accepted"
        p["previous_version"] = prev
    json.dump(blob, open(_path(page), "w"), indent=1)
    return p
