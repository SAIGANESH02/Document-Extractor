"""
Layer T — table reproduction, for ANY sheet that carries tables.

Tables need a different output from tags: every cell copied exactly, in rows
and columns. Two steps:

  1. find   — one call on the downscaled page lists each table and its box.
              A sheet with no tables returns an empty list and the layer ends.
  2. read   — each table is cropped from the NATIVE page and read in its own
              call. Isolating one table per call is the biggest accuracy lever:
              the model's attention is not split across the sheet, and the
              crop keeps the text at full size.

A crop larger than the API limits (2576 px long edge, 3.75 MP) would be
resized by the server; we resize it ourselves instead and record the scale, so
the output says plainly when a table was read below native resolution.
"""

from __future__ import annotations

import concurrent.futures as cf
import csv
import io

from PIL import Image

from pipeline.llm import ask_json

FIND_MAX_PX = 2200
API_MAX_EDGE = 2576
API_MAX_PIXELS = 3_750_000
# Boxes from the downscaled view are approximate: on page 3 one came back 85 px
# short of its table's right edge and cut a column off. Pad generously — extra
# margin costs nothing, a missing column costs the table.
PAD_FRAC, PAD_MIN = 0.10, 80

FIND_SCHEMA = {
    "type": "object",
    "properties": {"tables": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Table title or heading, empty if none"},
            "bbox": {"type": "object", "properties": {
                "x0": {"type": "integer"}, "y0": {"type": "integer"},
                "x1": {"type": "integer"}, "y1": {"type": "integer"}},
                "required": ["x0", "y0", "x1", "y1"], "additionalProperties": False},
        },
        "required": ["title", "bbox"], "additionalProperties": False}}},
    "required": ["tables"], "additionalProperties": False,
}

FIND_PROMPT = """List every DATA TABLE on this engineering drawing sheet: grids of
rows and columns holding values (equipment schedules, valve lists, setpoint
tables, line lists and similar).

Do NOT list the title block, the revision block, the notes list, or the legend.
If the sheet has no data tables, return an empty list.

Bounding boxes are pixel coordinates in this image exactly as shown, and must
enclose the whole table including its title and header rows."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "header_rows": {"type": "integer", "description": "How many of the first rows are headers"},
        "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                 "description": "Every row, every cell, exactly as printed. Same column count in every row."},
        "notes": {"type": "string", "description": "Merged cells, illegible cells, anything uncertain"},
    },
    "required": ["title", "header_rows", "rows", "notes"], "additionalProperties": False,
}

READ_SYSTEM = """You transcribe one table from a scanned engineering drawing, cell by cell.

- Copy every cell EXACTLY as printed: same characters, units, punctuation and
  spacing. Never normalise, expand abbreviations, or correct values.
- Keep the table's own row and column order. Every row has the same number of
  cells; an empty cell is "".
- A merged cell: put its text in the first cell it spans, "" in the others,
  and say so in notes.
- This is a single-stroke CAD font on a scan: V/Y, 0/O, 1/I, 5/S, 8/B confuse
  easily. If a cell is genuinely illegible, write your best reading and list
  it in notes rather than guessing silently."""


def _fit_for_api(crop: Image.Image) -> tuple[Image.Image, float]:
    s = min(1.0, API_MAX_EDGE / max(crop.size), (API_MAX_PIXELS / (crop.width * crop.height)) ** 0.5)
    if s < 1.0:
        crop = crop.resize((int(crop.width * s), int(crop.height * s)), Image.LANCZOS)
    return crop, round(s, 3)


def find_tables(page: Image.Image, model: str | None = None) -> list[dict]:
    s = min(1.0, FIND_MAX_PX / max(page.size))
    small = page.resize((int(page.width * s), int(page.height * s)), Image.LANCZOS)
    data, _ = ask_json(small, FIND_PROMPT, FIND_SCHEMA, max_tokens=8000, effort="high",
                       model=model, tag={"layer": "T", "role": "find"})
    from pipeline.llm import box_scale
    sx, sy = box_scale(model, small.width, small.height)
    out = []
    for t in data.get("tables", []):
        b = t["bbox"]
        x0, x1 = int(b["x0"] * sx / s), int(b["x1"] * sx / s)
        y0, y1 = int(b["y0"] * sy / s), int(b["y1"] * sy / s)
        px = max(PAD_MIN, int((x1 - x0) * PAD_FRAC))
        py = max(PAD_MIN, int((y1 - y0) * PAD_FRAC))
        out.append({"title": t["title"],
                    "bbox": [max(0, x0 - px), max(0, y0 - py),
                             min(page.width, x1 + px), min(page.height, y1 + py)]})
    return out


def read_table(page: Image.Image, table: dict, idx: int, model: str | None = None) -> dict:
    crop, scale = _fit_for_api(page.crop(tuple(table["bbox"])))
    data, _ = ask_json(crop, f"Transcribe this table. Its title on the sheet: {table['title']!r}.",
                       READ_SCHEMA, max_tokens=32000, effort="xhigh", system=READ_SYSTEM,
                       model=model, tag={"layer": "T", "role": "read", "tile": f"table{idx}"})
    rows = data.get("rows", [])
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]      # ragged rows → rectangular
    return {"index": idx, "title": data.get("title") or table["title"], "bbox": table["bbox"],
            "header_rows": data.get("header_rows", 1), "rows": rows, "notes": data.get("notes", ""),
            "read_scale": scale, "n_rows": len(rows), "n_cols": width}


def read_tables(page: Image.Image, tables: list[dict], model: str | None = None,
                on_progress=None) -> tuple[list[dict], list[str]]:
    page.load()
    out, errors, done = [], [], 0
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(read_table, page, t, i, model): i for i, t in enumerate(tables)}
        for fut in cf.as_completed(futs):
            done += 1
            try:
                out.append(fut.result())
            except Exception as exc:  # noqa: BLE001 — reported per table
                errors.append(f"table {futs[fut]}: {type(exc).__name__}: {exc}"[:200])
            if on_progress:
                on_progress(done, len(tables))
    return sorted(out, key=lambda t: t["index"]), errors


def to_csv(table: dict) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(table["rows"])
    return buf.getvalue()


def to_markdown(table: dict) -> str:
    rows = table["rows"]
    if not rows:
        return f"### {table['title']}\n\n(empty)\n"
    h = max(1, table.get("header_rows", 1))
    cell = lambda c: str(c).replace("|", "\\|").replace("\n", " ")
    head = [" / ".join(filter(None, (rows[r][c] for r in range(min(h, len(rows))))))
            for c in range(len(rows[0]))]
    lines = ["| " + " | ".join(cell(c) for c in head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows[h:]]
    return f"### {table['title']}\n\n" + "\n".join(lines) + "\n"
