"""
RULES.yaml — load, save with version history, and a changelog.

Rules are subject-matter knowledge written down, not model weights, so every
change is kept: the previous file goes to data/rules/history/ and the change
itself (what, why, from where) is appended to data/rules/changelog.json. Each
rule carries `note` (why it exists) and `source` (where that knowledge came
from), which is why the file is written by this module rather than edited as
free text — comments would not survive a programmatic save, fields do.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil

import yaml

from pipeline.paths import DATA, RULES_PATH

HISTORY = DATA / "rules" / "history"
CHANGELOG = DATA / "rules" / "changelog.json"

HEADER = """\
# ---------------------------------------------------------------------------
# Observation -> answer-key category mapping (Layer E).
#
# Layer D's observations are cached, so a change here re-runs Layer E in
# milliseconds: no model calls, and every edit is explainable in one sentence.
# Changes made through the viewer's rule assistant are versioned in
# data/rules/history/ with a changelog in data/rules/changelog.json.
#
# Each rule:
#   category : the answer-key category to emit under
#   source   : where this knowledge came from (legend, requested category,
#              drawing inspection, SME answer, review decision, rule assistant)
#   note     : why the rule exists, in one or two sentences
#   when     : ALL of these must match (case-insensitive regex, searched)
#   unless   : NONE of these may match
#   emit     : text     -> the observation's text verbatim (default)
#              capture  -> first capture group of `pattern`
#              split    -> split text on / | , and spaces; emit each token
#                          (keep_legend_codes: only codes the sheet's legend defines)
#              const    -> the literal `value`
#              connector_id -> off-page connector id: flag letter + line number
#                          ("20845 J" or "J 20845" -> J20845; no letter -> 20845)
#              template -> `template` with {text} and {name}, where {name} is
#                          group 1 of `pattern` searched in `pattern_field`
#   min_conf : drop observations below this read confidence
#
# Rules run in order; one observation may satisfy several rules and is emitted
# under each. That is deliberate: the answer key overlaps (PI2793 is under both
# compound_gauge and pressure_indicators), so categories are views, not a
# partition.
# ---------------------------------------------------------------------------

"""

RULE_KEY_ORDER = ["category", "source", "note", "when", "unless", "emit", "pattern_field",
                  "pattern", "template", "value", "keep_legend_codes", "min_conf"]


def load() -> dict:
    return yaml.safe_load(open(RULES_PATH))


def _ordered(rule: dict) -> dict:
    out = {k: rule[k] for k in RULE_KEY_ORDER if k in rule}
    out.update({k: v for k, v in rule.items() if k not in out})
    return out


def dump(doc: dict) -> str:
    """Readable YAML: header, one block per rule, then the settings."""
    def block(obj) -> str:
        return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=88,
                              default_flow_style=False)

    parts = [HEADER, "rules:\n"]
    for r in doc.get("rules", []):
        text = block([_ordered(r)])
        parts.append("\n" + "".join("  " + ln if ln.strip() else ln
                                    for ln in text.splitlines(keepends=True)))
    rest = {k: v for k, v in doc.items() if k != "rules"}
    parts.append("\n" + block(rest))
    return "".join(parts)


def save(doc: dict, change: dict) -> str:
    """Write RULES.yaml, keeping the previous version and logging the change.
    Returns the history file name of the previous version."""
    HISTORY.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    prev = HISTORY / f"RULES_{stamp}.yaml"
    shutil.copy(RULES_PATH, prev)
    text = dump(doc)
    yaml.safe_load(text)                       # never write a file that will not load
    RULES_PATH.write_text(text)
    log = json.load(open(CHANGELOG)) if CHANGELOG.exists() else []
    log.append({**change, "at": dt.datetime.now().isoformat(timespec="seconds"),
                "previous_version": prev.name})
    json.dump(log, open(CHANGELOG, "w"), indent=1)
    return prev.name


def changelog() -> list[dict]:
    return json.load(open(CHANGELOG)) if CHANGELOG.exists() else []
