"""
Layer D — observe.

One structured vision call per native-resolution tile. Returns generic
*observations*, never answer-key categories:

    {text, kind, symbol, attached_to, line_color, bbox, confidence}

WHY THE SPLIT MATTERS (§6.1). Categories are per-document and open-ended —
Doc 1 has 16, Doc 2 has 3, and Doc 2 introduced `demineralized_water`, which
Doc 1 never had. If categories lived in this prompt, an unseen category on
Doc 4 would mean re-prompting and re-inferencing all 15 tiles during the
10-minute live window. Because Layer D only reports what it *sees*, a missing
category is one line in RULES.yaml plus a Layer E re-run on cached
observations — seconds, and fully explainable on the spot.

Tiles are 1250px native: under both the 2576px long-edge and 3.75MP caps, so
the API performs no resampling and coordinates map 1:1 to page pixels.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import threading
import time
from typing import Callable

from PIL import Image

from pipeline.llm import ask_json

MAX_WORKERS = 4  # wall-clock matters for the demo; stay well inside rate limits
RETRIES = 3      # a tile lost to a 429/529 is a hole in the page, so retry it

# Per-provider throttling. A free-tier Gemini key rate-limits PER MINUTE, so
# four workers firing fifteen tiles exhausts the budget in seconds and 12 of 15
# tiles come back 429 — which scores as terrible accuracy when it is really
# just throttling. Pacing the request starts is what makes the comparison
# measure the model rather than the billing plan.
PROVIDER_LIMITS = {
    "anthropic": {"workers": MAX_WORKERS, "min_interval": 0.0},
    # Paid tier now: the free-tier pacing (2 workers, 6 s apart) is no longer needed.
    "gemini":    {"workers": 4,           "min_interval": 1.0},
    "openai":    {"workers": 4,           "min_interval": 0.0},
}

_pace_lock = threading.Lock()
_last_start = [0.0]


def _pace(min_interval: float) -> None:
    """Block until at least `min_interval` has passed since the last request
    start, across all worker threads."""
    if min_interval <= 0:
        return
    with _pace_lock:
        wait = min_interval - (time.monotonic() - _last_start[0])
        if wait > 0:
            time.sleep(wait)
        _last_start[0] = time.monotonic()
# A budget shared with xhigh thinking. At 8000 the densest five tiles on page 1
# truncated mid-JSON and were lost outright, which is why this is not tight.
MAX_TOKENS = 32000

OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The tag/label EXACTLY as printed. Empty if the symbol carries no text.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "valve", "instrument_bubble", "continuation_connection",
                            "boundary_interface", "equipment", "line_label",
                            "flexible_hose", "gas_cylinder", "panel_or_sink",
                            "annotation", "other",
                        ],
                    },
                    "symbol": {
                        "type": "string",
                        "description": (
                            "The drawn shape, described plainly: 'bowtie/gate valve', "
                            "'circle bubble', 'hexagon', 'split diamond', "
                            "'relief valve (spring)', 'U-bend loop in pipe', "
                            "'cylinder', 'filter', 'pump'. Describe what you SEE."
                        ),
                    },
                    "attached_to": {
                        "type": "string",
                        "description": (
                            "What the symbol or its stem/leader actually connects to, "
                            "described plainly from the drawing (a valve body, a "
                            "loop in the pipe, a tank, a gauge, a pipe run). For a "
                            "connector or box, include any label printed beside it. "
                            "Say 'not visible' if the connection is off the tile."
                        ),
                    },
                    "line_color": {
                        "type": "string",
                        "enum": ["green", "red", "black", "blue", "purple", "none"],
                    },
                    "bbox": {
                        "type": "object",
                        "description": "Pixel coords of the TEXT in this tile image",
                        "properties": {
                            "x0": {"type": "integer"}, "y0": {"type": "integer"},
                            "x1": {"type": "integer"}, "y1": {"type": "integer"},
                        },
                        "required": ["x0", "y0", "x1", "y1"],
                        "additionalProperties": False,
                    },
                    "confidence": {"type": "number", "description": "0-1, your read certainty"},
                },
                "required": ["text", "kind", "symbol", "attached_to", "line_color", "bbox", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["observations"],
    "additionalProperties": False,
}

SYSTEM = """You read scanned engineering drawings (P&IDs) at native resolution.

You report OBSERVATIONS ONLY — what is drawn and what is written. You never
classify things into project-specific categories; a later deterministic stage
does that.

Rules:
- Transcribe tags EXACTLY. Do not normalise, expand or correct them.
- This is a single-stroke CAD font on a scan. V and Y are easily confused, as
  are 0/O, 1/I, 5/S, 8/B, 2/Z. Look carefully and lower `confidence` when the
  glyph is genuinely ambiguous rather than guessing confidently.
- Describe the symbol you actually see. Do not infer a symbol from the tag
  prefix — the prefix is a naming convention, the symbol is the evidence.
- Report every distinct piece of text, including line labels and sizes.
- An instrument bubble often prints its type on the top line and its number
  below (PCV over 2829). Report the whole bubble as ONE observation with both
  lines: text "PCV 2829". Never split one bubble into two observations.
- If a region is blank or illegible, report nothing for it. Never invent a tag."""


# Changes whenever the instructions or the output schema change. It is part of
# the cache key, so readings made under an older prompt are never reused as if
# they came from this one.
import hashlib as _hashlib
import json as _json
PROMPT_FINGERPRINT = _hashlib.sha1(
    (SYSTEM + _json.dumps(OBSERVATION_SCHEMA, sort_keys=True)).encode()).hexdigest()[:8]


def _prompt(ctx: dict, box: tuple[int, int, int, int]) -> str:
    lines = [
        f"Tile from sheet {ctx.get('sheet_id', '(unknown)')}, "
        f"page pixels x{box[0]}-{box[2]}, y{box[1]}-{box[3]}.",
        "",
    ]
    if ctx.get("symbols"):
        lines.append("This sheet's own legend defines these symbols:")
        lines += [f"  - {s['shape']}: {s['meaning']}" for s in ctx["symbols"]]
        lines.append("")
    if ctx.get("system_prefixes"):
        lines.append("System/commodity prefixes on this sheet:")
        lines += [f"  - {p['code']} = {p['meaning']}" for p in ctx["system_prefixes"]]
        lines.append("")
    lines.append(
        "List every observation in this tile. Bounding boxes must be pixel "
        "coordinates within THIS tile image, not the full page."
    )
    return "\n".join(lines)


def _limits(model: str | None = None) -> dict:
    """Throttling follows the MODEL's provider, not the env default — in a
    cascade the two run side by side and have different rate limits."""
    if model is not None:
        from pipeline.models import provider_of
        p = provider_of(model)
    else:
        from pipeline.llm import provider
        p = provider()
    return PROVIDER_LIMITS.get(p, PROVIDER_LIMITS["anthropic"])


def observe_tile(
    tile: Image.Image, box: tuple[int, int, int, int], ctx: dict, tile_id: str,
    model: str | None = None, role: str = "reader",
) -> tuple[list[dict], dict]:
    """`tile` is a pre-cropped image. Cropping must happen on the calling
    thread: PIL lazily decodes, and concurrent crop() calls against one
    lazily-loaded Image corrupt the shared decoder ("broken PNG file")."""
    # Overloads and rate limits are transient and, at 4 workers over 15 tiles,
    # not rare. Without this a single 529 silently costs a whole tile's worth of
    # the page — the kind of gap that is invisible until someone asks why a tag
    # is missing.
    interval = _limits(model)["min_interval"]
    for attempt in range(RETRIES):
        try:
            _pace(interval)
            data, usage = ask_json(
                tile, _prompt(ctx, box), OBSERVATION_SCHEMA,
                max_tokens=MAX_TOKENS, effort="xhigh", system=SYSTEM, model=model,
                tag={"layer": "D", "role": role, "tile": tile_id},
            )
            break
        except Exception as exc:
            if attempt == RETRIES - 1:
                raise
            # A 429 means the budget is spent, not that the request was wrong —
            # back off far harder than for a transient overload.
            hard = "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc)
            time.sleep((20 if hard else 2) * (attempt + 1))
    from pipeline.llm import box_scale
    sx, sy = box_scale(model, tile.width, tile.height)
    out = []
    for o in data.get("observations", []):
        b = o.pop("bbox")
        o["bbox"] = [int(b["x0"] * sx) + box[0], int(b["y0"] * sy) + box[1],
                     int(b["x1"] * sx) + box[0], int(b["y1"] * sy) + box[1]]
        o["tile_id"] = tile_id
        if model:
            o["model"] = model      # provenance: which reader saw this
        out.append(o)
    return out, usage


def observe_page(
    page: Image.Image,
    tiles: list[tuple[int, int, int, int]],
    ctx: dict,
    on_progress: Callable[[int, int, int], None] | None = None,
    model: str | None = None,
    only_tiles: list[int] | None = None,
    role: str = "reader",
) -> tuple[list[dict], dict]:
    """Fan out across tiles. Returns (observations, usage totals)."""
    obs: list[dict] = []
    totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "errors": 0,
              "error_messages": []}
    done = 0

    page.load()                                   # force decode before threading
    crops = [page.crop(b) for b in tiles]         # crop serially — see observe_tile
    # `only_tiles` lets the cascade re-read just the tiles it could not settle,
    # rather than paying flagship rates for the whole page.
    idxs = list(range(len(tiles))) if only_tiles is None else list(only_tiles)

    with cf.ThreadPoolExecutor(max_workers=_limits(model)["workers"]) as ex:
        futs = {
            ex.submit(observe_tile, crops[i], tiles[i], ctx, f"t{i:02d}", model, role): i
            for i in idxs
        }
        for fut in cf.as_completed(futs):
            done += 1
            try:
                got, usage = fut.result()
                obs.extend(got)
                totals["input_tokens"] += usage["input_tokens"]
                totals["output_tokens"] += usage["output_tokens"]
                totals["calls"] += 1
            except Exception as exc:
                totals["errors"] += 1
                msg = f"{type(exc).__name__}: {exc}"[:200]
                if msg not in totals["error_messages"]:
                    totals["error_messages"].append(msg)
            if on_progress:
                on_progress(done, len(idxs), len(obs))
    return obs, totals
