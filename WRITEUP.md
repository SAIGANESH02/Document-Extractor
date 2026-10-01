# Write-up — structured extraction from scanned engineering drawings

**Approach.** The sheets are 5088×3296 scans with no text layer, so this is a vision problem. The
pipeline splits it into steps that need different tools:

| Layer | Job | How |
| --- | --- | --- |
| A | Ingest | PDF page → native raster (text layer probed); 15 overlapping 1250 px tiles |
| B | Read the sheet's own legend | One vision call on the whole sheet: drawing number, symbols, system codes, note/title boxes |
| C ∥ D ∥ T | Detect · read · tables | In parallel. **C**: PaddleOCR detector, local, finds every text region (cannot invent text). **D**: one vision call per tile reporting *what is drawn* — text, symbol, what it is attached to — never categories. **T**: finds data tables, reads each from a full-resolution crop |
| E | Map to categories | Plain Python + `RULES.yaml`: confidence floor, drop text inside note/title boxes, dedupe tile seams, regex rules over text + symbol + attachment |

Two decisions carry the design. **Native-resolution tiles**: the vision API caps images at
2576 px, which halves tag text and turns `V113` into `V1I3`; PaddleOCR has the same problem.
**Observations, not categories, from the model**: categories differ per sheet, so they live in an
editable rules file. A new category is a rule edit and a re-run in milliseconds on cached readings
— no new model calls — and every answer traces to one reading, one box and one rule.

**Evaluation.** The answer keys are sparse spot-checks (Doc 1 lists 2 valves; the sheet has 14+), so
**recall against the key is the headline**, key-precision is reported as a lower bound, and values
not in the key go to a review queue rather than counting as errors. Layer C adds a recall signal
against the pixels: *coverage*, the share of detected text regions the model read. Matches are
also split into exact vs. only-after-ignoring-punctuation, since a strict grader would differ.

**Results** (same rules, legend read and prompt for every reader; `data/bench/benchmark.md`):

| Reader (Layer D) | Doc 1 recall (33) | Doc 2 recall (4) | Cost, both docs | Time |
| --- | --- | --- | --- | --- |
| Claude Opus 5 (default) | **100%** | **75%** | $8.58 | 15 min |
| GPT-5.5 | 87.9% | 75% | $10.49 | 19 min |
| Gemini 3.8 Flash | 93.9% | 50% | $0.40 | 2 min |
| Gemini 3.1 Pro | 93.9% | 25% | $2.31 | 9 min |
| Claude Sonnet 5 | 81.8% | 25% | $4.70 | 23 min |

The one Doc 2 miss is `PCV2829`: read correctly, but the model describes its stem as reaching a
regulator valve rather than a U-bend, so the shape-defined rule does not fire. Doc 2's key has
four values, so it is weak evidence; Gemini Flash is the cost/speed option at ~20× cheaper.

**Tables (Doc 3).** Layer T finds the sheet's 3 data tables and reads each from a full-resolution
crop. Against a hand-checked reference, Sonnet 5 and Gemini 3.8 Flash got **62/62 data cells** on two
independent reads; Opus read the 14 drain-valve tags `V523…` as `Y523…` every time (48/62), so tables
run on Sonnet 5. Title-block and revision tables are excluded on purpose (open question below).

**Tried and rejected.** Tesseract (zero tags found). Whole-page vision calls (downscaling destroys
tags). Routing to a bigger model on the model's own confidence (correct and junk readings both sit
near 0.88). A two-cheap-readers + flagship cascade (escalate only where the readers disagree): the
readers agreed on only ~42% of readings, so most went to the flagship — it cost the sum of all three
models and scored no better than Gemini Flash alone ($3.53 vs $0.40, same recall). PaddleOCR's fast mobile detector (11× faster, missed answer-key hexagons). A tight
"looks like a tag" filter in the cascade (silently deleted real values). Self-hosting an open
vision model (setup and demo risk outweighed a benefit the brief does not grade; kept possible
behind one `ask_json` interface).

**Known failure modes.** V/Y and 0/O confusion in the CAD font — repeatable within one model,
which is why two different readers can beat one strong one. Categories defined by a symbol
depend on the model's wording (the U-bend PCVs). Note/title
boxes drawn slightly wrong suppress real values (surfaced in review). The hexagon rule picks up
non-hexagon labels on an unseen sheet (page 3). Providers differ in box conventions (Gemini: 0–1000).

**With more time.** A small detector trained on symbol crops for the shape-defined categories;
re-reading regions Layer C found but no model read; few-shot symbol examples in the Layer D prompt;
a distilled open model once enough reviewed labels exist.

**Questions for you (the SMEs).**
1. Is the U-bend loop what defines `pressure_control_valves (U-bend_pipe)`, or how those two happen to be drawn?
2. Is `instrument_bubbles` the set of prefix types present (PI, TI, PCV) rather than instances?
3. Doc 2's demineralized-water connectors read `A/X/Y/Z 20350`; the key lists only `Z20350`. Is the letter part of the connection id, and why only Z?
4. Do title-block and revision tables count as "every table on the sheet"?
