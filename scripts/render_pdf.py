"""
Turn a PDF or image into native-resolution page images the pipeline can run on.

    python scripts/render_pdf.py doc4.pdf            # appended after existing pages
    python scripts/render_pdf.py doc4.pdf --first 4  # explicit page number
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.render import ingest  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--first", type=int, default=None, help="page number to assign the first page")
    a = ap.parse_args()
    for m in ingest(a.pdf, a.first):
        print(f"page{m['page']}: {m['width']}×{m['height']} ({m['method']}), "
              f"text layer {m['text_layer_chars']} chars")


if __name__ == "__main__":
    main()
