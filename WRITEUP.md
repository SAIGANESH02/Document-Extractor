# Xmentium — Write-up

## What we're building

A pipeline that reads a scanned engineering drawing and returns its contents as structured data —
**Doc 1: 100% recall, Doc 2: 100%, and every cell of Doc 3's tables right** — with each value traceable
to the pixels and the rule that produced it.

**The problem.** The three Seabrook Station sheets are 5088 × 3296 px scans with no text layer. Tags
are ~18 px tall, in a thin CAD font where V/Y and 0/O look alike, drawn over pipe lines. Classic OCR
(Tesseract) found zero tags. Sending a whole page to a vision model fails too: the API shrinks it to
2576 px and `V113` becomes `V1I3`.

| Sheet | What it is | Output | Judged by |
| --- | --- | --- | --- |
| Docs 1, 2 | P&IDs (piping and instrument diagrams) | `{document_id, answers: {category: [values]}}` | Answer keys (sparse spot-checks) |
| Doc 3 | Service-water sheet with tables | Every table, cell for cell (CSV + Markdown) | 100% cell accuracy |
| Doc 4 | Unseen, given live | Either of the above | Live review with the SMEs |

**What matters most is the unseen Doc 4**, not the score on Docs 1–2. So the design separates a
*general reader* that describes what is drawn from a *small, editable rules file* that turns those
descriptions into the categories asked for. Nothing is fine-tuned: knowledge about the drawings
lives in rules anyone can read, test and change in the 10-minute live window.

## End-to-end pipeline

AI models only *describe* what is drawn; plain Python plus an editable rules file decides which
category each thing belongs to.

```mermaid
flowchart TD
    IN["Document in<br/>PDF (scanned or digital) or PNG / JPG / TIFF"] --> A
    A["A · Ingest<br/>full-resolution page; 15 overlapping 1250 px tiles"] --> B
    B["B · Sheet context (1 AI call)<br/>legend, system codes, drawing number, note boxes"] --> C & D & T
    subgraph P["Run in parallel: C, D and T start together after B"]
        C["C · Text detector<br/>PaddleOCR, local — cannot invent text"]
        D["D · Tag reader<br/>1 AI call per tile — text, shape, attached to"]
        T["T · Tables<br/>find tables, read each from a full-res crop"]
    end
    D --> E["E · Resolve (plain Python, no AI)<br/>drop text in notes, dedupe seams, apply rules"]
    R["RULES.yaml<br/>edited live · AI proposes, you approve"] --> E
    E --> ANS["answers.json<br/>{document_id, answers}"]
    E --> S["Score + review<br/>recall, coverage, queue"]
    C -- coverage --> S
    T --> TB["Tables out<br/>CSV + Markdown"]
```

| Layer | Job | Tool | Why this tool |
| --- | --- | --- | --- |
| A · Ingest | PDF or image → full-resolution page; cut into tiles | PyMuPDF, Pillow | Tiles stay under the API's 2576 px limit, so tag text is never shrunk |
| B · Sheet context | Read the legend, system codes, drawing number; box the notes and title block | 1 vision call (Opus 5) | Each sheet defines its own symbols, so reading them at run time is how Doc 4 adapts |
| C · Detector | Find every region with text | PaddleOCR (local CPU) | Can't invent text, so it's an honest check on what the AI skipped |
| D · Tag reader | Per tile: text, symbol shape, what it's attached to, box | 1 vision call per tile (Opus 5) | Reports what it sees, never categories, so a new category never needs a new read |
| T · Tables | Find tables, read each one cell by cell | 1 call to find (Opus 5) + 2 reads per table, voted (Sonnet 5 + Gemini Flash; Gemini Pro breaks ties) | One table per call at full resolution; two different readers catch each other's glyph mistakes |
| E · Resolve | Map readings to answer categories | Python + `RULES.yaml` | Same input, same output; instant and free to re-run; every answer explainable |

Around it: a scorer (recall against the key, precision as a lower bound, coverage, exact vs.
after-cleanup matches); a viewer showing the full-resolution crop and rule behind any value, live
cost per AI call, a review queue and a rule assistant; and `scripts/extract.py doc.pdf`, which
writes `answers.json` and the tables in one command.

## Cascade run (an alternative Layer D)

Two cheap readers check each other; only their disagreements go to a stronger judge. We escalate on
*disagreement*, not the model's own confidence — junk readings scored 0.88 too.

```mermaid
flowchart TD
    IN["15 tiles + the sheet legend"] --> LA["Cheap reader A<br/>as run: Gemini 3.8 Flash"] & LB["Cheap reader B<br/>as run: Gemini 3.1 Pro"]
    LA & LB --> CMP["Compare readings<br/>pair by position, then: same text?"]
    CMP --> AG["Agree (42%)<br/>kept as is"]
    CMP --> CF["Conflict<br/>same spot, different text"]
    CMP --> SO["Solo<br/>only one reader saw it"]
    CF & SO --> Q{"Could it change an answer?<br/>outside notes, ≤ 40 chars"}
    Q -- no --> K["Keep a cheap reading<br/>the more confident one"]
    Q -- yes --> J["Judge model<br/>as run: Gemini 3.1 Pro<br/>the real text, or nothing there"]
    AG & K & J --> M["Merge — anything judged 'not there' is dropped"]
    M --> OUT["Layer E, then review queue"]
```

**Result: on these sheets the cascade costs more and adds nothing.**

| Setup | Doc 1 recall | Doc 2 recall | Cost, both docs | Time, both docs |
| --- | --- | --- | --- | --- |
| Cascade: Gemini Flash + Gemini 3.1 Pro → Gemini 3.1 Pro | 93.9% | 50% | $3.53 | 12 min |
| Gemini 3.8 Flash alone | 93.9% | 50% | $0.40 | 2 min |
| Claude Opus 5 alone | 100% | 75% | $8.58 | 15 min |

The readers agreed on only 42% of readings, so 245 of 290 disputes on Doc 1 went to the judge —
mostly one reader reporting text the other missed. It paid for all three models and landed where
the cheaper reader alone already was.

## How rules raise recall

**Rule changes took Doc 1 from 84.8% to 97.0% without a single new AI call**; a prompt fix took it
to 100%. Each fix came from looking at a miss in the inspector, seeing what the model *actually
wrote*, and adjusting the rule — never the value.

| Step | What changed | Doc 1 (33) | Doc 2 (4) | New AI calls? |
| --- | --- | --- | --- | --- |
| Starting rules | Hand-written from the legend and the key's category names | 84.8% | 50% | — |
| Pump tag | Find `P-392` inside "VAC PUMP P-392" | | | No |
| Panels and sinks | Join a name and its tag read separately: "PRIMARY SAMPLE PANEL" + `CP-166A` | | | No |
| Boundary codes | Split "DM SS" on spaces; keep only codes the sheet's legend defines | | | No |
| Spacing | Stop squeezing spaces out of multi-word values | **97.0%** | 50% | No |
| Prompt: one bubble, one tag | Read `PCV` over `2829` as one tag; stop nudging the model toward "U-bend" | **100%** | 50% | Yes |
| Connector letter | Keep the letter in the arrow head: `Z 20350` → `Z20350` | 100% | **75%** | No |
| Visual check (Layer V) | Crop each PCV bubble and ask: does its stem end on a U-bend loop? | 100% | **100%** | Yes ($0.05–0.24/sheet) |

**The visual check replaced word-matching for a shape-defined category.** The U-bend rule used to
match words in the model's description, so landmark mentions ("left of the U-bend loop") counted and
`PCV2829`, described without a U-bend, was missed. Layer V outlines each PCV bubble in red on a
full-resolution crop and asks one yes/no question; it found `PCV2829`. It also says yes for six PCVs
the key does not list (`PCV2768`–`2771`, `2826`, `2827`) — each stem ends on a U-shaped element
labelled "VAR. PRESS. REG. ELEMENT", a sharper question for the SMEs.

The same rule fixes lifted every reader on the same saved readings (cascade 78.8% → 90.9%, GPT-5.5
75.8% → 81.8%). Every rule change is re-scored on *both* keyed sheets before it is kept — the
viewer's rule assistant does this automatically when it proposes changes for a new sheet.

## Benchmark

Same rules, same legend read and same prompt for every setup; re-scored from saved readings
(`scripts/benchmark_models.py`, full tables in `data/bench/benchmark.md`).

### Layer D — reading tags (Docs 1–2)

| Reader | Doc 1 recall (33) | Doc 2 recall (4) | Exact matches (of 37) | Coverage, Doc 1 | Cost, both docs | Time, both docs |
| --- | --- | --- | --- | --- | --- | --- |
| **Claude Opus 5** | **100%** | **75%** | 29 | 76.5% | $8.58 | 15 min |
| GPT-5.5 | 87.9% | 75% | 27 | 54.1% | $10.49 | 19 min |
| Gemini 3.8 Flash | 93.9% | 50% | 26 | 41.8% | **$0.40** | **2 min** |
| Gemini 3.1 Pro | 93.9% | 25% | 25 | 64.9% | $2.31 | 9 min |
| Claude Sonnet 5 | 81.8% | 25% | 23 | 50.5% | $4.70 | 23 min |
| Claude Haiku 4.5 | 51.5% | 25% | 14 | 5.9% | $0.63 | 3 min |
| Cascade, Gemini only | 93.9% | 50% | 26 | 56.4% | $3.53 | 12 min |

These Layer D numbers use the wording rule for U-bend PCVs; with the visual check (Layer V), Opus
reaches 100% on Doc 2. *Exact matches* need no punctuation cleanup (the key writes `PID-1-SS_LR20519`; the drawing prints
`PID-1-SS-LR20519`). *Coverage* is the share of text the independent detector found that the reader
also reported; it also reflects box accuracy. Doc 2's key has 4 values, so one value is 25 points.

### Layer B — reading the legend (all 3 sheets)

| Model | Drawing numbers right | Doc 1 recall with it | Doc 2 recall with it | Cost, 3 sheets |
| --- | --- | --- | --- | --- |
| **Claude Opus 5** | 3/3 | 100% | 75% | $0.18 |
| Claude Sonnet 5 | 3/3 | 100% | 75% | $0.08 |
| Gemini 3.1 Pro | 3/3 | 100% | 75% | $0.04 |
| GPT-5.5 | 3/3 | 100% | 75% | $0.63 |
| Claude Haiku 4.5 | 2/3 (`LR20791` for `LR20794`) | 90.9% | 75% | $0.02 |

### Layer T — tables (Doc 3: 3 tables, 62 data cells, each model read twice)

| Model | Data cells exact (read 1 / read 2) | Same answer both reads | Cost per read | Time per read |
| --- | --- | --- | --- | --- |
| **Claude Sonnet 5** | **62 / 62** | 100% | $0.029 | 32 s |
| **Gemini 3.8 Flash** | **62 / 62** | 100% | $0.006 | 20 s |
| Gemini 3.1 Pro | 62 / 62 | 98.8% | $0.018 | 47 s |
| Claude Opus 5 | 48 / 48 | 88% | $0.069 | 27 s |
| GPT-5.5 | 44 / 44 | 98.8% | $0.152 | 54 s |
| Claude Haiku 4.5 | 41 / 41 | 98.7% | $0.006 | 8 s |

Opus read the 14 drain-valve tags `V523…` as `Y523…` on both reads: the CAD font's V/Y confusion
repeats within one model, which is why the best tag reader is not the best table reader.

## Recommended setup

**Demo: the accurate setup** — about $3–6 and 6–10 minutes per sheet. For volume, the budget setup
does ~94% of the job for under $0.30 a sheet.

| Layer | Accurate (demo default) | Budget | Why |
| --- | --- | --- | --- |
| B · legend | Claude Opus 5 ($0.06) | Claude Sonnet 5 ($0.03) | One call per sheet; every later layer inherits its mistakes. Haiku got a drawing number wrong |
| C · detector | PaddleOCR server detector (free, ~3 min fresh) | same | Can't invent text; the mobile detector missed answer-key hexagons |
| D · tag reader | Claude Opus 5 ($3–5.50, 5–10 min) | Gemini 3.8 Flash ($0.16–0.25, ~1 min) | Only Opus reaches 100% on Doc 1; Flash reaches 94% at 1/20th the cost |
| T · tables | Claude Sonnet 5 ($0.03) | Gemini 3.8 Flash ($0.006) | Both 62/62 on two reads; Opus misreads V as Y |
| Cascade | Off | Off | Costs the sum of its readers; scored no better |

## Tried and rejected, and known failure modes

| Idea | What happened | Decision |
| --- | --- | --- |
| Tesseract OCR | Zero tags; `2864` read as `2564` even at 3× zoom | Rejected |
| Whole-page vision calls | Page shrunk to 2576 px; `V113` → `V1I3` | Rejected — tile at full resolution |
| Escalate on the model's own confidence | Right and junk readings both ~0.88; only 2 of 707 below the floor | Rejected — escalate on disagreement |
| Two cheap readers + a judge | 42% agreement; same recall as Flash alone at 9× the cost | Off |
| PaddleOCR mobile detector | 11× faster, missed answer-key hexagons | Rejected |
| Self-hosted open vision model | 1–2 days of setup; a GPU as a single point of failure in the demo | Deferred — one interface keeps the swap to one file |

**Known failure modes.** V/Y, 0/O, 1/I in the CAD font — and the mistakes repeat within one model.
Categories defined by a symbol depended on how the model describes it — for U-bend PCVs now handled
by a visual yes/no check (Layer V), which other shape-defined categories could reuse. Note/title boxes drawn in the
wrong place can drop real values (the review queue lists them). Rules written for one sheet may
catch junk on another (the hexagon rule on Doc 3). Providers differ in conventions (Gemini's
0–1000 boxes silently broke its results until fixed).

## Future steps

**Cheapest wins first, then models** — every step judged on the same benchmark before it is kept.

1. **Few-shot symbol examples** (days, cents per sheet). Add 3–5 reference crops to the tag reader's
   prompt (U-bend PCV, relief-valve PCV, hexagon, diamond). Targets the main miss — shapes described
   inconsistently; prompt caching keeps the repeated examples cheap.
2. **Read digital PDFs' text layer directly** before any vision call — exact and free for exported drawings.
3. **Grow the evaluation set.** The keys hold 37 values; every accept / reject / correct in the review
   queue is a labelled example, so normal use builds the dataset.
4. **Self-hosted vision model behind the existing interface** (e.g. Qwen2.5-VL), run on the same
   benchmark. Its native high-resolution input could simplify tiling, and drawings stay on the
   customer's network.
5. **Fine-tune only the narrow visual skills** (tag reading on crops, telling symbol types apart) once
   a few hundred reviewed examples exist. Categories stay in rules — editable live and explainable.
   Hold out whole drawings, not crops, so the test is not leaked.

**On security:** self-hosting keeps drawings off third-party servers, which matters for nuclear
safety-related sheets — but you then own patching, access control and the model supply chain. A
cloud-hosted model in the customer's own account with no data retention gives much of the same
protection without running GPUs; the right choice depends on the customer's contract.

## Exam sheet (Doc 4): `PID-1-WLD-LR20218`

**The unseen sheet ran end to end in 6.5 minutes for $3.78 — all 15 tiles read, none failed, 9 categories —
and the requested `reactor_coolant` category was added live for $0.04 more.** A scanned P&ID like
Docs 1–2 (Seabrook waste-processing liquid drains, reactor coolant system).

**Two bugs surfaced first, both fixed:**
- *Rotated scan read at half resolution.* The PDF places the scan rotated 90° with margins on a portrait
  page (68% coverage); the ingest step only accepted ≥90% and re-rendered the page — sideways, at half
  resolution. Now a page with no text layer and an image covering ≥50% is read as the scan, at full resolution.
- *Paying for retries that could not succeed.* At half resolution dense tiles overflowed the 32,000-token
  output budget and were retried with the same budget, three times each. Now an overflowing tile is read
  as four quarters, and overflowed calls' cost is recorded.

| Category | Count | Examples |
| --- | --- | --- |
| `reactor_coolant` (requested live) | 11 | `J20845 RC-E-1 INNER SEAL`, `K20845 RC-E-1 OUTER SEAL`, `L20841 RC-P-1A *2 SEAL`, `H20841 LOOP 1` |
| `valves` | 54 | `V110`, `V112`, `V113`, `V131` … |
| `system_boundary_interfaces` | 5 | `CS`, `NG`, `RC`, `SI`, `WLD` (from the sheet's own legend) |
| `instrument_bubbles` | 8 | `FE`, `FT`, `FV`, `LSH`, `LT`, `PI`, `PT`, `TE` |
| `pressure_indicators` | 2 | `PI1410`, `PI1411` |
| `vacuum_pumps` / `gas_cylinders` | 1 + 1 | `P-338`; `TK-55 REACTOR COOLANT DRAIN TANK` |
| `continuation_connections (hexagon)` | 14 | `20219`, `20222` — plus junk (below) |
| Tables | 2 | Revision block; reference-drawing list (no data tables on this sheet) |

**How `reactor_coolant` was added live.** Built like Doc 2's `demineralized_water`: off-page connectors
labelled REACTOR COOLANT, reported as **flag letter + line number + what the connector is**. One rule reads
the label inside the connector (`REACTOR COOLANT RC-E-1 20845 J INNER SEAL` → `J20845 RC-E-1 INNER SEAL`);
one reads a separate note pointing at a connector (`REACTOR COOLANT LOOP 2` → `G20842 LOOP 2`). Four loop
connectors' flag letters were read visually on crops (H, G, D, J) for $0.04. The legend line, title block
and equipment that only mentions reactor coolant are excluded; Docs 1–2 still score 100%.

**To check or fix live:** `D20843` (D or O?); single letters `B`–`L` and `CC LOOP A` in the hexagon
category (one rule edit: require a number); system-prefixed valves (`RC-V145`–`147`, `CS-V1162`–`1165`,
`SI-V104`) sit in `unclassified.json` — do they count as `valves`?; coverage 64% vs Doc 1's 77%.

## Working assumptions (built in; confirm with the SMEs live)

| Open point | What the pipeline does | Why this is the safer choice |
| --- | --- | --- |
| What defines `pressure_control_valves (U-bend_pipe)` | A PCV whose stem ends on a U-bend loop, checked visually on a crop of each bubble (Layer V) | The category is named after the U-bend; loosening the rule to catch Doc 2's one miss would be fitting to Doc 2 |
| `instrument_bubbles` | The prefix types on the sheet (PI, TI, PCV), not instances | Matches Doc 1's key exactly |
| Connector letters (`A/X/Y/Z 20350`) | Emit every connector with its letter | Keeps recall; extra values go to review instead of guessing why the key lists only Z |
| Title-block and revision tables | **Read them too, labelled** (title block / revision / reference list; a setting turns this off) | For "every table exactly", a missing table fails and a labelled extra one costs cents |
| Categories for Doc 4 | Every category we have rules for; the rule assistant proposes new ones from Doc 4's legend; `unclassified.json` lists tag-like readings no rule claimed | An unknown category still shows up somewhere to review live |

**Tables are now read twice, by different models, and voted cell by cell.** Sonnet 5 and Gemini 3.8
Flash each read every table; where they disagree, Gemini 3.1 Pro breaks the tie, and any cell still
unsettled is flagged. One strong reader repeats its own glyph mistakes (Opus V→Y; Sonnet 1→I on one
crop) while different models make different ones — on Doc 3 the vote gives **62/62 data cells**, for
about $0.17 a sheet. Table boxes are now drawn by the strong model with wider margins, after one crop
lost a column.
