# Detection Methods Review

A catalogue of every detection strategy in the Almonte Gazette pipeline, with
an assessment of effectiveness and suitability for production use. Organised
by pipeline stage. Updated as detection modules evolve — see "Update history"
at the bottom.

The pipeline runs through `process_issue.py`, which orchestrates per-page
profiling → ad/sliver detection → two-pass column detection → validation →
content layer detection (headlines, body text). Each strategy below names
the file and function where it lives.

---

## Stage 1 — Page profiling (geometry & adaptive baselines)

Every detection downstream consumes a page profile. Profiling is run once
per page at 150 DPI and produces three nested rectangles (R1/R2/R3), the
text-area bounds, paper baseline statistics, binding side, and a set of
quality flags that adapt thresholds in later stages.

### 1. PDF white margin detection (R1 → R2)

**File:** `page_profile.py`, `find_rectangles()`
**DPI:** 150

**What:** Finds R1→R2 transition — where the digital PDF canvas ends and the
scanned image begins.

**Signal:** PDF margins are digitally white (inverted value < 2). Paper tone
is always > 5. This exploits a digital artefact, not a scan property.

**Effectiveness:** Excellent. Works on every page tested (1865–2005). The
threshold of 2.0 on inverted values is robust. Falls back to PDF image
placement metadata when available, else uses raster threshold.

**Production suitability:** Keep as-is. No changes needed.

---

### 2. Recto/verso & binding side from page number

**File:** `page_context.py`, `build_context()`
**DPI:** n/a

**What:** Determines binding side deterministically from the page number:
odd → recto → binding-left; even → verso → binding-right. The clean side
is the opposite edge.

**Signal:** Page numbering parity — a structural fact about bound volumes,
not a scan property.

**Effectiveness:** 100% reliable when page numbers are correct in the source
(`files` table). Replaced the older darkness-comparison heuristic in
`page_profile.py` (which sometimes reported the wrong edge as binding when
the shadow was weak or a dark ad sat on the clean side).

**What it lacks:** Nothing intrinsic — the only failure mode is an upstream
mis-numbered page. Darkness is now a confirmation signal only (`binding_confirmed`
flag), never the primary determinant.

**Production suitability:** Done — the canonical method. Was strategy "#2 to
implement" in the original audit; landed and is the active path. Darkness
comparison is preserved in `find_rectangles()` for confirmation only.

---

### 3. R2 → R3 detection (binding shadow & facing-page sliver)

**File:** `page_profile.py`, `find_rectangles()` and `detect_sliver.py`,
`find_binding_edge()`
**DPI:** 150 (both)

**What:** R2→R3 trims the binding shadow on one side and the facing-page
sliver on the other. The two edges are detected differently because they
have different signatures.

**Signal:**
- Binding edge: dark gradient peak then flatten back to paper baseline.
- Facing-page edge: a print-margin gap between main content and the sliver
  (a deep, narrow dip in the darkness profile beyond the last column).

**Effectiveness:** Mixed-but-improved.
- The binding side is handled by `find_rectangles()` walking inward until
  the darkness gradient flattens to within paper_baseline + 2σ.
- The facing-page sliver is handled by `detect_sliver.find_binding_edge()`,
  which finds the deepest dip in the binding-side darkness profile below
  `margin_thresh = paper_baseline + 0.3 × (body_median − paper_baseline)` and
  walks out to gap boundaries. This produces an explicit sliver bounding
  box that downstream column detection treats as off-limits.

**What it lacks:** Both fail when the binding shadow is so dark that no
print-margin gap separates content from sliver (very tight bindings), or
when the facing-page sliver is itself textual and merges with main content.

**Production suitability:** Keep both. Sliver detection in particular has
removed a class of false-extra-column errors at the binding edge.

---

### 4. Text-area edge detection

**File:** `page_profile.py`, `_find_text_edge()`
**DPI:** 150

**What:** Within R3, finds where print margins end and text columns begin.

**Signal:** Heavily-smoothed darkness profile (σ=15). Pattern: shadow peak
→ margin minimum → column content rise. The first local minimum after the
shadow peak marks the print margin; the rise to body-median crossing marks
the text-area edge.

**Effectiveness:** Confidence scoring (margin depth 40% + peak clarity 30%
+ transition sharpness 30%) produces meaningful differentiation: clean
edges 0.7+, noisy edges 0.2–0.4. Strategy is good on the clean
(non-binding) side. Less reliable on the binding side, where the shadow
gradient is longer and may merge with the print margin.

**What it lacks:** No fundamental difference in handling between clean and
binding edges. The dead-reckoning approach (Stage 3, strategy #9) is the
intended remedy for the binding side.

**Production suitability:** Keep for the clean edge. On the binding edge,
defer to grid projection from interior columns rather than to the text-area
edge directly.

---

### 5. Adaptive thresholds & quality flags

**File:** `page_profile.py`, `profile_page()`
**DPI:** 150

**What:** Computes a paper baseline (25th percentile of darkness) and a
shadow threshold (paper_baseline + 2.5σ), and emits quality flags
(`low_contrast`, `show_through`, `noisy_paper`, `binding_shadow_*`) that
downstream stages read to widen or tighten their parameters.

**Signal:** Page-level darkness statistics from the central 60% of rows.

**Effectiveness:** Provides a single source of truth for "what does this
scan look like?" The flags are consumed by ad detection (Tier 1 multi-pass,
strategy #11), column-rule confidence scoring, and text-area edge
detection.

**Production suitability:** Keep. Page-level adaptation is more useful than
hard-coded global thresholds.

---

## Stage 2 — Display ad & feature masking

Ad and sliver detection runs before column detection so that ad zones can
be excluded from the consensus and projection passes — a column rule that
disappears behind a half-page display ad should not penalise the rule's
detection.

### 6. Display ad detection (multi-pass adaptive threshold)

**File:** `detect_ads.py`, `detect_ads()`, `_detect_ads_pass()`
**DPI:** 150

**What:** Detects bordered display ads via adaptive threshold + contour
analysis with two passes:
- **Pass 1 (strict):** `block_size=21, C=10`, 3×3 morphological close.
- **Pass 2 (loose, "Tier 1"):** `block_size=31, C=8`, 5×5 close. Triggered
  when `page_profile.contrast < 145` or the `low_contrast` quality flag is
  present.

**Signal:** Bordered rectangles are dense closed contours when the page is
adaptively binarised; their rectangularity (filled-area / bounding-area
> 0.40), aspect ratio (not thin rules), and area (> 0.5% page) discriminate
ads from noise.

**Effectiveness:** Pass 1 covers most pages 1900–2000. Pass 2 was added
specifically for low-contrast 1890s scans where Pass 1 missed bordered ads
because the threshold sat above the faint border. The contrast-145
threshold is empirically calibrated against the test corpus.

**What it lacks:** The 145 magic number is a single-point cutoff. Ads
without borders (run-in display, large bold text) are not detected as ads
and may be picked up later by headline detection instead. Photo-edge
contours are filtered by R2-edge proximity (±3%), which can over-filter on
pages where photos legitimately border the text area.

**Production suitability:** Keep. Tier 1 has been the largest single
robustness win on early issues. The 145 cutoff is a candidate for lifting
into a named constant (see opportunities in `refactor1_recommendations.md`).

---

### 7. Single-column ad detection

**File:** `detect_ads.py`, `detect_single_col_ads()`
**DPI:** 150

**What:** Narrower-width sibling of strategy #6, tuned for single-column
display ads (classifieds, small bordered notices). Uses fragmented-contour
recovery to assemble pieces of a broken ad border into one rectangle.

**Effectiveness:** Catches ads that the multi-column pass misses because
their borders are too narrow to clear the area threshold. Sibling-merge
logic extends full-height ads to absorb short fragments with ≤6 px shared
boundaries.

**Production suitability:** Keep. Together with strategy #6, gives full
coverage of bordered display content.

---

## Stage 3 — Column boundary detection

This is the spine. Two parallel implementations exist:

- **`column_pipeline.py`** — three-stage decomposed pipeline used by
  `process_issue.py`. The live pipeline.
- **`split_page.py`** — older multi-strip orchestrator with grid
  projection. The CLI path. Reachable only via `python split_page.py …`.
  Flagged in the refactor 1 audit as a drift risk; currently retained for
  diagnostic use.

Both share the low-level detector in `find_columns.py`.

### 8. Column rule detection (valley-spike-valley + row consistency)

**File:** `find_columns.py`, `find_column_boundaries()` and
`find_column_boundaries_morph()`
**DPI:** 450

**What:** Detects vertical column rules using two parallel strategies:
- **Valley-spike-valley:** column rule = dark spike flanked by lighter
  gutters. Score = `peak − mean(flanks)`.
- **Row-by-row consistency:** a real printed rule has the same darkness
  at every row (low std). Text has high std. Confirms candidates.
- **Morphological extraction:** as a fallback, runs a tall vertical
  kernel (1 px wide × 30% strip height) which extracts continuous
  rules even when contrast is poor.

**Signal:** Column rules are physical objects: dark, thin, vertical,
flanked by gutter whitespace, present at every row.

**Effectiveness:** The best strategy for *interior* column rules. Works
across all eras tested. The valley-spike-valley pattern is physically
grounded.

**What it lacks:** Cannot detect outer edges of the first and last columns
(no rule exists there — strategy #11 handles them). Sensitive to the
darkness threshold; the typical cutoff is ~140 with row_std < 35 and
valley_depth > 60–80.

**Production suitability:** Keep as the core interior boundary detector.
Used inside text-area-clipped strips, never across the full page width.

---

### 9. Three-stage decomposed detection (`column_pipeline`)

**File:** `column_pipeline.py`, `detect_strips()` → `cluster_boundaries()`
→ `place_columns()`
**DPI:** 450 (rendering inside `find_columns`); strip selection from a
shared `CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]` and `STRIP_WEIGHTS`
(centre strips weighted higher).

**What:** Splits column detection into three pure stages:
1. `detect_strips`: runs strategy #8 on each consensus row.
2. `cluster_boundaries`: merges nearby detections, weighted by strip
   position, with a composite rate-of-change score from strip profiles
   that reinforces detections at darkness peaks.
3. `place_columns`: dispatches to `place_standard()` (regular grid) or
   `place_page2_editorial()` (special template — see strategy #15).

**Effectiveness:** This is the live path used by `process_issue.py`.
Decomposition has made each stage testable in isolation and made
template-based placement (page-2 editorial) clean to add.

**What it lacks:** Cascading errors are still possible — if strip-level
detection is poor on every row (e.g. an all-ad page), clustering and
placement have nothing to work with. Quality flags surface this rather
than hide it (`no_boundaries_detected`, `insufficient_boundaries`,
`mostly_low_confidence`).

**Production suitability:** Keep as the canonical pipeline. Two
constants — `CONSENSUS_ROWS`, `STRIP_WEIGHTS` — live here and are
re-imported by `split_page.py` after refactor 1's B2 commit.

---

### 10. Multi-strip consensus (`split_page._detect_consensus`)

**File:** `split_page.py`, `_detect_consensus()`
**DPI:** 450

**What:** Older orchestrator for the same idea as strategy #9: run
detection on 7 horizontal strips and keep boundaries that appear
consistently across strips.

**Effectiveness:** Equivalent in design to `column_pipeline`. Acceptance
threshold: `weighted_score ≥ 1.5 OR strips_hit ≥ 3`.

**What it lacks:** Counterproductive for column-spanning headlines and
display ads — a rule that vanishes where a 2-column headline crosses gets
voted down and may be lost. The newer pipeline (`column_pipeline`) makes
this auditable; the CLI path here doesn't.

**Production suitability:** CLI / diagnostic only. Live pipeline runs
through `column_pipeline`. Drift risk is documented in
`refactor1_recommendations.md` opportunity #9.

---

### 11. Grid projection from interior columns

**File:** `split_page.py`, `_project_grid_edges()` and
`column_pipeline.place_standard()`
**DPI:** n/a (operates on % positions)

**What:** Uses interior column widths to predict where outer edges should
fall if the grid were perfectly regular. Scores predictions by how well
they match detected interior boundaries; aggregates to determine outer
edges.

**Signal:** Printing-press regularity. If interior columns are 10.4% wide,
outer columns are also 10.4% wide.

**Effectiveness:** Excellent for outer edge prediction. Combined with the
deterministic recto/verso (#2) and clean-side text-area edge (#4), the
binding-edge column boundary can be dead-reckoned with high confidence.

**What it lacks:** Currently projects both directions equally; principled
asymmetry (always project FROM the clean edge TOWARD the binding edge)
would be more accurate when the clean side has a high-confidence anchor.

**Production suitability:** Keep — primary outer-edge method.

---

### 12. Narrow-column merging

**File:** `column_pipeline._merge_narrow()`, `split_page._remove_narrow_columns()`
**DPI:** n/a

**What:** Merges boundaries closer than ~7% of page width, keeping the
stronger one.

**Effectiveness:** Removes false duplicates (ad borders ≈ a real column
rule). The 7% threshold suits the Gazette's typical 10–14% column widths.

**What it lacks:** Threshold should be relative to the expected column
width, not hard-coded. A 5-column 1870s issue has wider columns;
a 1980s 4-column layout wider still — the cutoff implicitly assumes
~10% pitch.

**Production suitability:** Keep. Adaptive threshold (e.g. 0.5 × median
detected column width) is a candidate for a later round.

---

### 13. Best-grid selection (combinatorial)

**File:** `split_page.py`, `_select_best_grid()`
**DPI:** n/a

**What:** When more boundaries are detected than the maximum (8), tries all
C(N, max) combinations (capped at 15 candidates) and selects the most
regular subset by CV (coefficient of variation) of column widths.

**Effectiveness:** Mathematically sound, expensive on large candidate sets.
Used as a last resort when too many boundaries are detected.

**What it lacks:** Treats all boundaries equally when some have much higher
confidence than others. A high-confidence boundary should never be dropped
in favour of a more "regular" but lower-confidence alternative.

**Production suitability:** Keep as a last resort. Confidence-weighted
scoring is a worthwhile refinement (out of refactor-1 scope).

---

## Stage 4 — Post-detection validation

### 14. Edge-column ink validation

**File:** `validate_columns.py`, `validate_edge_columns()`
**DPI:** 75 (coarse, fast)

**What:** Drops the first or last column if its mean ink (in the body band
y=20–90%) is below 35% of the median interior column. Prevents an empty
print margin or sliver from being treated as a column.

**Signal:** Real columns have text. Margin slivers don't.

**Effectiveness:** Catches the residual cases that strategies #3 (sliver
detection) and #11 (grid projection) didn't already exclude. The 35%
threshold was empirically calibrated on the 1878–1965 sample corpus.

**What it lacks:** Only addresses edge columns. A middle column that is
genuinely empty (all-photo, all-ad with no text) survives — by design,
since middle columns are presumed to be content even when dark.

**Production suitability:** Keep. Cheap insurance against a recurring
failure mode.

---

## Stage 5 — Content-layer detection (per column)

These run after column boundaries are stable. They populate
`page_analysis.json` for the viewer and don't feed back into column
detection.

### 15. Page-2 editorial wide-column template

**File:** `column_pipeline.place_page2_editorial()`, with persistence in
`layout_intelligence.layout_templates`
**DPI:** n/a

**What:** Places columns according to the recurring "page 2 editorial"
pattern — two wide editorial columns at 1.5× standard pitch on the left,
followed by 4 standard columns. Total = 7 grid widths.

**Signal:** A persistent layout convention from roughly 1937 onward
(see `layout_observations.md`). Detected via `LayoutDB.get_template`.

**Effectiveness:** Fits a layout that the standard placer would otherwise
mangle (it would try to find 7 equal boundaries and fail on the wide
columns). When the template is in scope, placement is deterministic.

**What it lacks:** Currently a single template; later eras have variants
(e.g. wide columns only at the top, modular layouts below) that don't fit
the same shape.

**Production suitability:** Keep as template #1 of an extensible system.

---

### 16. Multi-column headline detection (gutter-fill)

**File:** `detect_headlines.py`, `detect_headlines()`
**DPI:** 150

**What:** Detects multi-column headlines by scanning vertical blocks for
"filled" gutter zones. A gutter is "filled" when its darkness exceeds 2×
the baseline gutter darkness or `baseline + 40`. Contiguous runs of 2+
filled gutters mark a headline spanning those columns.

**Signal:** Headlines extend across what would normally be empty gutter,
darkening it.

**Effectiveness:** Good for 2+ column headlines on standard editorial
pages. Handles vertical extent via run detection.

**What it lacks:** Does not detect single-column headlines (they don't
cross a gutter). Ad zones are passed in for exclusion but the algorithm
itself is gutter-centric — sparse layouts where a header sits in a wide
empty area can be missed.

**Production suitability:** Keep. Pair with single-column-headline
detection (a future strategy) for full headline coverage.

---

### 17. Body-text region detection (rhythm / periodicity)

**File:** `detect_body_text.py`, `detect_body_text()`
**DPI:** 300 (higher than ad/headline detection — see `dpi_constants.py`
rationale)

**What:** Per-column body-text detection via vertical-stripe periodicity.
Samples a fixed-width strip from each column centre, scans overlapping
windows for peak/trough periodicity (line spacing 8 × dpi/150 px,
contrast > 20, peak_mean > 15). Two passes:
1. Strict thresholds find the main body rhythm.
2. Faint-text recovery extends the regions into adjacent areas where the
   rhythm is preserved at lower contrast.
Bridges gaps < 5% page height.

**Signal:** Body text has a strong vertical periodicity (repeating dark
text rows separated by lighter inter-line space). Headlines, photos, and
ads do not.

**Effectiveness:** Reliable across eras for resolving where the main text
columns extend. The 300 DPI render is necessary — at 150 DPI, adjacent
text lines blur together and the rhythm signal collapses.

**What it lacks:** Sensitive to large line spacing and ragged-right
setting (some 1920s editorials). Depends on accurate column boundaries —
a body-text region inside a misplaced column will be misplaced too.

**Production suitability:** Keep. Higher-DPI cost is justified by the
detection-quality gain.

---

### 18. Era priors & layout templates (`LayoutDB`)

**File:** `layout_intelligence.py`, class `LayoutDB`
**Persistence:** SQLite (`page_layouts`, `page_geometry`,
`layout_templates`, `era_patterns`)

**What:** Records every successful page layout (column count, boundary
positions, widths, confidence, profile JSON), aggregates them into era
patterns by decade, and returns priors for new pages.
`get_prior(year, window=5)` returns expected_columns, typical_widths
(median), and confidence based on the surrounding ±5-year window.
`get_template(name, page, year)` retrieves named recurring patterns
(e.g. `page2_editorial_wide`).

**Signal:** Layouts persist across runs of years. A new 1923 page is much
more likely to be 7 columns than 5, given the data we already have for
1918–1928.

**Effectiveness:** Used by `page_context.build_context()` to bias
detection toward expected counts and widths. Also used by `split_page` as
a fallback when detection produces irregular results — if a known-good
issue prior exists, the detected boundaries are anchored to it.

**What it lacks:** Sparse data early in a run (a new decade with few
pages processed) gives unreliable priors; the confidence score reflects
this but the consumer code doesn't always weight it.

**Production suitability:** Keep. The cross-issue learning is the main
defence against single-page detection failures.

---

## Summary table — strategies in production

| # | Strategy | Verdict | Role |
|---|---|---|---|
| 1  | PDF white margin (R1→R2) | **Keep** | Always reliable; digital signal |
| 2  | Recto/verso from page number | **Keep — canonical** | Replaced darkness guessing |
| 3  | Binding shadow / sliver detection | **Keep** | Removes a class of false-extra-column errors |
| 4  | Text-area edge detection | **Keep — clean side only** | Defer to grid projection on binding side |
| 5  | Adaptive thresholds & quality flags | **Keep** | Single source of truth for "what does this scan look like?" |
| 6  | Display ad detection (multi-pass) | **Keep** | Tier 1 (loose pass at contrast<145) is the biggest 1890s win |
| 7  | Single-column ad detection | **Keep** | Sibling pass; together with #6 covers bordered ads |
| 8  | Column rule (valley-spike-valley + row std + morph) | **Keep** | Core interior boundary detector |
| 9  | Three-stage decomposed pipeline | **Keep — canonical** | Live path; testable stages |
| 10 | Multi-strip consensus (`split_page`) | **CLI only — drift risk** | Diagnostic; not the live path |
| 11 | Grid projection from interior | **Keep** | Primary outer-edge method |
| 12 | Narrow column merging | **Keep, make adaptive** | Cleanup step |
| 13 | Best-grid selection (combinatorial) | **Keep — last resort** | Confidence weighting is a refinement |
| 14 | Edge-column ink validation | **Keep** | Cheap insurance |
| 15 | Page-2 editorial template | **Keep** | Template #1 of an extensible system |
| 16 | Multi-column headline (gutter-fill) | **Keep** | Pair with single-column headline detection later |
| 17 | Body-text rhythm (300 DPI) | **Keep** | Higher DPI cost is justified |
| 18 | Era priors & layout templates | **Keep** | Cross-issue learning |

---

## Key architectural insights

**Anchor from the clean side.** The clean (non-binding) edge is highly
reliable. From there, interior column rules establish the grid pitch. The
binding edge should be determined by dead reckoning from the clean edge
using the established pitch — not by trying to detect it through shadow,
noise, and facing-page bleed. This principle informs strategies #2, #4,
#11, and the asymmetric handling in `page_context`.

**Profile once, adapt everywhere.** Page-level statistics (paper baseline,
contrast, quality flags) drive parameter choices in every downstream
stage. A "low-contrast" flag from `page_profile` triggers Tier 1 ad
detection (#6) and softens column-rule confidence scoring. Centralising
this avoids per-detector recalibration.

**Decompose for testability.** `column_pipeline` (strategy #9) separates
strip detection from clustering from placement, with each stage a pure
function. This makes templates (#15) a clean addition rather than a
special case threaded through a monolithic detector.

**Persist state across runs.** `LayoutDB` (#18) means the pipeline gets
better at issues from a given era as more issues from that era are
processed. This is the difference between detection and *learning*
detection.

---

## Update history

This file is meant to evolve. When a strategy is added, retired, or
materially changed, append a dated note here so the catalogue's drift is
auditable.

- **2026-04-26 — Comprehensive rewrite.** Original April 2026 catalogue
  documented 10 strategies in `find_columns`/`split_page`. This rewrite
  brings it forward to cover: page profiling and adaptive flags
  (`page_profile`, `page_context`); three-stage `column_pipeline`;
  ad detection with Tier 1 multi-pass (`detect_ads`); sliver detection
  (`detect_sliver`); validation (`validate_columns`); content layers
  (`detect_headlines`, `detect_body_text`); persistent priors and
  templates (`layout_intelligence`). Strategy #2 (binding-side from page
  number) was a TODO in the original; now landed and marked canonical.
  Old strategies retained where still accurate, re-numbered into stages.
- **(earlier)** — Original catalogue, April 2026 R&D session. Archived
  reference in `instructions/archive/newspaper_column_analysis_pipeline.md`.
