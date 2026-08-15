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

### 5i. Stage 1c — THE PAGE CONTENT AREA (built 2026-08-15)

`transcribe/scaled/detect_content_area.py`. Runs **before** columns and
owns the page's content rectangle; `pages.content_left_pct` /
`content_right_pct` / `content_top_pct` / `content_bottom_pct`.

**Why it exists as its own step.** The column fitter was deriving its own
left/right bounds from the extremes of the block-edge peak distribution,
so one scan artefact at the sheet edge anchored the whole lattice to the
physical page edge instead of the type. Measured over 90 pages:

- `text_left` was **0.00%** on many pages — up to **7.2%** left of where
  type actually starts (1980-04-06 p4: 0.00% vs a real content left of
  7.08%). **Every column on those pages was displaced.**
- error >1.5% on **31%** of pages at the left edge, **53%** at the right.

After: column 0's left edge is within 1% of the content left on **100%**
of pages (median 0.30%), none beyond 3%.

**Lines, not blocks.** A block bbox can be inflated by an artefact swept
into it; a line with ≥2 recognised words is a real run of type.

**Left/right and top/bottom are found DIFFERENTLY, and this is the
point.** Top and bottom are EXTREMES — the first and last line of type,
with nothing above or below. Left and right are **clusters**: body text
is flush left in every column, so hundreds of lines start at the same x,
and the content's left edge is the leftmost position a meaningful number
of lines actually start at. Taking the minimum is precisely what produced
the 0.00% failures; a hanging indent or one overhanging headline must not
be able to move the margin.

Stage 3 no longer computes its own top/bottom — it delegates here, so two
stages cannot disagree about where the page's content is.

### 5f. Stage 2 — COLUMNS (one pass; pass 2 archived)

Following `instructions/typesetting_practice.md`: the page was set on a
fixed grid, so **fit a few numbers, don't discover boundaries.**
`transcribe/scaled/detect_grid.py`.

Per PAGE, not per issue — the photography varies too much between pages
(skew, scale, crop), and one page carries plenty of blocks.

**What is measured: BLOCKS.** hOCR *lines* contribute no edges. They are
referred to for exactly one purpose — deriving the minimum height a block
must exceed. Also fed in: `ocr_separator` **vertical rules**, with their
edges mapped CROSSED OVER, because a rule sits *inside* the gutter:

    rule.L  ->  the preceding column's RIGHT edge
    rule.R  ->  the following column's LEFT edge

Mapping left-to-left would offset every column by a rule width. Page-edge
rules (<2% or >97%) are excluded as scan artefacts — including them
dragged column 0's left edge to 0.60%.

**Truncation**, applied to every item:
- taller than `MIN_ITEM_HEIGHT_LINES` (1.5) text lines — a one-line
  fragment says nothing about a column edge;
- shorter than `MAX_ITEM_HEIGHT_FRAC` (0.90) of the page — full-height
  boxes are scan artefacts (photo shadows, page-edge blobs);
- and the shortest decile by height is dropped before an edge is chosen
  (`HEIGHT_PCTL_FLOOR`).

**Y is item HEIGHT, not item count.** A tall block running down a column
is strong evidence of its edge; a pile of fragments at the same x is not.
Count-weighting remains available as `peak_counts()`.

#### Columns (1) — rigid lattice
Grid-search pitch and offset; keep the lattice explaining most peak
weight, chance-corrected. Establishes pitch, offset, column width and
column count. A rigid lattice cannot follow the scan's own scale drift.

#### Refinement — subsume stray blocks
A block wholly inside another, narrower than 50% of that parent, and no
more than 3 hOCR lines tall is a fragment Tesseract split out (a price, a
drop cap, a stray ad line). Left in place it contributes edges at
arbitrary x. Corpus: 350 subsumed, mean 3.9/page.

#### Columns (2) — leaned to extremes
Re-runs the same `analyse()` on cleaned blocks, then sets each edge by
**leaning to the extreme**, not by averaging a cluster:
- left edge  = LEFTMOST tall block start near the prediction
- right edge = RIGHTMOST tall block end near the prediction

Averaging was the earlier mistake and it made pass 2 *weaker* than pass 1:
it pulls an edge toward wherever most items happen to stop, which is
inside the column.

**Order matters.** All left edges are chosen first; each right edge is
then bounded by the ACTUAL next left edge less a minimum gutter. Bounding
against the *predicted* next-left was not enough — the next column leans
left to that same prediction and the two meet, collapsing the gutter to
zero.

**Result:** a consistent gutter appears. 1980-04-06 p11 gutters
0.87 0.72 0.80 0.74 1.37 0.77 1.85, previously erratic or zero; fit
0.55 → 0.79. Corpus: 603 gutters, **median 0.78% (~1 pica)**, only 4%
degenerate, median within-page stdev 0.44%. Column counts consolidated on
8 (50 pages) and 6 (26).

**Margins are single lines, not gutters** — a gutter exists only BETWEEN
columns, never before the first or after the last.

#### Honest limits, visible on the page render
The aggregate numbers look good but the page overlay for
1980-04-06 p11 shows the fitted pitch (11.55%) is close but not exact,
and the error accumulates left→right: the lines at ~61%, ~73% and ~84%
fall INSIDE text blocks rather than in gutters. A consistent gutter on a
slightly wrong pitch is exactly the failure that flatters aggregate
statistics. Likely causes: the right half of that page is display
advertising with no printed rules, so the lattice is extrapolating there.
Worth trying: weighting the fit toward regions where rules exist, or
allowing a slight pitch stretch across the page.

**Validation caveat:** `ocr_separator` rules were previously used as an
INDEPENDENT ground truth for grading the fit. They are now inputs, so
that comparison is circular — any future validation must use a signal
these do not contribute to.

#### Evidence is weighted by kind (2026-08-15)

Not every item is equally trustworthy about where a column edge lies:

| item | weight | edge mapping |
|---|---|---|
| text block (`ocr_carea`) | full | straight through |
| vertical rule (`ocr_separator`) | half | **crossed over** — sits in the gutter |
| photo region (`ocr_photo`) | half | straight through — sits ON the columns |

Rules and photos are real evidence but looser: Tesseract reports photo
borders and box edges as separators, and its photo boxes are only
approximately placed. Both get the same minimum-height and full-height
truncation as blocks.

#### Lines enter the geometry in exactly one place

The **last** column's right edge may not sit left of the rightmost hOCR
line in the rightmost block. A block bbox can under-report its extent;
its lines cannot. Fires on 57/90 pages.

#### Column-count sense check

A page cannot have each of its columns divided in two. Three guards:

- **`MIN_PITCH_PCT` 8.0** — a column must be wide enough to set body text
  in. Halved fits sit at 6.25–7.20% pitch and every sound fit at 11.30%+,
  so the threshold sits in an empty gap, not against a cluster edge.
  ~7 picas, well under the 11–13 of `typesetting_practice.md`.
- **Pass 2 may correct pass 1's count by one, never double it.**
  Subsuming stray blocks legitimately sharpens the reading; a jump to 2×
  means pass 2 found a sub-division (an ad's price columns, a table's
  cells). Fires on 1 page in 90 (1980-04-06 p12, 4 → 8).
- **`low_evidence`** below 60 text lines. 1980-04-06 p7 is a full-page
  picture spread with 25 lines, all captions — it cannot evidence a grid
  and a fit there is a guess dressed as a measurement. 4 pages in 90.

#### `col_width` comes from the slot's END ZONE only

Accepting the heaviest right peak *anywhere* inside a slot let a wide
item that stopped early set the width, inflating gutters to 8–13% on
pages whose real gutter is ~1 pica. This was the actual cause of the
regression the weighting and photo changes appeared to cause — found by
isolating the two changes, not by guessing. `COL_END_ZONE_FRAC = 0.70`.

Result over 90 pages: gutters median **0.53%**, 3% above 2.5%,
within-page stdev **0.42%**.

#### Negative results worth keeping

Four candidate column-count discriminators were measured and **none
separates cleanly** — p3, p7 and p12 are correctly fitted yet score like
the halved pages, because multi-column headlines and ads legitimately
straddle gutters and inflate line widths:

- median line width ÷ column width
- modal line width ÷ column width
- fraction of lines matching exactly one column
- fraction of lines straddling a gutter

Do not reach for these again expecting them to work. The measure floor
(`MIN_PITCH_PCT`) is what actually separates.

#### Pass 2 archived 2026-08-15 — pass 1 is the answer

Column detection is now **one pass**: two global parameters (pitch,
offset) fitted across the page, one column width derived, columns read
straight off the lattice. The gutter is therefore **constant down the
page by construction**, which is what a gutter physically is.

The former "columns (2)" — subsume stray blocks, refit, then lean each
column edge independently to the outermost nearby edge — is preserved
runnable at `transcribe/scaled/archive/refine_columns.py`. It was set
aside on the user's reading that pass 1 wins in almost every case, which
the measurement supports:

| | within-page gutter variation, 89 pages |
|---|---|
| pass 1 | **0.00%** (constant by construction) |
| pass 2 | median 0.42%, mean 0.48%, max 1.24% |
| pass 2 | varies >0.30% within the page on **54/89 (61%)** |

Pass 1 fits 2 parameters; pass 2 fits 2n, one per edge, each leaning
toward whatever sits furthest out nearby — including display-ad interiors
(see below). **We may return to it:** the scan scale drift it was built
to absorb (~1.3% by the right-hand edge) is still unsolved. Any retry
must stay parametric — one global scale/skew term, gutter held constant —
not per-edge.

Surviving from the pass-2 work: the last column's right edge may not sit
left of the rightmost hOCR line in the rightmost block. That widens only
the last column, leaving every interior gutter untouched.

#### Display ads carry their OWN grid — measured, not yet solved

A display ad's **outer** rectangle is quantised to the page grid (it was
sold by the column inch). Its **interior** is the advertiser's own
design, at whatever measure they chose, and says nothing about the page.
This is what halved 1980-04-06 p2: the grocery ad's internal item/price
sub-columns.

Measured on the 28 pages of 1980-04-06 + 1997-07-16, using the
production route's LLM-labelled `items.item_type='display_ad'` boxes as
an oracle (105 ads). **This oracle is also the non-circular validation
signal §5f was missing — `ocr_separator` contributes nothing to it.**

- **30% of all text blocks sit inside a display ad.** Per page it reaches
  **100%** (1980-04-06 p14 is a single full-page ad).
- Type size does NOT cleanly separate ad interiors: `x_size_median` is 44
  inside vs 36 outside, distributions heavily overlapping. Too weak to
  classify a block on its own — **do not build on it alone.**
- Replacing each ad's interior blocks with its outer rectangle was
  **inconclusive, not a win**: median gutter 0.49% → 0.44%, one page
  clearly fixed (p8, 6 → 8 columns, gutter 1.10% → 0.00%) against four
  clearly worse.

**The test was confounded** — do not read it as a refutation:
  1. Ad rectangles were given full weight and their full height, so a few
     tall rectangles dominate a height-weighted fit. They should carry
     weak weight, like a rule or photo.
  2. Pages that are mostly ad lose nearly all evidence (p13 58% removed,
     1997 p12 55%, p14 100%).

Two things follow. **A full-page-ad page has no page grid to find** — the
ad *is* the page, and 1980-04-06 p14 / 1997-07-16 p14 should be flagged
like `low_evidence` rather than fitted. And doing this without an LLM
needs a classical display-ad detector, which does not exist in `scaled`
yet; `x_size` dispersion alone will not carry it.

**Still wrong:** 1980-04-06 p2 no longer halves (14 slots → 7) but the
page's true grid is 8 at ~10.5%, visible in the obituary text and the
grocery ad's five product columns. Over-columned has become
under-columned there; the page's evidence is dominated by one full-page
ad. Verified by rendering, not by the fit number.

### 5j. Stage 2b — BOXED ZONES (built 2026-08-15)

`transcribe/scaled/detect_boxes.py`, `page_boxes` (schema v18). A boxed
area is a deliberate page landmark — mostly display ads, but notices,
tenders, standing panels and section headers use the same device.

**Corner-matching does NOT work — measured, don't retry it.** The obvious
method is `ocr_separator` rules meeting at their corners. Of 4,452
horizontal-rule endpoints, only **22%** sit within 0.5% of a vertical
rule's end (26% within 1%, 33% within 3%; **median distance 9.0%**).
Tesseract does not reliably report all four sides.

**What works: a top and bottom rule sharing an x-extent.** That pair is
the box's signature and survives the verticals being absent.

**Containment matching was tried and REVERTED — do not reintroduce it.**
The theory was that Tesseract merges collinear rules from adjacent boxes,
so a wide rule should be allowed to pair with a narrow one. It raised
recall on paper but let almost any rule pair with almost any other: boxes
went from 6.8 to **20.8 per page**, and the render of 1980-04-06 p6 was a
thicket of overlapping rectangles cutting across body text. The user's
verdict — *"The box detection is not working out … overcomplication of
your detection methods more than anything else"* — was correct.

**The lesson, and it generalises.** Rendering the raw `ocr_separator`
regions over p5 and p6 shows that **Tesseract's rules already trace these
boxes**: the Pakenham Seniors panel, the Beach Party ad, the Sidewalk
Sale, HI MOM/RELAX and Heritage IDA are each outlined by their own four
rules. The job was to READ that, not to infer boxes the rules do not
support. Current rule: strict extent match on the horizontal pair, and a
vertical side must be present between them (`n_sides >= 3`) — a box has
sides. Result: **p6 gives 7 boxes and all 7 are correct**; corpus median
2/page.

**`n_sides` is recorded, never filtered on at write time.** 4-sided boxes
are near-perfect; 2-sided ones are where only a top and bottom were
reported. The consumer chooses.

**On judging this stage: the obvious metric is wrong.** Scored against
LLM-labelled `display_ad` boxes, `n_sides>=4` gives 27% recall and 20%
precision. But **19 of the 20 four-sided boxes on 1980-04-06 p11 are
correct by eye** — the metric counts PAKENHAM UNION CEMETERY, COUNTY OF
LANARK TENDERS and CLASSIFIED ADS as false positives because they are
notices, not display ads. Recall is likewise capped because many display
ads simply are not ruled boxes. Use the ad overlap as a weak sanity
signal only; judge this stage by rendering it.

### 5k. Missing box rules — Tesseract tuning ruled out (2026-08-15)

Some obvious boxes have no complete rule set in Tesseract's output (the
CBO 920 ad on 1980-04-06 p5, the I.D.A. ad on p6). Three routes were
proposed: escalate to an LLM, tune Tesseract, or both.

**Tuning Tesseract does NOT work.** Re-ran p5 and p6 across five configs
— Sauvola / Otsu / Leptonica-Otsu thresholding, psm 3 / psm 1, table
detection on / off. **Separator counts were identical in every case**
(12 on p5, 45 on p6).

This is not a broken experiment; the control proves the variants applied.
p6's hOCR differs between them — base 374,573 bytes / 2,270 words, otsu
376,395 / 2,265, notab 382,545 / 2,267, three distinct hashes. **The OCR
text changes while the layout analysis does not.** `--print-parameters`
confirms Tesseract exposes only debug/visualisation switches for
`textord`; there is no rule-sensitivity knob. Don't spend more time here.

**Pixel-level rule detection finds what Tesseract misses.** A rule is
THIN as well as long, so filtering for dark pixels with no dark neighbour
a few rows above *and* below, then taking long runs, yields in p6's
I.D.A. region: `y 54.4%, x 53.3–90.9%` and `y 95.2%, x 52.7–90.7%` — a
matching top/bottom pair bounding the ad `detect_boxes` could not build.

**Caveat, stated plainly:** at these parameters the crude version finds
FEWER rules overall than Tesseract (4 vs 12 horizontal on p5, 23 vs 38 on
p6). It is a promising prototype, not a tuned detector, and has not been
compared page by page. Reproduce with
`transcribe/scaled/experiments/rule_detection_sources.py`.

**Conclusion: the cheap classical route is not exhausted, so LLM
escalation is not yet justified for this.** If resumed: build the pixel
rule detector properly, measure it against Tesseract's separators
corpus-wide, and escalate only boxes neither source supports.

### 5h. Stage 3 — HORIZONTAL alignments (built 2026-08-15)

`transcribe/scaled/detect_hlines.py`, rendered by `render_hlines.py`,
stored in `page_hlines` (schema v17) plus `pages.content_top_pct` /
`content_bottom_pct`.

**What killed the previous attempt, and what changed.** §5d cut the page
into strips bounded by a page-wide rule. Measured across the corpus there
are **2,226 horizontal `ocr_separator` rules but only 20 span 8+
columns** — 1,240 span one column, 581 two, 196 three. The band approach
therefore discarded ~99% of the available evidence, which is exactly why
it could only produce coarse strips. On a post-1980 mosaic page an
alignment is **local**: columns 3–5 break while 1–2 run on.

So every alignment here carries a **column span** and is never required
to cross the page. Visible on 1997-07-16 p4: the alignments for "Nuclear
waste", "Don't check your brain at the door" and "LETTERS" span columns
1–5 and stop short of column 0, where the "CFL back in Ottawa?" editorial
runs the full page height.

**The unit of evidence: independent columns agreeing.** Strength is
`n_columns` — how many distinct columns contributed an edge at that y —
**never the raw edge count**. A column Tesseract fragmented into twenty
blocks must not out-vote two columns genuinely breaking at the same
height. This is an evidence count, not a self-assessment; nothing here
scores its own trustworthiness (§5c, §5g).

**Not a lattice fit.** Vertical rhythm is not quantised the way column
pitch is — ads are sold by the column *inch*, so heights vary
continuously. There is no vertical pitch to fit and pretending otherwise
would repeat, on the other axis, the error `typesetting_practice.md`
warns about.

Evidence: horizontal rules (1.0), `ocr_header` line tops (1.0), photo
top/bottom (0.75). **Block edges were used and have been REMOVED.** They
took the page from ~17 alignments to ~44, and the extra ~27 were inferred
boundaries with no printed counterpart; rendered, they obscured the real
structure rather than describing it. What remains is what the page
actually prints. If a boundary has no rule, photo edge or heading, this
stage does not claim it. Corpus median is now **20/page**, was 44.

**Validation against a non-circular signal.** Horizontal rules do not
feed the stage-2 column fit, so they independently corroborate it: rule
endpoints land within 1% of a fitted column edge **52% of the time vs
21% for an evenly-spaced control** — 2.5× better than chance.

Recall of LLM-labelled `display_ad` edges (ads ≥2 columns wide, so the
edge *can* be a multi-column alignment):

| min agreeing columns | recall | median offset | random control | lift |
|---|---|---|---|---|
| 2 | **72%** | 0.30% | 44% | 1.65× |
| 3 | 58% | 0.45% | 38% | 1.53× |
| 4 | 48% | 0.58% | 21% | 2.25× |
| 5 | 34% | 0.96% | 14% | 2.40× |

**State this honestly: the lift over control is real but modest.** The
control rate is high because 45 alignments/page is dense. The tight
median offset (0.30%, about one line height) is the more convincing
number. Corpus: 4,082 alignments over 90 pages, median 47/page.

**No threshold is baked in.** All alignments are stored with their
`n_columns`; filtering is the caller's choice (`--min-cols` on the
renderer, tiered layers in the manifest). Picking a cutoff at write time
would destroy signal a later stage needs.

**Content extent.** `content_top_pct` / `content_bottom_pct` come from
text LINES, not blocks, ignoring the outer 1.5% margins and requiring
≥2 words per line. That last rule was forced by a real case: 1997-07-16
p4 reported a content top of 0.46% from a single one-word line reading
`"a` at the sheet edge, where the real top is 2.42% ("OPINION"). A
content line is an EXTREME, so one artefact moves it — a median would
have absorbed it. Corpus medians after the fix: top 4.35%, bottom 96.99%.

### 5d. ARCHIVED — band-first segmentation

Because full-height columns are the wrong model for 1980+ (5b), stage 2
was re-cut around **bands**: a horizontal strip bounded by a wide
`ocr_separator` rule or by a y-gap no text line crosses. Columns are then
found *within* a band. `transcribe/scaled/archive/detect_bands.py` (archived), schema v16
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
2 · Columns     columns (1) | columns (2) | columns (1) + (2)
3 · Items       (disabled — not built)
4 · Refined     (disabled — not built)
```

Columns (1) and (2) are separate selectable entries with their own
manifests (`manifest_grid1.json`, `manifest_grid2.json`), plus a combined
overlay (`manifest_grid.json`) for judging the refinement directly.
Default view is columns (2). Names are deliberately numbered rather than
descriptive — more steps are expected.

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
python3 -m transcribe.scaled.detect_grid run [--date YYYY-MM-DD]
python3 -m transcribe.scaled.detect_grid show YYYY-MM-DD --page N
python3 -m transcribe.scaled.detect_grid report

# the signal the fit rests on: block edges by height, before/after refine
python3 -m transcribe.scaled.plot_edges YYYY-MM-DD --page N

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

- **2026-08-15 (latest)** — Stage 2 rebuilt as COLUMNS (1)+(2) on the
  typesetting model. Blocks only (lines used solely for a minimum
  height), height-weighted, `ocr_separator` vertical rules included with
  edges crossed over. Averaging replaced by leaning to extremes; left
  edges chosen before right. Consistent gutter appears: corpus median
  0.78% (~1 pica), 4% degenerate. Band-first and confidence scoring
  archived. Page render still shows pitch drift on the ad-heavy right
  half — recorded, not tuned away.
- **2026-08-15 (earlier)** — Band-first stage 2 built
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
- **2026-08-15** — §5f: evidence weighted by kind (blocks full, rules and
  photo regions half); lines constrain the last column's right edge;
  column-count sense check (measure floor, pass-2 doubling guard,
  low-evidence flag); `col_width` restricted to the slot end zone. Four
  column-count discriminators measured and recorded as negative results.
  Viewer: combined "columns (1) + (2)" option removed.
- **2026-08-15** — Pass 2 (per-edge refinement) archived to
  `transcribe/scaled/archive/refine_columns.py`; detection is one pass and
  the gutter is constant by construction. Viewer and manifests reduced to a
  single `columns` layer. Display-ad grid contamination measured (30% of
  blocks) and recorded as open.
- **2026-08-15** — Stage 3 built: `detect_hlines.py` + `render_hlines.py`,
  schema v17 (`page_hlines`, `pages.content_top_pct`/`content_bottom_pct`),
  IIIF layer `manifest_hlines.json` tiered by agreeing-column count. See
  §5h, including the measured reason §5d's band approach failed (it
  discarded ~99% of horizontal rules by requiring page-wide extent).
- **2026-08-15** — Stage 1c added (`detect_content_area.py`): the content
  rectangle is now established before columns and is authoritative for the
  fit. Fixes a measured bug where `text_left` was 0.00% on many pages,
  displacing every column by up to 7.2%. See §5i.
- **2026-08-15** — Stage 2b added (`detect_boxes.py`, schema v18
  `page_boxes`): ruled rectangles from top/bottom rule pairs, with
  containment matching for Tesseract's merged collinear rules. Corner
  matching measured and rejected (22% of endpoints coincide). See §5j.
- **2026-08-15** — Measured that Tesseract config tuning cannot recover
  missing box rules (identical separator output across 5 configs, with a
  control proving the variants applied); pixel-level rule detection can.
  See §5k and `transcribe/scaled/experiments/rule_detection_sources.py`.
