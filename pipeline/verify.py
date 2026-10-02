"""
Layer V — visual yes/no checks for categories defined by a drawn shape.

Some categories are defined by the drawing, not the text: a PCV belongs in
`pressure_control_valves (U-bend_pipe)` only if its stem ends on a U-bend loop.
Layer D describes that in free prose ("regulator ... left of the U-bend
loop"), and matching words in prose fails both ways — landmarks count as
matches, and a U-bend the model did not mention is missed.

So a rule can carry a `verify` block: every candidate the rule's `when`
selects is cropped from the full-resolution page, the symbol is outlined in
red so the model knows which one is meant, and one question is asked with a
structured yes / no / unclear answer and the visible evidence. Layer E then
uses the answer instead of the prose. Answers are cached per page, model and
question, so a re-run costs nothing; if no answer exists the rule's
`fallback_when` / `fallback_unless` (the old wording test) applies.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.llm import ask_json
from pipeline.paths import CACHE

CROP_PX = 900          # wide enough to follow a stem or leader to where it ends
MAX_WORKERS = 6

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "evidence": {"type": "string", "description": "One sentence: what you see that decides it"},
    },
    "required": ["answer", "evidence"],
    "additionalProperties": False,
}

SYSTEM = """You check one symbol on a scanned engineering drawing (P&ID).
The symbol in question is outlined with a RED rectangle. Answer the question
about THAT symbol only, from what is drawn — not from its tag name. Follow its
stem or leader line to where it actually ends. If the crop does not show
enough to decide, answer "unclear"."""


def norm_tag(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def _key(category: str, text: str) -> str:
    return f"{category}|{norm_tag(text)}"


def _match(obs: dict, conds: dict | None) -> bool:
    return all(re.search(p, str(obs.get(f, "")), re.I) for f, p in (conds or {}).items())


def _any(obs: dict, conds: dict | None) -> bool:
    return any(re.search(p, str(obs.get(f, "")), re.I) for f, p in (conds or {}).items())


def candidates(observations: list[dict], rules: dict) -> list[tuple[dict, dict]]:
    """(rule, observation) pairs needing a visual check — one per tag per rule,
    the most confident reading of that tag."""
    out: dict[str, tuple[dict, dict]] = {}
    for rule in rules.get("rules", []):
        if not rule.get("verify"):
            continue
        for o in observations:
            if not _match(o, rule.get("when")) or not norm_tag(o.get("text", "")):
                continue
            k = _key(rule["category"], o.get("text", ""))
            if k not in out or float(o.get("confidence", 0)) > float(out[k][1].get("confidence", 0)):
                out[k] = (rule, o)
    return list(out.values())


def _crop(page: Image.Image, bbox: list[int]) -> Image.Image:
    cx, cy = (bbox[0] + bbox[2]) // 2, (bbox[1] + bbox[3]) // 2
    x0, y0 = max(0, cx - CROP_PX // 2), max(0, cy - CROP_PX // 2)
    x1, y1 = min(page.width, x0 + CROP_PX), min(page.height, y0 + CROP_PX)
    crop = page.crop((x0, y0, x1, y1)).convert("RGB")
    d = ImageDraw.Draw(crop)
    pad = 14
    d.rectangle((bbox[0] - x0 - pad, bbox[1] - y0 - pad, bbox[2] - x0 + pad, bbox[3] - y0 + pad),
                outline=(230, 0, 0), width=4)
    return crop


def _cache_path(page: int, model: str) -> Path:
    slug = "".join(c if c.isalnum() or c in "-." else "-" for c in model)
    return CACHE / f"page{page}_verify__{slug}.json"


def _qhash(question: str) -> str:
    return hashlib.sha1(question.encode()).hexdigest()[:8]


def load(page: int, model: str) -> dict:
    p = _cache_path(page, model)
    return json.load(open(p)) if p.exists() else {}


def verdicts_for(page: int, model: str, rules: dict) -> dict:
    """{category|TAG: verdict} for the questions the current rules ask."""
    cache = load(page, model)
    qs = {r["category"]: _qhash(r["verify"]["question"]) for r in rules.get("rules", []) if r.get("verify")}
    return {k: v for k, v in cache.items() if qs.get(k.split("|")[0]) == v.get("question_hash")}


def run(page: int, img: Image.Image, observations: list[dict], rules: dict, model: str,
        cache_only: bool = False, on_progress=None) -> tuple[dict, dict]:
    """Ask every pending question. Returns (verdicts, report)."""
    cache = load(page, model)
    todo = []
    for rule, o in candidates(observations, rules):
        k, qh = _key(rule["category"], o["text"]), _qhash(rule["verify"]["question"])
        if cache.get(k, {}).get("question_hash") != qh:
            todo.append((k, qh, rule, o))
    report = {"candidates": len(candidates(observations, rules)), "asked": 0, "cached": 0, "errors": []}
    report["cached"] = report["candidates"] - len(todo)
    if cache_only or not todo:
        return verdicts_for(page, model, rules), report

    img.load()
    crops = {k: _crop(img, o["bbox"]) for k, _, _, o in todo}

    def ask(item):
        k, qh, rule, o = item
        data, _ = ask_json(crops[k], f"Tag on the outlined symbol: {' '.join(str(o['text']).split())}.\n"
                           f"Question: {rule['verify']['question']}", SCHEMA,
                           max_tokens=4000, effort="medium", system=SYSTEM, model=model,
                           tag={"layer": "V", "tile": norm_tag(o["text"])})
        return k, {**data, "question_hash": qh, "tag": " ".join(str(o["text"]).split()),
                   "category": rule["category"], "bbox": o["bbox"], "model": model}

    done = 0
    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for fut in cf.as_completed([ex.submit(ask, t) for t in todo]):
            done += 1
            try:
                k, v = fut.result()
                cache[k] = v
                report["asked"] += 1
            except Exception as exc:  # noqa: BLE001 — a failed check falls back to the wording rule
                report["errors"].append(f"{type(exc).__name__}: {exc}"[:160])
            if on_progress:
                on_progress(done, len(todo))
    json.dump(cache, open(_cache_path(page, model), "w"), indent=1)
    return verdicts_for(page, model, rules), report


def passes(rule: dict, obs: dict, verdicts: dict | None) -> bool | None:
    """Visual verdict for this rule + reading: True/False, or None if no check
    exists (the caller then applies the rule's fallback wording test)."""
    if not rule.get("verify") or not verdicts:
        return None
    v = verdicts.get(_key(rule["category"], obs.get("text", "")))
    if v is None:
        return None
    return v.get("answer") == "yes"


def fallback(rule: dict, obs: dict) -> bool:
    vr = rule.get("verify") or {}
    return _match(obs, vr.get("fallback_when")) and not (
        vr.get("fallback_unless") and _any(obs, vr.get("fallback_unless")))


# ---- reading a connector's flag letter ------------------------------------------
# Some connector ids come out as a bare line number because the flag letter in
# the connector's arrow end was not read. A rule with `read_flag_letter: true`
# sends each such connector (cropped, outlined) for one narrow read: the letter.

FLAG_SCHEMA = {
    "type": "object",
    "properties": {
        "letter": {"type": "string", "description": "The single letter in the connector's flag end, or empty if there is none"},
        "evidence": {"type": "string", "description": "One sentence: where you see it"},
    },
    "required": ["letter", "evidence"],
    "additionalProperties": False,
}

FLAG_SYSTEM = """You read one off-page continuation connector on a scanned P&ID.
The connector is outlined with a RED rectangle: a box with the line number,
with a pointed flag or arrow end that usually holds one capital letter.
Report only that letter, exactly as printed. If no letter is printed, return
an empty string — never guess."""


def _flag_cache(page: int, model: str) -> Path:
    slug = "".join(c if c.isalnum() or c in "-." else "-" for c in model)
    return CACHE / f"page{page}_flags__{slug}.json"


def read_flags(page: int, img: Image.Image, observations: list[dict], resolved: dict, rules: dict,
               model: str, cache_only: bool = False) -> dict:
    """Give bare-number connector values their flag letter. Mutates `resolved`
    (answers + provenance) and returns a short report."""
    cats = {r["category"] for r in rules.get("rules", []) if r.get("read_flag_letter")}
    report = {"asked": 0, "cached": 0, "letters": 0, "errors": []}
    if not cats:
        return report
    path = _flag_cache(page, model)
    cache = json.load(open(path)) if path.exists() else {}
    jobs = []
    for cat in cats:
        for p in resolved.get("provenance", {}).get(cat, []):
            m = re.match(r"(\d{4,6})\b(.*)$", str(p["value"]))
            if not m:                      # already has a letter
                continue
            v, rest = m.group(1), m.group(2)
            cons = [o for o in observations if o.get("kind") == "continuation_connection"
                    and norm_tag(o.get("text", "")) == v]
            px, py = (p["bbox"][0] + p["bbox"][2]) / 2, (p["bbox"][1] + p["bbox"][3]) / 2
            con = min(cons, key=lambda o: abs((o["bbox"][0] + o["bbox"][2]) / 2 - px)
                      + abs((o["bbox"][1] + o["bbox"][3]) / 2 - py)) if cons else None
            box = con["bbox"] if con else p["bbox"]
            key = f"{cat}|{v}|{box[0] // 50}|{box[1] // 50}"
            jobs.append((key, cat, v, box, p, rest))
    todo = [j[:5] for j in jobs if j[0] not in cache]
    report["cached"] = len(jobs) - len(todo)
    if todo and not cache_only:
        img.load()

        def ask(job):
            key, cat, v, box, _ = job
            crop = _crop(img, box)
            data, _ = ask_json(crop, f"Line number on the outlined connector: {v}. Which letter is in its flag end?",
                               FLAG_SCHEMA, max_tokens=3000, effort="medium", system=FLAG_SYSTEM, model=model,
                               tag={"layer": "V", "role": "flag letter", "tile": v})
            return key, {**data, "value": v, "bbox": box, "model": model}

        with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for fut in cf.as_completed([ex.submit(ask, j) for j in todo]):
                try:
                    k, res = fut.result()
                    cache[k] = res
                    report["asked"] += 1
                except Exception as exc:  # noqa: BLE001 — the bare number stays
                    report["errors"].append(f"{type(exc).__name__}: {exc}"[:160])
        json.dump(cache, open(path, "w"), indent=1)
    for key, cat, v, box, p, rest in jobs:
        letter = str(cache.get(key, {}).get("letter", "")).strip().upper()
        if len(letter) == 1 and letter.isalpha():
            old, new = str(p["value"]), letter + v + rest
            vals = resolved["answers"].get(cat, [])
            if old in vals:
                vals[vals.index(old)] = new
            if new in vals and vals.count(new) > 1:
                vals.remove(new)
            resolved["answers"][cat] = sorted(set(vals))
            p["value"] = new
            p["flag_letter"] = {"letter": letter, "evidence": cache[key].get("evidence"), "model": model}
            report["letters"] += 1
    return report
