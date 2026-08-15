# The "scaled" pipeline experiment

**Started 2026-08-15.** An isolated, parallel track testing whether
newspaper structure can be derived from *classical* signal — Tesseract's
own hOCR layout output plus plain geometry — instead of paying an LLM to
look at the page image.

This file is the durable record of what was measured, what was decided,
and what was learned. It is written to be read by a future session with
no memory of this one. **Read it before touching `transcribe/scaled/`.**

> **Read `instructions/typesetting_practice.md` first.** A newspaper page
> is a designed, quantised artefact assembled on a fixed grid — four
> numbers (margin, column width, gutter, column count) with everything
> snapping to integer column spans. Most of the complexity in the earlier
> stages of this experiment came from treating that regular, designed
> object as an unknown natural signal. **Thinking like a typesetter is
> key to this project.**

---

## 1. Why this exists — the cost problem, measured

| | |
|---|---|
| Corpus | **70,063 pages**, 7,087 issues, 1861–2007 |
| Processed to date | 562 pages = **0.80%** |
| OCR+LLM route cost | **77,233 tok/page** (harness) / **103,710** (sum of `page_llm_calls`) |
| Wall clock | **93.4 s/page** |
| Extrapolated to full corpus | **5.4–7.3 billion tokens**, **~76 days** continuous |

Where the cost is, per page (from `page_llm_calls`):

| stage | tokens/page | share |
|---|---|---|
| `items` (segmentation) | 74,720 | **72%** |
| `cleanup` (OCR correction) | 28,991 | 28% |

**Note the two token figures disagree by ~34%** (harness aggregate is
consistently 68–81% of the per-call sum, 0.750 overall). Most likely the
harness figure excludes cache-read input tokens. Unresolved — do not
build a cost model on one number without saying which.

Also uncosted: the **pre-1980 column route has no token data at all**
(`column_transcripts.tokens_in/out` are NULL on all 3,126 rows,
`transcribe_runs` is empty). What it does have is wall clock, and it is
worse: ~1,005 s of agent time per page, roughly 5× the OCR route.

---

## 2. The discovery: Tesseract already gives us the layout

`ocr_llm.parse_hocr()` selects only `.//div[@class='ocr_carea']` and,
within it, each word's `bbox` + `x_wconf`. **Everything else Tesseract
emits is discarded.** Verified directly on
`transcribe/work/ocr_llm/1990-10-10/p2/page.hocr`:

| discarded | what it is | measured on that page |
|---|---|---|
| `ocr_separator` | printed rules — **a vertical one IS a column boundary** | 12, incl. `bbox=[1947,171,1967,1849]` = 20px × 1678px vertical rule |
| `ocr_photo` | image regions | 8, incl. a real 520×524 photo |
| `x_size` | x-height px, a font-size proxy | every line; body median **35**, headlines to **320** |
| `ocr_header` / `ocr_caption` / `ocr_textfloat` | Tesseract's **own** heading/caption/float classes | 10 header, 26 textfloat |
| `ocr_par` | paragraph structure | 135 |
| `baseline` | per-line skew | every line |

`ocr_separator` and `ocr_photo` are **siblings of `ocr_carea`**, direct
children of `ocr_page` — which is precisely why a carea-only XPath never
sees them.

This is the same signal `ocr-items.md` currently pays an LLM to derive by
looking at the image. **All 93 `.hocr` files are on disk, so recovering
it costs zero OCR and zero LLM.**

Corpus-wide recovery from the 90 parseable pages:
**5,081 regions (1,344 vertical rules, 1,511 photos), 560 `ocr_header`
lines, 447 `ocr_caption` lines** — all previously invisible.

### What more Tesseract can be made to emit — tested, not assumed

Re-ran the exact production command on 1997-07-16 p1 with
`-c hocr_font_info=1` and diffed the output:

| | production | with `hocr_font_info=1` |
|---|---|---|
| title keys | baseline, x_ascenders, x_descenders, x_size, x_wconf | **+ `x_fsize`** |
| bold/italic tags | none | **still none** |
| `x_font` (font name) | absent | **still absent** |
| element classes | (same 10) | (same 10) |

So the only real gain is **`x_fsize` — a per-WORD font size in points**
(measured: n=1921, min 4, median 9, max 75; 1501 words at 9pt = body).
That is finer-grained than `x_size`, which is per-line.

**Correction to an earlier note in this file:** bold/italic is *not*
"one flag away". `hocr_font_info` documents `<strong>`/`<em>` output,
but with the LSTM engine (`--oem 1`, what we run) Tesseract does not
report font attributes at all — empirically zero bold/italic tags and
zero `x_font`. Only the legacy engine reports those, and switching
engines would cost recognition accuracy. **Treat "no bold/italic
signal" as a fixed property of this setup, not a config gap.**

---

## 3. Architecture and decisions

### Isolation — total, by explicit direction
Everything lives in `transcribe/scaled/`. It writes only to schema-v15
tables and two additive columns. **Delete the package and production is
unaffected.** `ocr_llm.parse_hocr()` is deliberately untouched.

The isolation goes further than "don't modify": **the experiment does
not import from the rest of the repo at all**, even where reuse would be
sensible. `transcribe/scaled/_support.py` holds local copies of
`pct_to_px`/`px_to_pct` (from repo-root `coordinates.py`) and
`open_connection`/`new_uuid`/`now_iso` (from `transcribe/db.py`). There
are zero `from ..` or `import coordinates` lines in the package.

Rationale: a change inside the experiment cannot perturb production, and
the whole thing can be deleted without unpicking shared imports. Cost:
these copies can drift from their originals — **if the originals change
materially, reconcile by hand and note it in this file's update
history; do not silently re-point at them.**

The one thing deliberately *not* duplicated is the database file. The
experiment writes to the same `transcribe.db` on purpose, so its output
can be compared against production output in a single query.

Isolation was verified as behaviour-preserving: after removing every
cross-package import, the two reference pages score identically
(1997-07-16 p11 = 0.426, 1980-04-06 p11 = 0.776) and the escalation rate
is unchanged at 88/90.

### Factory shape
Each stage has its own CLI, its own readiness column, its own cadence —
the pattern already proven by `extract_terms.py`/`classify_terms.py`.
`pages.hocr_parsed_at` is stage 1b's readiness signal, exactly as
`items.terms_extracted_at` is Unit 3's.

### Confidence-gated escalation
Classic pass emits a confidence; only pages below `CONFIDENCE_GATE`
(0.60) go to an LLM. **The fraction below the gate is the headline
result of the whole experiment**, so it is a named constant, not a magic
number.

### Thresholds are measured, not chosen
| constant | value | evidence |
|---|---|---|
| `MIN_RULE_HEIGHT_PCT` | 25.0 | genuine column rules on 1990-10-10 p2 are 29.7% tall; median separator overall is 11.3% |
| `EDGE_MARGIN_PCT` | 2.0 / 97.0 | ~27% of tall vertical rules sit at the page edge (scan artefacts) |
| `MERGE_FRACTION` | 0.5 × median col width | `detection_methods_review.md` §12 flags the classical 7%-of-page constant as era-wrong |

Separator height distribution across 1,344 vertical rules:
min 0.8%, median 11.3%, max 99.9%; ≥25% tall: 330 (240 interior).

---

## 4. Tech choices, with the documentation that justified them

Every choice below was made by reading current documentation, **not from
recall**. See `feedback_read_docs_not_memory` in session memory.

### Viewers — the manifest is the contract, viewers are clients
The official [IIIF Cookbook viewer matrix](https://iiif.io/api/cookbook/recipe/matrix/)
settles this:

| viewer | annotations | polygon annos | notes |
|---|---|---|---|
| **TIFY** | ✅ | ❌ | source public ([subugoe/tify](https://github.com/subugoe/tify)), self-hostable, 141 kB gz, built on OpenSeadragon |
| **Theseus** | ✅ | ✅ | best annotation support, **but source not published** (only `embed`/`issues` repos under [github.com/theseus-viewer](https://github.com/theseus-viewer)); embed loads JS from theseusviewer.org at runtime; its own embed repo says *experimental* |
| **Mirador** | ✅ | ✅ | already have hard-won 3.4.2 config in `preview/iiif/mirador.html` |
| Clover | ❌ | ❌ | **no annotation support — ruled out** |
| Universal Viewer | ❌ | ❌ | **no annotation support — ruled out** |

**Status 2026-08-15: only Mirador is confirmed working.** TIFY does not
render overlays for these manifests even after fixing the
`view==='fulltext'` gate found in its bundle — that fix was predicted to
work and was falsified on device. Cause not yet found. Mirador is the
default; TIFY stays selectable for debugging only. Theseus was removed
entirely (hosted-only, unpublished source, and unreachable behind
Cloudflare Access).

Mirador working is itself the useful result: it independently proves the
manifest is valid and the Text Granularity extension is harmless, so a
TIFY-only failure is a TIFY problem, not a data problem.

**Original decision: emit one standards-compliant manifest and support all
three.** TIFY is the built-in (self-hosted, no third-party runtime
dependency, works behind the existing tunnel). Theseus and Mirador get
opened via the **IIIF Content State API**: parameter is `iiif-content`,
and per the spec *"If the Content State is a URI, it must not be
content-state-encoded"* — so a plain manifest URL needs no encoding.
That makes viewer support a one-line link, not an integration.

TIFY options that matter (read from its actual `src/config.js`, not its
README): `annotationsVisible` (clickable toggleable image overlays),
`layers`, `urlQueryKey`/`urlQueryParams` (bookmarkable QA links),
`viewer` (passes straight through to OpenSeadragon), `contentStateEnabled`.

**Known limitation:** TIFY does not render non-rectangular polygons, so
the old route's `items.geometry_polygon_json` staircases won't display.
Our detection output is rectangles, so this doesn't bite here.

### IIIF specifics
- Presentation **3.0**. Detection overlays go in Canvas **`annotations`**
  (not `items`) as labelled `AnnotationPage`s with
  `motivation: "supplementing"`; `items` is reserved for `painting`.
- Region targeting via the `#xywh=x,y,w,h` fragment.
- Image API **level 0** — `full/max/0/default.jpg` only, plain static
  files, no image server and no tiling.

### hOCR
Per the [hOCR spec](https://kba.github.io/hocr-spec/1.2/), `ocr_carea`
elements *"should appear in reading order"*. Note the spec defines
`x_fsize`, not `x_size` — `x_size` is a Tesseract extension, which is
why it is read defensively.

---

## 5. RESULT: what actually happened

### 5a. hOCR recovery — clear success
90/93 pages parsed (3 skipped: their `hocr_path` points at stale `/tmp`
files from an old comparison run). Zero block-count regression. 5,081
regions and 1,007 header/caption lines recovered for free.

### 5b. ARCHIVED — full-height column detection — negative result

**Escalation rate: 88/90 = 97.8%.** Confidence min 0.00, median 0.29,
max 0.78.

This is a real negative result and it should not be tuned away. What it
means:

**Full-height column detection is the wrong model for 1980+ pages.**
`layout_observations.md` already said so — *"1980s–2000s: modular — no
page-level grid at all"* — and this experiment now has direct
measurement to back it:

> On 1997-07-16 p11, a full-height x-projection of the 69%-wide
> right-hand region finds **zero** zero-coverage strips. Band the same
> region at 10% of page height and consistent gutters appear at
> **50.1–50.6%** and **73.2–73.4%**, but only across the y=50–80% bands.
> Different stacks of display ads put their gutters in different places;
> projecting the full height fills every gap.

Where the page genuinely has a printed column grid, the detector works
well: **1980-04-06 p11** (classified/ads page, real printed rules)
resolves to a clean 7-column grid at 3.7 / 15.4 / 26.8 / 39.0 / 50.2 /
61.5 / 74.0 / 94.8, confidence 0.776, **visually verified correct**.

### 5c. The confidence metric flattered itself — caught by rendering

**This is the most important process lesson in the experiment.**

The first version scored 1997-07-16 p11 at **0.853 and accepted it**.
Rendering the page showed the detector had found *one* boundary and
swallowed the whole right-hand ad region into a single 69%-wide
"column". The metric was built from `corroboration` and `rule_support`,
which only ask *"are the boundaries I found well-supported?"* — it had
no notion of **recall**.

This reproduced, exactly, the failure `post1980_layout_observations.md`
warns about: *"Aggregate stats flattered the pipeline because the
metrics share authorship with the code under review; bad cuts the
detector didn't recognise as bad don't raise flags."*

Fix: a `completeness` term that **multiplies** rather than adds — a
detector that missed half the page must not be rescuable by being very
sure about the half it got. It is computed per horizontal band, because
(per 5b) a full-height test cannot see modular structure.

Validated in both directions:
| page | visual truth | before | after |
|---|---|---|---|
| 1997-07-16 p11 | wrong | 0.853 accept | **0.426 escalate** |
| 1980-04-06 p11 | correct | 0.776 accept | **0.776 accept** |

**Rule for future work here: never accept a confidence number for this
pipeline without rendering the page. `transcribe/scaled/render_overlay.py`
exists for exactly this.**

---

### 5f. Stage 2 is now the UNDERLYING GRID — everything builds up from it

Following `instructions/typesetting_practice.md`: the page was set on a
fixed grid, so **fit four numbers, don't discover boundaries.**
`transcribe/scaled/detect_grid.py`:

1. Pool left+right edges of every block and line (per PAGE, not per
   issue — the photography varies too much between pages, and one page
   carries plenty of edges).
2. Cluster into peaks — the alignment positions the page actually uses.
3. Grid-search pitch and offset; keep the lattice explaining most peak
   weight.
4. Derive column width from each slot's dominant right-edge peak;
   gutter is the remainder of the pitch.

**Validated against the physical evidence** on 1980-04-06 p11 — the
fitted lattice vs the page's own printed column rules:

| grid | printed rule | error |
|---|---|---|
| 15.49 | 15.2 | 0.29 |
| 27.14 | 26.1–27.4 | 0.26 |
| 38.79 | 38.1–39.1 | 0.31 |
| 50.44 | 49.7–50.7 | 0.26 |
| 62.09 | 61.0–62.0 | 0.09 |
| 73.74 | 72.8–73.9 | 0.16 |

Every boundary within ~0.3%, gutter 0.86% (≈1 pica, as expected). The
8th slot at 85.4 has no printed rule because an ad spans two slots —
which is the model working, not failing.

Corpus: 90 pages fitted, median fit 0.75. Modal column counts 8 (39
pages) and 6 (27).

**Three bugs worth remembering**, all found by plotting/rendering rather
than by reading numbers:
- Raw hit-rate scoring made finer lattices always win, so a 7-column
  page fitted as 15 columns. Fixed with chance correction (each lattice
  line accepts ±tol, so random hit probability is 2·tol/pitch).
- Scoring every edge rather than peaks understated a *visually correct*
  grid at 0.20 — most edges on a newspaper page are ad interiors and
  centred headlines that never touch the grid.
- `pitch = span/n` silently assumed the last column *starts* at the text
  right edge; it *ends* there. The lattice drifted left by ≈gutter/n.
  Pitch is now searched, not derived.

`fit` is reported as a **diagnostic only — there is no gate.** Scoring
with escalation thresholds is archived as a dead end (see below).

### 5d. ARCHIVED — band-first segmentation

Because full-height columns are the wrong model for 1980+ (5b), stage 2
was re-cut around **bands**: a horizontal strip bounded by a wide
`ocr_separator` rule or by a y-gap no text line crosses. Columns are then
found *within* a band. `transcribe/scaled/detect_bands.py`, schema v16
adds `page_bands`.

Result:

| | full-height columns | band-first |
|---|---|---|
| Escalation | 97.8% (88/90) | **31.5% (28/89)** |
| Median confidence | 0.29 | **0.72** |
| Bands found | — | 266 (1–6 per page) |

The 266 figure was reproduced independently by a throwaway prototype
before the module existed — a useful check that the detector does what
it is believed to do.

**But rendering it shows the score is still too generous.** On the
*best*-scoring page (1980-04-06 p4, confidence 0.917):

```
band 0   y  3.6–20.2%   (16.6% tall)   2 col
band 1   y 21.6–36.8%   (15.2% tall)   2 col
band 2   y 36.8–99.2%   (62.4% tall)   2 col   <- 346 lines
```

Band 2 covers 62% of the page and contains several distinct articles with
different internal column counts, all described as one 2-column strip.
Bands 0 and 1 put column edges through the masthead and a photo.

So the band unit is still **too coarse**, and `coverage` + `regularity`
do not penalise a band that is internally heterogeneous — structurally
the same weakness as 5c, one level up. **The likely fix is recursive
splitting (an XY-cut): after finding a band, look for cuts inside it
rather than stopping at the first pass. Not built.**

### 5e. Stage navigation in the viewer

`preview/scaled/iiif/viewer.html`'s selector is grouped by pipeline
stage, so the stages can be stepped through and compared on the same
canvases:

```
1 · Tesseract   blocks | lines | blocks + lines
2 · Columns     bands | full-height
3 · Items       (disabled — not built)
4 · Refined     (disabled — not built)
```

Items and Refined are rendered `disabled` rather than hidden: the shape
of the pipeline stays visible without implying they exist.

Each stage has its own manifest (`build_iiif.py`): `manifest_blocks`,
`manifest_lines`, `manifest.json`, `manifest_columns` (with each raw
signal — separator / left-edge / valley — as its own layer so
disagreement is inspectable), `manifest_bands`. A layer is omitted
entirely when a stage produced nothing for a page, so an empty layer
never masquerades as a real result.


### 5g. Archived dead end: confidence scoring

`detect_columns.py` and `detect_bands.py` are in
`transcribe/scaled/archive/` with a README explaining why. Short version:
they tried to *discover* layout from weak signals and then score how much
to trust the answer. Every version of that score ended up certifying its
own detector, and each failure was caught only by looking at a rendered
page. On a designed grid the question is simply "do the page's alignment
positions land on this lattice?" — which needs a fit, not a confidence
model.

## 6. What this implies for the plan

1. **Recovering hOCR signal is worth doing regardless** — free photos,
   captions, headings, rules and font size, already proven.
2. **Columns are the wrong first stage for 1980+ — band-first is
   better but not yet sufficient.** Built and measured (5d): escalation
   97.8% -> 31.5%. Next step is recursive splitting inside a band.
   `tools/post1980/page_layout.py:237 find_whitespace_bands` and
   `column_grid.py:145 pick_measurement_bands` remain useful references.
3. **Column detection likely pays off on pre-1980** — 63.6% of the
   corpus (44,595 pages), real printed column grids, and
   `find_columns.py` already proves the signal exists there. Untested
   here only because those pages have never been OCR'd.
4. `items` is 72% of token cost, so it remains the real prize.

---

## 7. How to run

```bash
# 2. bands (the working stage-2 model for 1980+)
python3 -m transcribe.scaled.detect_bands run [--date YYYY-MM-DD]
python3 -m transcribe.scaled.detect_bands show YYYY-MM-DD --page N
python3 -m transcribe.scaled.detect_bands report

# IIIF manifests for every stage + the stage-grouped viewer
python3 -m transcribe.scaled.build_iiif YYYY-MM-DD [YYYY-MM-DD ...]

# 1b. recover hOCR signal (no OCR, no LLM, idempotent)
python3 -m transcribe.scaled.hocr_parse backfill [--date YYYY-MM-DD] [--force]
python3 -m transcribe.scaled.hocr_parse show YYYY-MM-DD --page N

# 2. columns
python3 -m transcribe.scaled.detect_columns run [--date YYYY-MM-DD]
python3 -m transcribe.scaled.detect_columns show YYYY-MM-DD --page N
python3 -m transcribe.scaled.detect_columns report      # escalation rate

# verify by LOOKING (mandatory before trusting any score)
python3 -m transcribe.scaled.render_overlay YYYY-MM-DD [--page N]
# -> preview/scaled/<date>/pN_columns.jpg
```

## 8. Schema v15 / v16 (all additive)

- `pages.hocr_parsed_at`, `scan_res_x`, `scan_res_y`
- `page_ocr_blocks.block_class`, `x_size_median`
- `page_hocr_lines` — line level, with `line_class` and `x_size`
- `page_hocr_regions` — separators and photos, with derived `orientation`
- `page_columns` — one row per column per `method`, so each raw signal
  stays inspectable next to the combined answer
- **v16** `page_bands` — one row per band: extent, column count, column
  edges, regularity, line count. The layout unit for 1980+

## Update history

- **2026-08-15 (later)** — Band-first stage 2 built
  (`detect_bands.py`, schema v16 `page_bands`): escalation 97.8% ->
  31.5%. Visual check shows bands are still too coarse (a 62%-tall band
  scored 0.917) — recursive splitting is the next step, not built.
  Viewer selector regrouped by pipeline stage; per-stage IIIF manifests
  added. TIFY hidden (does not render overlays; code retained).
  hocr_font_info tested: adds only `x_fsize`, no bold/italic.
- **2026-08-15** — Created. hOCR full-fidelity parse built and
  backfilled (90/93 pages). Column detector built; **97.8% escalation on
  1980+, a negative result explained by modular layout**. Confidence
  metric found flattering itself via visual check and fixed with a
  multiplicative recall term. Viewer research completed (TIFY primary,
  Theseus/Mirador via `iiif-content`).
