"""
Layer C benchmark — does a text detector earn its place in the pipeline?

Answers three questions with numbers rather than assumptions:

  Q1  Does a two-stage detector find the tags at all, where Tesseract found
      zero? (§2.2)
  Q2  Does the detector need the same tiling the frontier model needs, or can
      it take a full 5088px page? (mirrors §2.3)
  Q3  Is recognition good enough to be a useful second opinion on V/Y-class
      confusions, or is it detection-only value?

Outputs: data/bench/layer_c_results.json + per-page overlay PNGs.

Usage:  python scripts/benchmark.py [--pages 1 2 3] [--read]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.detect import LayerC, Region, tile_grid  # noqa: E402

from pipeline.paths import BENCH, RENDERS  # noqa: E402

# Hand-counted ground truth for Q1. Two tiles I counted by eye from the
# native-resolution crops; crops kept in the private workspace.
# Format: page -> tile box -> number of distinct text regions visible.
HANDCOUNT: dict[int, dict] = {
    1: {"box": (400, 900, 1650, 2150), "expected": None, "note": "sample lines, V113/V116"},
    2: {"box": (1850, 640, 3100, 1890), "expected": None, "note": "dense CP-166B panel"},
}


def overlay(page: Image.Image, regions: list[Region], out: Path, tiles=None) -> None:
    im = page.convert("RGB")
    d = ImageDraw.Draw(im)
    if tiles:
        for t in tiles:
            d.rectangle(t, outline=(200, 200, 255), width=2)
    for r in regions:
        # Colour by region height: small = the tags we care about.
        c = (255, 0, 0) if r.height < 40 else (255, 140, 0)
        d.rectangle(r.bbox, outline=c, width=3)
    im.resize((im.width // 3, im.height // 3), Image.LANCZOS).save(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", nargs="+", type=int, default=[1, 2, 3])
    ap.add_argument("--read", action="store_true", help="also run recognition (Q3)")
    args = ap.parse_args()

    results: dict = {"generated": time.strftime("%Y-%m-%d %H:%M"), "pages": {}}

    print("loading detector…", flush=True)
    det = LayerC(mode="detect")

    for pg in args.pages:
        src = RENDERS / f"page{pg}_full.png"
        page = Image.open(src)
        entry: dict = {"size": page.size}
        print(f"\n=== page {pg}  {page.size} ===", flush=True)

        # --- Q2: full page vs tiled -------------------------------------
        for tiled in (False, True):
            regions, meta = det.detect_page(page, tiled=tiled)
            small = [r for r in regions if r.height < 40]
            meta["small_regions"] = len(small)
            meta["median_height"] = (
                int(sorted(r.height for r in regions)[len(regions) // 2])
                if regions
                else None
            )
            key = "tiled" if tiled else "full_page"
            entry[key] = meta
            print(
                f"  {key:10s} regions={meta['regions']:5d}  "
                f"small(<40px)={meta['small_regions']:5d}  "
                f"median_h={meta['median_height']}  {meta['seconds']}s",
                flush=True,
            )
            if tiled:
                overlay(
                    page, regions, BENCH / f"page{pg}_layerC_tiled.png",
                    tiles=tile_grid(*page.size),
                )
                entry["regions_sample"] = [r.to_dict() for r in regions[:5]]
                json.dump(
                    [r.to_dict() for r in regions],
                    open(BENCH / f"page{pg}_regions.json", "w"),
                    indent=1,
                )
            else:
                overlay(page, regions, BENCH / f"page{pg}_layerC_fullpage.png")

        entry["tiling_gain"] = (
            round(entry["tiled"]["regions"] / max(1, entry["full_page"]["regions"]), 1)
        )
        results["pages"][pg] = entry

    # --- Q3: recognition on one dense tile ------------------------------
    if args.read:
        print("\n=== Q3: recognition sample ===", flush=True)
        rd = LayerC(mode="detect+read")
        for pg, spec in HANDCOUNT.items():
            page = Image.open(RENDERS / f"page{pg}_full.png")
            regs = rd.detect_tile(page, spec["box"], tile_id="sample")
            reads = [
                {"text": r.text, "conf": round(r.text_score or 0, 3), "bbox": r.bbox}
                for r in regs
                if r.text
            ]
            results.setdefault("recognition", {})[pg] = {
                "box": spec["box"],
                "note": spec["note"],
                "n": len(reads),
                "reads": reads,
            }
            print(f"  page{pg} tile {spec['box']}: {len(reads)} reads", flush=True)
            for x in reads:
                print(f"    {x['conf']:.2f}  {x['text']!r}")

    out = BENCH / "layer_c_results.json"
    json.dump(results, open(out, "w"), indent=2)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
