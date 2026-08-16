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

### 5n. Stage 2b CONSOLIDATED — `detect_zones` (2026-08-16)

Boxed zones now run from the grid, in one path:

    Tesseract separators   RAW
      -> separator_grid     quantised onto SQUARE cells; corners resolved
      -> ad_rectangles      rectangles, from corners alone
      -> content            what each contains, and what that says

**Correction, 2026-08-16.** An earlier version of this section said the
separators are cleaned first by `rules.py` (conjoined regions dropped,
fragments rejoined). **They are not.** `separator_grid.build()` defaults to
`clean=False` and the live path reads raw `ocr_separator` rows; the
cleaning runs only under `--clean`. The claim was written without checking
that any live caller reached it — the review that caught this is the same
class of error as everything else in §5z.

And it should stay off, on measurement across 90 pages:

| | zones |
|---|---|
| raw separators (live) | **273** |
| cleaned | 256 — worse on 15 pages, better on 10 |

On p13 cleaning drops 8 → 7, losing the Sidewalk Sale, the very box
`_merge_fragments` was written to rescue. The reason it reverses: that
cleaning was built for the rule-PAIRING detector, where a fragmented rule
broke the pair and a conjoined region invented one. The corner derivation
wants rule **ends** — they become corners once near-misses resolve to
their axis crossing — and merging fragments removes ends.

`rules.py` is kept rather than archived because `--clean` is a useful
comparison, and because the observation (Tesseract both merges and
splits rules) is durable even though the remedy is not.

**Carried forward** from the retired pairing detector: the content-area
filter, and the page-edge exclusion of scan artefacts (which lives in
`separator_grid`, not in `rules.py` — `rules.EDGE_MARGIN_PCT` is dead).

**Left behind with it:** the six geometric thresholds, and `n_sides` with
its three-sided closure — which inferred a foot where none was printed.
The corner map already carries that evidence.

**Newly implemented, never wired in before: the CONTENT check.** Each zone
records its blocks, lines, photos and column span, and carries advisory
flags — `empty`, `pictorial`, `duplicate`, `encloses`. Geometry decides;
content is evidence. Nothing is dropped on a content test: 28.8% of boxes
hold no text block and many are pictorial ads, so an emptiness rule
counting only text would delete them. Corpus: 53 `empty`, 14 `pictorial`,
no duplicates and no enclosures.

**Withdrawn:** an earlier version called those two zeroes "a live check
that the corner predicate is working". They are not. `encloses` only fires
when the inner zones' blocks EXACTLY cover the outer's, so an outer box
carrying any text of its own can never trip it — and 13 geometric nestings
do exist in the corpus. The flag proves nothing about the predicate. A
real check would test the geometry directly.

Schema v20 adds `page_zones`. 273 zones over 90 pages, 3.0/page.

Archived: `archive/detect_boxes_pairing.py` (the pairing detector),
`archive/corner_quadrilaterals.py` (the quadrilateral generator),
`archive/percent_box_filters.py` (its two percent-unit filters).

### 5m. Rectangles from CORNERS ALONE — the derivation that stuck

`transcribe/scaled/experiments/ad_rectangles.py`. Standalone: corner
points in, rectangles out. No database, no separators, no Tesseract. Once
the corners are established the ruling has done its job.

**One predicate: a rectangle is an item when no other corner interrupts
its sides.** A bridge spanning two stacked ads has its left and right
sides running straight THROUGH the divider's corners. A gutter sliver has
the corners of everything above it sitting on its top edge. Unions of any
depth, same argument, no special case.

That single test replaced SIX tuned thresholds — aspect ratio, thin
dimension, gap tolerance, twin collapse, double-rule merge, gutter drop.

**It works in CELLS**, which are square by construction. Page percent is
two units (x of width, y of height) and mixing them lost a real box: a
0.9% tolerance meant 1.80 cells across but 2.53 down, and the vertical
figure swallowed a genuine 2-cell gap, dropping the Sidewalk Sale. See
§5z.7.

**Order-independent by construction.** Corners cluster by sorting and
splitting on gaps; candidates are every pair of x-lines against every pair
of y-lines; atomicity is a property of the whole corner set. The earlier
detectors' worst bugs were all order artefacts and this cannot have them.

**Both halves of the predicate ask MEMBERSHIP, not distance** (fixed
2026-08-16, review findings 5 and 6). The clusters are built by splitting
on gaps, so a cluster can be WIDER than the tolerance that built it —
measured, 18 such clusters corpus-wide, worst 3.0 cells across 14 corners.
Any test that then compares a corner to its own line's CENTROID is
therefore incoherent, and both tests originally did:

  * `has_corner` — 25 corners on 9 pages sat further than `LINE_TOL` from
    the centroid of the line they had themselves defined, so they could
    not certify it.
  * `_interrupted` — worse, because it fails in the other direction. A
    corner belonging to the right-hand line sat 1.04 cells from that
    line's centroid: too far to count as part of the side, yet inside
    `x1 - LINE_TOL`, so it counted as an interior point of the TOP edge
    and vetoed the rectangle. On 1980-04-06 p10 the Carleton Refrigeration
    / TRUCKING / McKay stack survived or died on whether that centroid
    landed at 122.83 or 123.04 — a fifth of a cell.

Both now index into the cluster map: "same line" is identity and
"strictly between" is an integer comparison, so `_interrupted` carries no
tolerance at all. **A corner of a side is not an interruption of the edge
that ends on it.** Corpus zones 266 -> 273.

**Nearest neighbour wins the corner scan, and only the nearest.** An end
one cell short of another rule resolves to a crossing, but if it is near
several rules only one is the one it was reaching for. `separator_grid`
used to take whichever came first in `NEIGHBOURS` order — a list starting
at `(-1,-1)`, so a top-left DIAGONAL beat an orthogonal neighbour that is
plainly closer. Distance decides instead (orthogonal 1.0 before diagonal
1.41), ties kept together because a tie is genuinely ambiguous.

That was reported as an order-dependence bug and it was, but the corner
map it produced was mostly a symptom: once `_interrupted` was fixed, p10
gives 10 zones either way. The remaining difference is 4 pages, and
rendering them the nearest-neighbour reading is better or equal
everywhere — p13 gains the Township of West Carleton SNOW REMOVAL and
ANNUITIES boxes, p11 gains one, nothing is lost.

Column lines and photo containment SCORE the survivors, never reject one.

1980-04-06 p13: 8 rectangles, the complete set, rendered and checked.

**Two generations are archived**, both superseded by this:
`archive/corner_quadrilaterals.py` (enumerating corner quadrilaterals —
asks "is this a valid rectangle?", to which a union answers yes) and
`archive/percent_box_filters.py` (the two filters it needed, which
conflated percent units). The `cornerboxes` IIIF layer that showed the
quadrilateral output has been removed; the grid chart already draws the
derived boxes.

### 5j. Stage 2b — BOXED ZONES (built 2026-08-15)

`transcribe/scaled/detect_boxes.py`, `page_boxes` (schema v18). A boxed
area is a deliberate page landmark — mostly display ads, but notices,
tenders, standing panels and section headers use the same device.

**Four sides, with the print's own quirks allowed for.** Three
properties of the actual print, each confirmed in the data, and each one
of which breaks a naive reading:

1. **Rounded corners mean the sides never meet.** On p8 the Fastball
   standings box has horizontals spanning x 50.82–72.00 while its
   verticals sit at 50.35 and 72.40 — the rules stop ~0.5% SHORT of the
   join. CENTENNIAL DOLLARS, ornately bordered, is inset 2.5–3.9%. **This
   is why corner-matching scored only 22%** when measured (4,452 endpoints;
   26% within 1%, median distance 9.0%): the corners genuinely do not
   touch. A side must BRIDGE the box within `INSET_PCT`, not meet a corner.

2. **Drop shadows make opposite sides uneven.** p8's Sidewalk Sale is
   28px on top against 48px at the bottom; another box is
   [32, 26, 19, 23]. An earlier version REQUIRED matching side weights and
   found **2 boxes on the whole page**. Thickness is recorded (`side_px`)
   and never filtered on.

3. **Stacked boxes share their verticals.** POLICE CONSTABLE and
   Congratulations sit inside one pair of verticals running y 39–73. So
   every bridging horizontal is collected and a box emitted between each
   CONSECUTIVE pair, plus one for the whole enclosure. That yields
   Fraser's Meat Market as one box AND its price rows, the Sidewalk Sale
   grid AND its cells.

**The verticals define the sides, not the horizontals.** Extending a box
to the horizontals' ends stretched POLICE CONSTABLE and Congratulations a
whole column left into the body text, because a bridging rule frequently
belongs to a neighbouring box too and overshoots. Page-edge verticals are
excluded as scan artefacts, as in `detect_grid`.

**Result on p8:** Fastball standings, Fraser's, POLICE CONSTABLE,
Congratulations and the Sidewalk Sale grid all correct, at container and
cell level.

**A bug worth recording, because the symptom looked like missing data.**
CENTENNIAL DOLLARS on p8 was reported as "no bottom border in Tesseract's
output" — that claim was WRONG, and checking it rather than repeating it
found a real defect. All four sides were present:

    top     H  x 15.52-49.13  y 73.63-73.94  (18px)
    left    V  x 15.47-15.88  y 73.70-95.46  (17px)
    right   V  x 48.80-49.47  y 39.01-95.47  (28px)
    bottom  H  x 15.47-94.91  y 94.74-95.56  (48px)

The pair loop takes `V[i], V[j]` with `j > i` and requires
`vr.x - vl.x >= MIN_WIDTH_PCT`. SQLite returns the rows unordered, so
whenever the LEFT-hand rule happened to be listed later, the difference
came out negative and the pair was skipped **in silence**. Here the
x 49.13 rule is listed before the x 15.68 one. **`V` is now sorted by x,
and that sort is load-bearing.** Fixing it took p8 from 32 boxes to 70
and recovered CENTENNIAL DOLLARS and its inner ticket panel.

Smithson Motor Sales does remain genuinely incomplete in Tesseract's
output. See §5k — pixel-level rule detection is the route there, and
Tesseract config tuning is ruled out.

**Tesseract both MERGES and SPLITS rules, and each needs undoing.**

- *Conjoined* — it reports the individual rules AND a single region
  covering them. On p13 the left edge appears three times: the real upper
  rule (x 4.29–4.69, y 25.82–47.79, 17px), the real lower rule
  (x 4.57–5.27, y 49.51–95.80, 29px), and both merged (x 3.76–4.96,
  y 25.82–95.88, 50px ≈ the sum). The merged region spans the gap between
  the real rules and manufactures boxes across a boundary that isn't
  there. A region is conjoined when ≥2 others of the same orientation lie
  within its RUN and overlap it on the thickness axis. **A bbox
  containment test does NOT find these** — the merged region is typically
  slightly wider than its own parts.
- *Fragmented* — the opposite, and it was the actual cause of p13's low
  detection rate. The Sidewalk Sale box occupies the whole lower half of
  the page and has left, right and top rules, but its foot arrives in
  pieces (x 4.43–75.05 and x 80.97–95.24 at y ≈ 95.5). Neither bridges
  both verticals, so **the largest box on the page was missed entirely**.
  Collinear pieces within `FRAGMENT_POS_PCT` across the rule and
  `FRAGMENT_GAP_PCT` along it are merged back into one.

**Boxes may NEST or be disjoint — never straddle.** Fraser's price rows
were being drawn from the column gutter at x 49.13 while Fraser's own box
starts at 61.89, so every row crossed both its container and the gutter.
Larger boxes are accepted first, so anything crossing an accepted box is
the bad one and is dropped. p8: 70 boxes → 48.

**A three-sided box can be closed, but only with a barrier, and it is
marked for review.** The PLEXIGLASS ad has a head and two verticals but
no printed foot, with the top of the next box immediately below. Two
conditions must both hold: the verticals are a genuine PAIR (both ends
agree within `PAIR_MATCH_PCT`, i.e. drawn as the sides of one box), and a
BARRIER sits below within `BARRIER_GAP_PCT` — another rule or an
established box. Without a barrier there is nothing to say where the box
ends and the foot would be invention. Such a box gets `n_sides = 3` and
**`needs_review = 1`**, so a later LLM pass confirms or rejects it rather
than inheriting a guess dressed as a measurement. 17 across the corpus.

Note PLEXIGLASS itself does NOT come through this path: its left "side"
is a long column rule running y 29.70–95.40, not a matched pair with the
right side at y 29.67–45.62. It is closed by the main pass using the next
box's head. Worth knowing before tuning `PAIR_MATCH_PCT`.

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

## 5z. PROCESS FAILURES in this experiment — read before changing a detector

Six failures, all in one session, all avoidable. They are recorded with
what they cost because each one is a pattern, not a one-off slip.

### 1. Shipping on a metric without rendering

Relaxed the corner requirement from four marked corners to three, on the
reasoning that three fix an axis-aligned rectangle and only 43% of known
boxes have four marked. Every number improved: boxes 547 → 812 against
`detect_boxes`' 815, agreement 80%, under-finding pages 31 → 18 of 90.
**Reported it as a win. Never rendered the page.** The render was a
thicket of slightly-offset near-duplicate rectangles around every ad,
because a corner a cell or two away can substitute for the missing one
and each substitution makes another valid rectangle. Reverted.

*A count agreeing with another detector is not evidence.* This project
has an explicit rule about rendering before believing a number, and it
was ignored while quoting statistics.

### 2. Changing the test to fit the fix

Wanted a single whited-out cell to sever a 52-cell edge, so replaced the
edge-support test with a continuity test. That silently invalidated real
boxes and lost the **largest box on the page** (the full-width Sidewalk
Sale), which went unnoticed for two further changes. Designing the
measurement around the desired answer. The break needed to be strong
enough to matter, or the idea needed rejecting — not the test loosened
around it.

### 3. "Fixing" what the printer actually did

Built machinery to split a long vertical into per-box segments, believing
Tesseract had merged them. A column rule is ONE continuous strip with ads
butting against it — the reading was correct and the "fix" invented gaps
that were never in the ink. See `typesetting_practice.md`, "What the
RULES tell you".

### 4. Accretive patching instead of questioning the approach

Enumerating corner quadruples generates unions of stacked boxes, gutter
slivers and double-rule pairs. Each got its own filter — twin-collapse,
gutter-drop, double-rule merge, six tuned thresholds in all. Not once was
the generator questioned. Face extraction produces none of those
artefacts and needs none of those filters (measured: p13, 8 regions, zero
filtering). *A growing pile of post-hoc filters is evidence the
derivation is wrong, not evidence of progress.*

### 5. Re-litigating a settled direction

Told "content confirms, geometry decides", then went and gathered
evidence for why content should be primary. Direction had been given;
the correct response was to build it.

### 6. Not re-checking the whole page after each change

Changes were verified in the region the question was about, and box
counts reported without inspecting what the count was made of. The
Sidewalk Sale disappeared two changes before anyone noticed.

### 7. Page-percent is TWO units, and every shared threshold is skewed

Audited after the Sidewalk Sale was lost to it. `x%` is a percentage of
page WIDTH, `y%` of page HEIGHT. They are interchangeable only on a square
page. Every threshold written as one `_PCT` constant and applied to both
axes is therefore silently anisotropic — on 1980-04-06 p13 the vertical
reading is 1.41x the horizontal for the same physical distance.

Found in four places:

| where | effect |
|---|---|
| `separator_grid.drop_gutters` | its "ratio" is **not** an aspect ratio — a SQUARE 20x20-cell region scores 1.406. Every threshold in it was tuned against a distorted number. |
| `separator_grid.merge_double_rules` | `max()` over both axes against one threshold; 2 cells reads 1.00% across, 0.71% down |
| `_within_content` / `_fold_contained` | `CONTENT_PAD_PCT` 2.0% = 4.0 cells across, 5.6 down |
| `detect_boxes.INSET_PCT` | 2.5% = 35px across but 49px down — the rounded-corner allowance is 40% more generous vertically. `FRAGMENT_POS_PCT` likewise. |

**The fix is to work in GRID CELLS, which are square by construction** (7x7
px on p13). `ad_rectangles.py` does; the older helpers do not. Anything
new that compares a distance on one axis with a distance on the other must
convert to cells first.

### The through-line

Every one of these is the same failure in a different coat: **substituting
a number, or a local check, for looking at the artefact.** The remedy is
not more care in the abstract — it is: render the whole page, before
reporting anything, every time.

## 5p. CODE REVIEW pass (Opus subagent, 2026-08-16)

An independent Opus subagent reviewed everything built that day. Its
findings and what each turned out to be, because several were symptoms of
one another and the pattern is worth keeping.

**Two real defects, both the same mistake.** The clusters that define the
grid lines are built by splitting on gaps, so a cluster can be WIDER than
the tolerance that built it — 18 such clusters corpus-wide, worst 3.0
cells across 14 corners. Any test comparing a corner to its own line's
CENTROID is therefore incoherent, and both halves of the corner predicate
did it. `has_corner` failed one way (25 corners on 9 pages could not
certify the line they had themselves defined); `_interrupted` failed the
other (a corner belonging to the right-hand line was too far from that
line to count as part of the side, yet close enough to count as an
interior point of the TOP edge, vetoing the rectangle). Both now ask
cluster MEMBERSHIP. Detail in §5m. 266 -> 273 zones.

**One real defect the predicate structurally cannot see.** Rectangles may
NEST or be DISJOINT, never cross; the corner predicate judges each
rectangle against the corner set alone, so it is local by construction.
The archived pairing detector had a `_crosses` check and it was not
carried forward. Three crossing pairs existed on p8. Now zero.

**One finding that dissolved on measurement.** The undocumented `break`
in the neighbour scan was reported as order-dependence, and it was — but
rendering the two variants showed each losing real ads the other kept,
with totals a wash. Chasing which arbitrary variant to keep would have
been the wrong move; measuring which test actually rejected the p10 ads
found the `_interrupted` bug above, after which p10 gives the same answer
either way. **A finding that trades wins for losses is usually pointing
at something upstream of itself.**

**One false claim of my own, withdrawn.** §5n had said the zero
`duplicate`/`encloses` flags were "a live check that the corner predicate
works". They are not: `encloses` only fires when the inner zones' blocks
exactly cover the outer's, so it can barely fire at all, and 13 geometric
nestings exist.

**Mechanical, fixed without incident:** duplicate `AnnotationPage` ids on
89/90 canvases (the id came from the label's first word, and the hlines
variant emits three tiers all starting "Horizontal") — now the whole
label, slugged, with a per-canvas collision counter; `confirm_boxes_ccl`
calling a `find_boxes` that no longer existed — repointed at the live
`detect_zones`; `cell_size()` added because `CELL_PCT / aspect` was
written out at four sites; `_gutter_centres`/`_photo_units` scale
arguments made required rather than defaulting to None and silently
returning percent; the photo+caption union defined once in
`detect_captions.photo_unit()` instead of twice; `detect_zones`'s
`encloses` containment moved off flat page-percent onto cells (see
§5z.7); dead font machinery, an unused import and an unused unpack
removed.

**Known remaining instance of §5z.7:** `experiments/faces.py:143` still
uses a flat ±0.5 page-percent for its enclosure containment. It is an
experiment, not in the live path, and is left alone deliberately rather
than changed as a side effect of an unrelated pass.

## 5r. Stage 1b — SLIVERS AT THE RIM (built 2026-08-16)

`transcribe/scaled/sliver_pass.py`. Runs BEFORE the content area, and
before anything else that reads Tesseract's regions. The binding gutter
and the sheet edge come back as `ocr_separator` and `ocr_photo` regions,
and they are not printing.

**Why a dedicated pass and not a filter inside each consumer.** Every
stage that reads regions has to deal with the same artefacts, and each one
that solved it locally solved it differently and wrongly. The content area
had a page-percent band, `separator_grid` has its own `_within_content`,
and both were tuned against different pages.

### Three tiers

**TIER 1, SAFE.** A sliver lying WHOLLY inside the outer 4-cell rim is
removed outright, no alignment test. Nothing is printed in the margin.
409 of 537 removals.

**TIER 2, CANDIDATE.** A sliver reaching past the rim is removed only if
NOTHING ALIGNS with it. A real rule belongs to the column structure and
something shares its position — the blocks that stop against it, the rules
above and below it in the same gutter. 128 removals; only 25 items were
rescued by alignment corpus-wide.

**TIER 3, THE RIM MOVES.** Where content blocks intrude into the rim, the
margin on that page really is narrow — a quirk of how the page was
photographed — so the rim is pulled in per side to stop short of the
outermost intruding block. The safe tier can then never remove anything at
or beyond where content demonstrably starts. 54 of 90 pages have at least
one side pulled in. `n_intruders` travels with the decision rather than
being folded into a threshold.

### Two rules that make it work

**A SLIVER CANNOT BE CORROBORATED BY ANOTHER SLIVER.** Two shadows along
the same edge agree with each other perfectly. 1980-04-06 p4's left shadow
separator (x 1.13-2.32) was kept on "2 items align", and both vouchers
were edge regions — one the binding shadow photo at x 0.00-2.34 spanning
y 0.75-81.56. Rim slivers of any type are excluded from the alignment
count.

**THE EDGE TESTED IS THE ONE THE SLIVER RUNS PARALLEL TO.** A shadow lies
ALONG the edge it came from; it does not cross the page. Taking the
nearest edge in any direction dropped a full-width horizontal rule on
1990-10-10 p5 (x 35.15-96.18 at y 43.39-43.67, 122 cells long, mid-page)
because its right END reached the margin. Three of that page's eight
removals were this mistake.

### What is a candidate

Separators, and photos that SIZE as slivers — one `THIN_CELLS` test covers
both, so a photo wide enough to be a picture never enters the pass (568
are classified "not a sliver"). **Blocks are never candidates:** a block
with words is real type, and the same test applied to blocks would drop
664 of 8,969.

### Measured, 90 pages

    removed                537   (419 separators + 118 photos)
    tier 1                  409
    tier 2                  128
    rescued by alignment     25

    removals lying wholly INSIDE the content area -- the false-positive
    proxy -- 30 of 537, 5.6%. The thin-and-near-an-edge test it replaces
    scored 244 of 773, 31.6%.

That 5.6% is an UPPER bound: the survivors sit at x 0.15-0.40 and
y 99.45-100.00, hard against the sheet, and count as "inside" only because
the content box itself over-reaches on those pages.

### Consumed by stage 1c

`content_box_blocks` now takes `sliver_pass.survivors()` instead of its
own band. Effect on the content area: margins L6.1 R6.7 T11.4 B10.0 cells,
items outside the box 5.8%.

**Open, and it is the residual failure.** A BLOCK bbox can have the shadow
swept into it, and blocks are never sliver candidates. 1990-10-10 p15's
content left is 0.21% — 0.4 cells — set by two full-width `ocr_carea`
blocks at x 0.21-94.40 and x 0.23-95.79 whose left edge is the binding
shadow. They agree with each other, so the agreement rule passes them.
This is exactly what stage 1c's original docstring warned about blocks,
and it is not solved.

### Viewing it

`experiments/block_grid.py` draws block and photo PERIMETERS on the grid
with every removed sliver filled in bright red, so the pass is judged by
looking rather than by its own counts. It takes its verdicts from
`sliver_pass` so the view cannot drift from the pass.

## 5q. RULE RECALL IS THE CEILING ON STAGE 2b (measured 2026-08-16)

Found by acting on the second review's "render p8 and p2 before trusting
273". The CCL cross-check reported 4/38 agreement on 1980-04-06 p8 and
0/5 on 1986-01-08 p2, and those two numbers mean opposite things.

**p8: CCL over-counts, we are roughly right.** Its 38 "enclosed regions"
include every typographic cell of the Sidewalk Sale product grid and each
price row of Fraser's. The user's standing read is that those cells are
typography, not ruling. So 4/38 is not evidence against `page_zones`, and
CCL's raw count must never be treated as ground truth — it answers "what
does the ink enclose", which on a dense ad page is not "what are the
items".

**p2 of 1986-01-08: we are badly wrong, and worse than the number said.**
It is a full-page grocery ad — a lattice of roughly 40 ruled product
cells — and `detect_zones` finds **ONE** zone. CCL's 5 is no better.
Neither found the page's structure at all.

**FIRST ANSWER, WRONG — recorded because the correction is the point.**
I wrote that the cause was Tesseract's recall: only 22 separators, 5 of
them vertical, so "the ink was never reported to it". The user then
produced the separators layer for 1980-04-06 p2, which has 36, and asking
why THAT page fails too exposed the real mechanism. Generalising a root
cause from a single page is the §5z failure in another coat.

**REAL ROOT CAUSE: the corner map is built from rule ENDS ONLY.**
`_ends()` returns a separator's two endpoints and nothing else, so a rule
CROSSING another mid-span contributes no corner there. Measured, true
geometric intersections of a vertical with a horizontal against corners
actually marked:

    page             V x H    intersections   mid-span of BOTH   corners
    1980-04-06 p13   18 x 23       43                 0             36
    1980-04-06 p2    13 x 23       31                 3             29
    1986-01-08 p2     5 x 17       18                12              7

p13 is the case the derivation was built on — boxes butted together,
rules ENDING at corners, zero mid-span crossings, corner map near
complete. A grocery price grid is the opposite: it is ruled with
CONTINUOUS lines straight across the block, so two thirds of its
intersections are mid-span and every one is invisible. That is why
1986-01-08 p2 gives 7 corners and one zone, and why 1980-04-06 p2 finds
nothing inside its Red & White lattice despite 36 separators being
reported — the lattice's own intersections are not ends.

**This is a limit of OUR derivation, not of Tesseract**, and it follows
from `typesetting_practice.md`: a ruled TABLE and a stack of ruled ADS are
different printing, and the corner predicate was derived from the second.

**Not yet fixed, and not a quick patch.** Marking true crossings as
corners adds corners on every page, and every added corner can VETO a
rectangle through `_interrupted` as readily as complete one. It needs its
own measurement and a render pass across the corpus.

**BLOCKS describe these pages when ruling does not** — the user's
observation, measured:

    page             blocks  lines   block cover   zones   zone cover
    1986-01-08 p2      49      90        55.7%       1        0.9%
    1980-04-06 p2     145     343        32.6%       3        7.7%
    1980-04-06 p8     106     277        64.0%       9       18.9%
    1980-04-06 p13     49     156        38.6%       8       70.7%

On 1986-01-08 p2 the single zone covers 0.9% of the page and contains no
block at all, while Tesseract's own LAYOUT analysis covers 55.7% in 49
blocks. Its layout pass found that page; only its separator output failed
us. p13, the page the corner derivation was built on, is the one page here
where zone cover exceeds block cover.

Do NOT overread it as one-block-per-cell: median block height is 1.9% of
page height — a line or two — so blocks are SUB-cell fragments, not the
cells. What they do carry is the lattice's column structure, 12 distinct
left edges across those 49 blocks. So blocks are a real complementary
evidence source for zones on pages whose ruling is a table rather than a
stack of ads, but they need aggregating first, and that is unbuilt.

**Rule recall is a second, smaller limit, and the pixel prototype's
threshold caps it.** `experiments/rule_detection_sources.py` filters for
rules that are THIN and long, at `THIN_PX = 4`. The grocery ad's borders
are heavier than that. Sweeping it on the same page:

    thin_px   4 (current)  ->  25H /  7V
    thin_px   8            ->  69H / 26V
    thin_px  16            ->  70H / 19V

26 verticals is the right order for a 5-column lattice of product cells.
So the thinness filter, not the ink and not the corner predicate, is what
made this page invisible.

**What this means for the plan.** Step 1 of §5o removes boxed content
before re-fitting the columns. On a page where the boxes are not found,
nothing is removed, and the ad-interior contamination the step exists to
fix stays exactly where it was — on precisely the pages where it is
worst, because a full-page ad is 100% ad interior. **Both limits above are
prerequisites for step 1, not independent improvements to make later** —
and the mid-span crossing one is the larger.

Not yet done: `THIN_PX` is one number swept on one page. It needs
sweeping across the corpus and, more likely, making adaptive — a rule's
weight is a property of the printing, and §5j already records opposite
sides of one box differing 28px vs 48px.

## 5o. NEXT — the agreed sequence (set 2026-08-16, planned 2026-08-16)

Four steps, in order, each depending on the one before. Every step is
stated with the measurement that motivates it, the guard that stops it
running away, and — separately — **how it will be checked by something
that did not produce it.** That last column is the point: the second Opus
review's sharpest criticism was that every number offered as verification
so far has been one the pipeline generated about itself.

### The independent checks available

There are four, and they are independent for stated reasons:

| check | why it is independent |
|---|---|
| Horizontal rule endpoints vs column edges | horizontal rules take NO part in the column fit; currently 52% land within 1% of an edge against a 21% control |
| `items.item_type='display_ad'` | produced by the OCR+LLM route from the page IMAGE; separators and corners contribute nothing to it. Covers **83 of 90 pages, 256 of 273 zones, 359 ad labels** |
| `experiments/confirm_boxes_ccl.py` | connected-component labelling of the rule raster — a different algorithm on the same ink |
| Rendering the page | the only one that has ever caught a real regression here (§5z) |

**Known disagreement to resolve BEFORE step 1.** The CCL check currently
reports 4/38 agreement on 1980-04-06 p8, 3/9 on p2, 0/5 on 1986-01-08 p2.
Flood regions over-count (dilation closes rounded corners, gutters enclose),
so 38 is not truth — but this has not been looked at, and step 1 takes
`page_zones` as an input. Render p8 and p2 first.

### 1. Columns, pass 2 — with boxed content and photos REMOVED

**Why.** Measured and recorded in §5f: **30% of all text blocks sit inside
a display ad** (100% on a full-page-ad page), and an ad's interior is set
to its own grid, not the page's. That contamination halved the grid on
1980-04-06 p2 — 14 columns at 6.45% pitch where the real measure is
~10.5%. An earlier exclusion attempt was inconclusive *because the ad
boxes were LLM-labelled and the test was confounded*; `detect_zones` now
supplies them classically, so the experiment can finally be run clean.

**Method.** Re-fit the lattice on the evidence left after removing blocks
whose centre falls inside a `page_zones` rectangle, and the `ocr_photo`
regions. Same fitter, same weighting — this is a change of INPUT, not of
algorithm. Pass 1 is kept; pass 2 is stored alongside it.

**Guards.** Two, both already established:
  * **Pass 2 must agree with pass 1 within a margin of error** — the
    user's rule from the archived refinement pass. Not twice as many
    columns; a small correction. A pass-2 count outside pass 1 ±1 is a
    failure to investigate, not a result to store.
  * **Evidence floor.** Removing ad interiors removes real lines. If what
    remains falls below the existing `low_evidence` threshold (60 text
    lines), keep pass 1 and flag it. On an all-ads page there is no
    editorial grid to find and the honest answer is "pass 1".

**Independent check.** Rule-endpoint alignment should **improve** if the
grid is truer to the editorial page, because horizontal rules never fed
the fit. Baseline 52% within 1% against a 21% control; a pass-2 grid that
scores WORSE on this has not been improved regardless of how much
prettier its pitch looks. Plus: render 1980-04-06 p2, the known failure.

### 2. Which boxed areas are ARTICLES, not ads

**Why.** `detect_zones` finds ruled rectangles; it does not say what they
are. Steps 3 and 4 both need to know, because an ad's boundary is not an
editorial boundary.

**Method.** Classically, from evidence already in the DB: zone content
(blocks / lines / photos), the `x_size` distribution inside the zone (ad
copy is display type, body text is uniform — but note §5f measured
`x_size` does NOT cleanly separate ad interiors, 44 vs 36 overlapping, so
expect it to be one weak signal among several), Tesseract's own
`ocr_header`, position on the page, and whether the zone sits on the
column lattice.

**Guard.** Do not build a confidence score. §5g is an archived dead end
for exactly this: detectors that discover structure from weak signals and
then grade their own trustworthiness certify themselves every time. Emit
the evidence per zone and let the decision be a stated rule.

**Independent check.** `items.item_type='display_ad'`, on the 256 zones
that have it. This is a real label from a different route reading the
image. Report precision AND recall — §5c is the record of a metric that
scored a visibly wrong page 0.853 by measuring only precision.

### 3. Horizontals, re-tracked with the boxes known

**Why.** Stage 3 currently draws on rules, photo edges and heading tops
(§5h) and cannot tell an ad's top border from a rule dividing two stories.
That distinction is the whole point of the view.

**Method.** Label each alignment by whether it coincides with a
`page_zones` edge. What remains — rules and heading tops NOT explained by
a box — is editorial structure, and is the input to step 4.

**Guard.** Keep storing everything, filtering stays the caller's business
(§5h). Do not drop ad-boundary alignments; label them.

**Independent check.** Render. An editorial-only horizontal layer should
visibly follow story breaks on 1997-01-08 p1 and p4, which were chosen
earlier precisely because they have clear editorial segments.

### 4. Join non-boxed content into single items

**Why.** This is the prize: `items` segmentation is **72%** of the
OCR+LLM route's token cost — 74,720 tokens/page of the ~104k, which over
70,063 pages is where the 5.4-7.3 billion-token estimate comes from.

**Method.** Within the editorial region (page content area minus boxed
zones), group blocks into stories bounded by the step-3 editorial
horizontals and by the column lattice. Respect modular layout: a story is
a RECTANGLE spanning an INTEGER number of columns
(`typesetting_practice.md`). A story that wraps an inset ad is a
rectilinear region, not a bounding box — the same lesson as
`project_merge_polygon_union_not_bbox`.

**Guard.** Coverage is the invariant that matters: every text block must
land in exactly one item, and anything unclaimed must surface as an
explicit orphan rather than vanish. The production route learned this the
expensive way — 4 of 188 blocks silently dropped on 2001-01-03 p9 — and
answered it with `recover_orphaned_blocks()`. Build the equivalent from
the start, not after the incident.

**Independent check.** Against the production route's own `items` on the
83 covered pages: how many classically-derived items correspond 1:1 to an
LLM-derived one, and what the disagreements look like. That comparison is
the actual deliverable of this whole experiment — it is the number that
says whether the LLM pass can be reduced to a cheap confirmation, and
therefore whether the corpus is affordable.

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
- **2026-08-16** — Stage 2b consolidated onto the corner derivation
  (`detect_zones.py`, schema v20 `page_zones`); the rule-pairing detector
  and two earlier corner generations archived. See §5n, §5m.
- **2026-08-16** — Opus code review acted on. Both halves of the corner
  predicate now ask cluster MEMBERSHIP rather than distance to a line
  centroid, crossing rectangles are resolved, and the neighbour scan takes
  the nearest neighbour rather than the first in list order. 266 -> 273
  zones, crossing pairs 3 -> 0, duplicate IIIF AnnotationPage ids on 89/90
  canvases fixed. See §5p.
- **2026-08-16** — Second Opus review acted on. `encloses` made to mean
  geometric nesting (it was structurally unable to fire, 0 against 12 real
  nestings, and that zero had been quoted as evidence); `cell_size()` now
  the single definition (`build()` was a fifth site); the grid render's
  caption printed rather than discarded; `_gutter_centres` no longer takes
  an unused `chh` and can be given the caller's own lattice; stale
  266/251 measurement re-run as 273/256. See §5p.
- **2026-08-16** — Rendered p8 and p2 as the review demanded and found a
  structural limit: **the corner map is built from rule ENDS only**, so a
  rule crossing another mid-span marks no corner. Harmless for butted ad
  stacks (p13: 0 mid-span crossings of 43 intersections), fatal for a
  ruled price grid (1986-01-08 p2: 12 of 18, giving 7 corners and 1 zone).
  My first answer blamed Tesseract's recall and was WRONG — corrected
  after the user produced 1980-04-06 p2, which has 36 separators and fails
  anyway. Rule recall is a real but smaller second limit. Blocks cover
  these pages far better than zones do (1986-01-08 p2: 55.7% against
  0.9%). Both limits are prerequisites for §5o step 1. See §5q.
- **2026-08-16** — Stage 1b added (`sliver_pass.py`): rim slivers removed
  before the content area, in three tiers (wholly-inside-the-rim; reaches
  past but nothing aligns; rim pulled in where content intrudes). 537
  removed over 90 pages with 5.6% landing inside the content area, against
  31.6% for the thin-and-near-an-edge test it replaces. Stage 1c's
  agreement derivation now consumes it. See §5r.
