# Xmentium — Write-up (one page)

**The pipeline reads a scanned drawing and returns answer-key JSON plus every table — Doc 1 at 100%
recall, Doc 2 at 100%, Doc 3's tables 62/62 cells — with each value traced to its pixels and rule.**
Full version with diagrams and benchmarks: `WRITEUP.md`.

## Approach

The sheets are 5088×3296 scans with no text layer: Tesseract found zero tags, and a whole-page vision
call shrinks tags until `V113` reads `V1I3`. So each step uses the tool that fits it:

1. **A · Ingest** — PDF or image → full-resolution page, cut into 15 overlapping 1250 px tiles (tag text never shrunk).
2. **B · Sheet context** — one AI call reads the sheet's own legend, system codes and drawing number, and boxes the notes. This is how an unseen sheet adapts.
3. **C ∥ D ∥ T in parallel** — **C**: a local text detector (PaddleOCR) that cannot invent text, measuring what the AI skipped. **D**: one AI call per tile reports *what is drawn* (text, shape, what it's attached to) — never categories. **T**: reads each table from a full-resolution crop.
4. **E · Resolve** — plain Python and an editable `RULES.yaml` map readings to categories: instant, free to re-run, explainable.

**No fine-tuning — knowledge lives in rules.** Rule fixes alone took Doc 1 from 84.8% to 97.0% with no
new AI calls; a prompt fix took it to 100%. Every rule change is tested on both keyed sheets.
**Evaluation:** the keys are sparse spot-checks (Doc 1 lists 2 valves; the sheet has 14+), so recall
is the headline, precision is a lower bound, and extra values go to a review queue.

## Results

| Tag reader (Layer D) | Doc 1 recall (33) | Doc 2 recall (4) | Cost, both docs |
| --- | --- | --- | --- |
| **Claude Opus 5** | **100%** | **75%** | $8.58 |
| Gemini 3.8 Flash | 93.9% | 50% | $0.40 |
| GPT-5.5 | 87.9% | 75% | $10.49 |
| Cascade (2 cheap readers + judge) | 93.9% | 50% | $3.53 |

With the **visual check (Layer V)** — each PCV bubble cropped and asked "does its stem end on a U-bend?"
— Opus reaches **100% on Doc 2** (found `PCV2829`, $0.24); the table uses the older wording rule.

**Recommended:** Opus 5 for tags; tables read twice (Sonnet 5 + Gemini Flash) and voted cell by cell, Gemini Pro breaking ties —
62/62 on Doc 3 (one reader repeats its mistakes: Opus read `V523` as `Y523`); Opus 5 for the legend. About $3–6 per sheet; Gemini Flash does ~94% of the tag job at 1/20th the cost.

## Tried and rejected · known failure modes

**Rejected:** Tesseract (zero tags) · whole-page vision calls (tags shrunk) · escalating on the model's
own confidence (right and junk both ~0.88) · the cascade (readers agreed on 42% → 9× Flash's cost,
same recall) · PaddleOCR's fast detector (missed hexagons) · self-hosting for the demo (setup time, GPU risk).

**Failure modes:** V/Y and 0/O confusion, repeated within one model · symbol-defined categories depend
on the model's wording (U-bend PCVs now checked visually instead) · a misplaced note box can drop
real values (listed in review) · a rule written for one sheet can catch junk on another.

## Next steps

Few-shot symbol images in the tag reader's prompt → read digital PDFs' text layer directly → grow the
labelled set from review decisions → a self-hosted model behind the existing interface (drawings stay
on the customer's network) → fine-tune only tag reading and symbol types; categories stay in rules.

## Working assumptions (confirm with the SMEs live)

1. The U-bend PCV category is defined by the drawn U-bend — its name says so; loosening it to catch Doc 2's one miss would be fitting to Doc 2.
2. `instrument_bubbles` = prefix types (PI, TI, PCV) — matches Doc 1's key.
3. Every demineralized-water connector is emitted with its letter (`A/X/Y/Z 20350`) rather than guessing why the key lists only Z.
4. Title-block and revision tables are read too, labelled — a missing table fails "every table".
5. Doc 4 categories: the rule assistant proposes new rules; `unclassified.json` lists tag-like readings no rule claimed.
