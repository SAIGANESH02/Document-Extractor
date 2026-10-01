"""
Layer C — independent text detection over engineering drawings.

Role in the pipeline:
  1. RECALL ORACLE. A segmentation-based detector cannot hallucinate a text
     region that is not in the pixels. Comparing its region count against the
     observations Layer D (frontier VLM) reports gives us a recall signal
     measured against the drawing itself rather than against the sparse
     answer keys.
  2. SECOND OPINION. Where both layers read the same region, agreement is a
     confidence boost and disagreement (V113 vs Y113) is an auto-raised flag.

This module is deliberately standalone: no API calls, no network at runtime
once models are cached, CPU-only.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Iterable, Literal

import numpy as np
from PIL import Image

# Paddle is chatty on import and per-call; keep the pipeline output readable.
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("FLAGS_call_stack_level", "0")
for _n in ("ppocr", "paddlex", "paddle"):
    logging.getLogger(_n).setLevel(logging.ERROR)

# Tile geometry must match Layer D so coverage is comparable tile-for-tile.
TILE_PX = 1250
OVERLAP = 0.15

# PP-OCR detectors resize the input so the longest side <= limit_side_len.
# On a 5088px page that is a catastrophic downscale (the same failure mode as
# the frontier API's 2576px cap, §2.3). Keeping it >= TILE_PX means a tile is
# passed through at native resolution.
LIMIT_SIDE_LEN = 1280


@dataclass
class Region:
    """One detected text region, in PAGE coordinates."""

    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1
    score: float
    poly: list[tuple[int, int]] = field(default_factory=list)
    text: str | None = None
    text_score: float | None = None
    tile_id: str | None = None

    @property
    def area(self) -> int:
        x0, y0, x1, y1 = self.bbox
        return max(0, x1 - x0) * max(0, y1 - y0)

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    def to_dict(self) -> dict:
        return asdict(self)


def tile_grid(
    width: int, height: int, tile: int = TILE_PX, overlap: float = OVERLAP
) -> list[tuple[int, int, int, int]]:
    """Deterministic overlapping grid. Last row/column are flush to the edge so
    no strip is dropped; that makes the final step smaller, never larger."""
    step = int(tile * (1 - overlap))
    xs = list(range(0, max(1, width - tile + 1), step)) or [0]
    ys = list(range(0, max(1, height - tile + 1), step)) or [0]
    if xs[-1] + tile < width:
        xs.append(width - tile)
    if ys[-1] + tile < height:
        ys.append(height - tile)
    return [
        (x, y, min(x + tile, width), min(y + tile, height)) for y in ys for x in xs
    ]


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    if not inter:
        return 0.0
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua else 0.0


def dedupe(regions: Iterable[Region], iou_thresh: float = 0.4) -> list[Region]:
    """Collapse duplicates produced by tile overlap. Highest score wins.

    O(n^2) but n is ~1-2k per page, which is milliseconds. Kept simple
    deliberately: this runs in the demo path and must be trivially auditable.
    """
    kept: list[Region] = []
    for r in sorted(regions, key=lambda r: -r.score):
        if not any(_iou(r.bbox, k.bbox) > iou_thresh for k in kept):
            kept.append(r)
    return kept


class LayerC:
    """Text detector (and optional recognizer) over a full drawing page."""

    def __init__(
        self,
        mode: Literal["detect", "detect+read"] = "detect",
        limit_side_len: int = LIMIT_SIDE_LEN,
        det_model: str = "PP-OCRv5_server_det",
        box_thresh: float = 0.5,
    ):
        self.mode = mode
        self.limit_side_len = limit_side_len
        if mode == "detect":
            from paddleocr import TextDetection

            self._engine = TextDetection(
                model_name=det_model,
                limit_side_len=limit_side_len,
                limit_type="max",
                box_thresh=box_thresh,
            )
        else:
            from paddleocr import PaddleOCR

            self._engine = PaddleOCR(
                lang="en",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
                text_det_limit_side_len=limit_side_len,
                text_det_limit_type="max",
                text_det_box_thresh=box_thresh,
            )

    # -- internals ---------------------------------------------------------

    def _run(self, arr: np.ndarray) -> list[tuple[np.ndarray, float, str | None, float | None]]:
        out = self._engine.predict(arr)
        if not out:
            return []
        r = out[0]
        polys = r.get("dt_polys", r.get("rec_polys", []))
        scores = r.get("dt_scores", [1.0] * len(polys))
        texts = r.get("rec_texts", [None] * len(polys))
        tscores = r.get("rec_scores", [None] * len(polys))
        return list(zip(polys, scores, texts, tscores))

    # -- public ------------------------------------------------------------

    def detect_tile(
        self, page: Image.Image, box: tuple[int, int, int, int], tile_id: str = ""
    ) -> list[Region]:
        """Detect within one tile; returns regions in PAGE coordinates."""
        x0, y0, _, _ = box
        arr = np.array(page.crop(box).convert("RGB"))
        regions = []
        for poly, score, text, tscore in self._run(arr):
            p = np.asarray(poly, dtype=float).reshape(-1, 2)
            p[:, 0] += x0
            p[:, 1] += y0
            xs, ys = p[:, 0], p[:, 1]
            regions.append(
                Region(
                    bbox=(int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())),
                    score=float(score),
                    poly=[(int(a), int(b)) for a, b in p],
                    text=text,
                    text_score=float(tscore) if tscore is not None else None,
                    tile_id=tile_id,
                )
            )
        return regions

    def detect_page(
        self, page: Image.Image, tiled: bool = True
    ) -> tuple[list[Region], dict]:
        """Detect across a whole page. Returns (regions, timing/meta)."""
        t0 = time.time()
        if tiled:
            boxes = tile_grid(*page.size)
            raw: list[Region] = []
            for i, b in enumerate(boxes):
                raw.extend(self.detect_tile(page, b, tile_id=f"t{i:02d}"))
        else:
            boxes = [(0, 0, *page.size)]
            raw = self.detect_tile(page, boxes[0], tile_id="full")

        regions = dedupe(raw) if tiled else raw
        meta = {
            "tiled": tiled,
            "n_tiles": len(boxes),
            "raw_regions": len(raw),
            "regions": len(regions),
            "dropped_as_duplicate": len(raw) - len(regions),
            "seconds": round(time.time() - t0, 2),
            "limit_side_len": self.limit_side_len,
            "mode": self.mode,
        }
        return regions, meta


def coverage(
    detector_regions: list[Region],
    observations: list[tuple[int, int, int, int]],
    iou_thresh: float = 0.3,
) -> dict:
    """THE metric this layer exists for (§8.2).

    Of the text regions the detector found, how many did Layer D account for?
    Unmatched detector regions are concrete, localized misses — each one is a
    crop we can open and look at.
    """
    matched = [
        r for r in detector_regions
        if any(_iou(r.bbox, o) > iou_thresh for o in observations)
    ]
    n = len(detector_regions)
    return {
        "detector_regions": n,
        "observations": len(observations),
        "matched": len(matched),
        "coverage": round(len(matched) / n, 4) if n else None,
        "unmatched_bboxes": [r.bbox for r in detector_regions if r not in matched],
    }
