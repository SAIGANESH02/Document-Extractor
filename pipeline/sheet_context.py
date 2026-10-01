"""
Layer B — sheet context.

One vision call over the whole sheet that extracts the drawing's *own* legend:
sheet id, symbol dictionary, system prefixes, note-block regions, sheet type.

This is the generalization mechanism for an unseen Doc 4 (§2.7). Every sheet
defines its own symbology in-sheet, so reading that at runtime is what lets the
downstream prompts adapt without retuning. It is also what tells Layer E which
page regions are prose (notes, title block) whose tag-shaped strings must be
suppressed — the §9 risk #7 trap where LR Note 2 on Doc 2 *lists*
PCV2816/2817/2818 as text, none of which are field instruments.

The page is sent DOWNSCALED on purpose. Legend and note text is set large;
it survives the resize, and the sheet-wide layout question ("where are the
blocks?") needs the whole page in one view. This is the one stage where
downscaling is correct — everything tag-level goes through native-resolution
tiles instead (§2.3).
"""

from __future__ import annotations

from PIL import Image

from pipeline.llm import ask_json

CONTEXT_MAX_PX = 2200

SCHEMA = {
    "type": "object",
    "properties": {
        "sheet_id": {"type": "string", "description": "Drawing number, e.g. PID-1-SS-LR20519"},
        "title": {"type": "string"},
        "sheet_type": {"type": "string", "enum": ["pid", "table_sheet", "other"]},
        "revision": {"type": "string"},
        "system_prefixes": {
            "type": "array",
            "description": "Commodity/system codes defined on this sheet",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "meaning": {"type": "string"},
                },
                "required": ["code", "meaning"],
                "additionalProperties": False,
            },
        },
        "symbols": {
            "type": "array",
            "description": "Symbol dictionary as stated by this sheet's legend",
            "items": {
                "type": "object",
                "properties": {
                    "shape": {"type": "string", "description": "e.g. hexagon, diamond, triangle"},
                    "meaning": {"type": "string"},
                },
                "required": ["shape", "meaning"],
                "additionalProperties": False,
            },
        },
        "suppress_regions": {
            "type": "array",
            "description": (
                "Bboxes of PROSE blocks (notes, legends, title block, revision "
                "table). Tag-shaped strings inside these are references, not "
                "field instruments, and must not be extracted."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["notes", "legend", "title_block", "revision_table", "other"],
                    },
                    "bbox": {
                        "type": "object",
                        "description": "Pixel coords in the image exactly as shown",
                        "properties": {
                            "x0": {"type": "integer"}, "y0": {"type": "integer"},
                            "x1": {"type": "integer"}, "y1": {"type": "integer"},
                        },
                        "required": ["x0", "y0", "x1", "y1"],
                        "additionalProperties": False,
                    },
                },
                "required": ["kind", "bbox"],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "array",
            "description": "Numbered note text, verbatim",
            "items": {"type": "string"},
        },
    },
    "required": [
        "sheet_id", "title", "sheet_type", "revision",
        "system_prefixes", "symbols", "suppress_regions", "notes",
    ],
    "additionalProperties": False,
}

PROMPT = """This is a full engineering drawing sheet.

Do NOT try to read the small field tags — a separate native-resolution pass
handles those. Your job is only the sheet's own metadata and legend.

Extract:
1. The drawing number and title from the title block.
2. Whether this is a P&ID (schematic of pipes/instruments) or a table-heavy
   sheet (its content is mostly tabular data).
3. Every system/commodity prefix the legend defines, with its meaning.
4. Every symbol the legend defines (hexagon, diamond, triangle, etc.) and what
   it indicates.
5. Bounding boxes of PROSE blocks: notes lists, legend blocks, the title block,
   revision tables. Be generous with these boxes — they are used to suppress
   false extractions, and a tag-shaped string quoted inside a note is a
   reference, not a piece of field equipment.
6. The numbered notes, verbatim.

Bounding boxes must be in pixel coordinates of the image exactly as shown."""


def sheet_context(page: Image.Image, model: str | None = None) -> tuple[dict, dict]:
    """Returns (context, usage). Bboxes are rescaled to NATIVE page coords."""
    s = min(1.0, CONTEXT_MAX_PX / max(page.size))
    small = page.resize((int(page.width * s), int(page.height * s)), Image.LANCZOS)

    ctx, usage = ask_json(small, PROMPT, SCHEMA, max_tokens=8000, effort="high",
                          model=model, tag={"layer": "B"})

    # The model saw the downscaled image; the rest of the pipeline is native.
    from pipeline.llm import box_scale
    sx, sy = box_scale(model, small.width, small.height)
    inv = 1 / s
    for r in ctx.get("suppress_regions", []):
        b = r["bbox"]
        r["bbox"] = [int(b["x0"] * sx * inv), int(b["y0"] * sy * inv),
                     int(b["x1"] * sx * inv), int(b["y1"] * sy * inv)]
    ctx["_scale_applied"] = round(inv, 4)
    return ctx, usage
