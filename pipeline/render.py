"""
Layer A, step 0 — PDF to native-resolution page images.

Each page becomes data/renders/page{N}_full.png, which is what every later
layer reads. Scanned sheets carry one embedded raster per page; that raster is
extracted as-is rather than re-rendered, so the pipeline sees exactly the
scanned pixels at their true resolution (the whole design depends on never
downscaling tag text, see the architecture guide §2). Pages without a single
dominant raster are rendered at 300 DPI instead.

The text-layer probe is recorded alongside: a born-digital PDF would carry its
tags as real text, which is cheaper and exact compared with any vision read.
"""

from __future__ import annotations

import json
from pathlib import Path

import fitz  # PyMuPDF

from pipeline.paths import RENDERS

FALLBACK_DPI = 300


def next_page_number() -> int:
    nums = [int(p.stem[4:-5]) for p in RENDERS.glob("page*_full.png") if p.stem[4:-5].isdigit()]
    return max(nums, default=0) + 1


def _native_pixmap(doc: fitz.Document, page: fitz.Page) -> tuple[fitz.Pixmap, str]:
    """The page's own scanned raster when there is exactly one, else a render."""
    images = page.get_images(full=True)
    if len(images) == 1:
        pix = fitz.Pixmap(doc, images[0][0])
        if pix.n - pix.alpha >= 4:          # CMYK → RGB so PNG can hold it
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix, "embedded raster"
    return page.get_pixmap(dpi=FALLBACK_DPI), f"rendered at {FALLBACK_DPI} DPI"


def render_pdf(pdf_path: str | Path, first_page_number: int | None = None) -> list[dict]:
    """Write every page of `pdf_path` as page{N}_full.png. Returns one record per page."""
    doc = fitz.open(pdf_path)
    n = first_page_number or next_page_number()
    out = []
    for i, page in enumerate(doc):
        pix, how = _native_pixmap(doc, page)
        target = RENDERS / f"page{n + i}_full.png"
        pix.save(target)
        meta = {
            "page": n + i,
            "source_pdf": Path(pdf_path).name,
            "source_page_index": i,
            "width": pix.width,
            "height": pix.height,
            "method": how,
            "text_layer_chars": len(page.get_text().strip()),
        }
        json.dump(meta, open(RENDERS / f"page{n + i}_meta.json", "w"), indent=1)
        out.append(meta)
    return out
