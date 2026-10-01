"""
Layer A, step 0 — a document (PDF or image) to native-resolution page images.

Each page becomes data/renders/page{N}_full.png, which is what every later
layer reads.

PDF pages come in two kinds, and must be told apart:
  * SCANS carry one raster that covers the whole page. That raster is
    extracted as-is, so the pipeline sees the scanned pixels at their true
    resolution (re-rendering could only resample them).
  * DIGITAL PDFs (an exported invoice, a CAD export) are drawn from vectors
    and text, and may embed small images such as a logo. These are rendered
    at RENDER_DPI. Treating "one embedded image" as "a scan" once turned an
    invoice into its 40-point logo, so coverage of the page is what decides.

Images (PNG, JPG, TIFF) are taken as they are: one image, one page.

The text-layer probe is recorded alongside: a born-digital PDF carries its
text as real characters, which is cheaper and exact compared with any vision
read.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from pipeline.paths import CACHE, RENDERS, RUNS

RENDER_DPI = 300
SCAN_COVERAGE = 0.90          # an embedded image covering ≥90% of the page is a scan
# A page with NO text layer whose largest image covers at least half the page is
# also a scan — e.g. a landscape drawing placed rotated, with margins, on a
# portrait letter page (68% coverage). Rendering that page instead turned the
# exam sheet sideways at half resolution.
SCAN_COVERAGE_NO_TEXT = 0.50
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
DOC_TYPES = {".pdf"} | IMAGE_TYPES


def next_page_number() -> int:
    nums = [int(p.stem[4:-5]) for p in RENDERS.glob("page*_full.png") if p.stem[4:-5].isdigit()]
    return max(nums, default=0) + 1


def _clear_page(n: int) -> None:
    """A page number is about to hold new content: drop everything cached for
    the old one. Caches are keyed by page number, so a stale entry would
    otherwise be reused as if it described the new document."""
    for d in (CACHE, RUNS):
        for f in d.glob(f"page{n}_*"):
            if f.is_dir():
                shutil.rmtree(f)
            else:
                f.unlink()
    rp = RUNS.parent / "rules" / "proposals" / f"page{n}.json"
    if rp.exists():
        rp.unlink()


def _page_pixmap(doc: fitz.Document, page: fitz.Page) -> tuple[fitz.Pixmap, str]:
    """The page's own scanned raster when one image covers the page; else a render."""
    area = page.rect.width * page.rect.height
    no_text = not page.get_text().strip()
    for img in page.get_images(full=True):
        rects = page.get_image_rects(img[0])
        covered = max((r.width * r.height for r in rects), default=0)
        share = covered / area if area else 0
        if share >= SCAN_COVERAGE or (no_text and share >= SCAN_COVERAGE_NO_TEXT):
            pix = fitz.Pixmap(doc, img[0])
            if pix.n - pix.alpha >= 4:          # CMYK → RGB so PNG can hold it
                pix = fitz.Pixmap(fitz.csRGB, pix)
            return pix, "scan: embedded raster extracted"
    return page.get_pixmap(dpi=RENDER_DPI), f"digital PDF: rendered at {RENDER_DPI} DPI"


def _sha(path: Path) -> str | None:
    """Fingerprint of the decoded pixels, not the file bytes: the same image
    re-encoded by another tool must count as unchanged."""
    if not path.exists():
        return None
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        return hashlib.sha1(f"{rgb.size}".encode() + rgb.tobytes()).hexdigest()


def _write(n: int, save, width: int, height: int, meta: dict) -> dict:
    target, tmp = RENDERS / f"page{n}_full.png", RENDERS / f"page{n}_incoming.png"
    save(tmp)
    # Same pixels as before (the same document re-rendered): keep its caches —
    # they are paid for and still true. Different pixels: they describe
    # another document, so they go.
    if _sha(tmp) != _sha(target):
        _clear_page(n)
    tmp.replace(target)
    meta["sha1"] = _sha(target)
    meta = {"page": n, "width": width, "height": height, **meta}
    json.dump(meta, open(RENDERS / f"page{n}_meta.json", "w"), indent=1)
    return meta


def render_pdf(pdf_path: str | Path, first_page_number: int | None = None) -> list[dict]:
    """Write every page of `pdf_path` as page{N}_full.png. Returns one record per page."""
    doc = fitz.open(pdf_path)
    n = first_page_number or next_page_number()
    out = []
    for i, page in enumerate(doc):
        pix, how = _page_pixmap(doc, page)
        out.append(_write(n + i, pix.save, pix.width, pix.height, {
            "source_pdf": Path(pdf_path).name, "source_page_index": i, "method": how,
            "text_layer_chars": len(page.get_text().strip())}))
    return out


def ingest_image(image_path: str | Path, page_number: int | None = None) -> list[dict]:
    """A PNG/JPG/TIFF becomes one page, pixels unchanged (multi-frame TIFFs:
    one page per frame)."""
    n = page_number or next_page_number()
    out = []
    with Image.open(image_path) as im:
        frames = getattr(im, "n_frames", 1)
        for i in range(frames):
            im.seek(i)
            rgb = im.convert("RGB")
            out.append(_write(n + i, rgb.save, rgb.width, rgb.height, {
                "source_pdf": Path(image_path).name, "source_page_index": i,
                "method": "image file, used as-is", "text_layer_chars": None}))
    return out


def ingest(path: str | Path, first_page_number: int | None = None) -> list[dict]:
    """Any supported document → pages."""
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return render_pdf(path, first_page_number)
    if ext in IMAGE_TYPES:
        return ingest_image(path, first_page_number)
    raise ValueError(f"unsupported file type {ext!r}; use one of {sorted(DOC_TYPES)}")
