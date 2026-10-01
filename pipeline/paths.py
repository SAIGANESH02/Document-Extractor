"""
Every filesystem location the pipeline reads or writes, in one place.

Generated artefacts (page renders, per-layer caches, run records) live under
`data/`, which is gitignored: they are large, regenerable, and may contain
client drawings.
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENV_FILE = PROJECT_ROOT / ".env"
ANSWER_KEY = PROJECT_ROOT / "answer_key.json"
RULES_PATH = Path(__file__).resolve().parent / "RULES.yaml"

DATA = PROJECT_ROOT / "data"
RENDERS = DATA / "renders"   # page{N}_full.png — native-resolution page images
CACHE = DATA / "cache"       # per-layer results, reused unless a run is forced fresh
RUNS = DATA / "runs"         # one record per finished run, plus review decisions
BENCH = DATA / "bench"       # Layer C benchmark output

for _d in (RENDERS, CACHE, RUNS, BENCH):
    _d.mkdir(parents=True, exist_ok=True)
