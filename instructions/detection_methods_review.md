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

**Boundary extension (`_extend_to_rules`):** After the contour pass, each
candidate's bbox is snapped outward to the nearest thick rule
(≥80% ink-fill) within 6% on each of its four sides. Mirrors the
boundary-search refinement in `detect_single_col_ads`, generalised to
all four sides. Catches the case (1947-02-27 p8) where an ad's outer
border bonds with the page running-head rectangle so the contour engine
returns only an inner sub-shape, while a thick rule sits a few percent
outside that sub-shape and IS the visible frame. Guarded against
crossing other ads' bboxes; growth capped at 6% per side.

**Edge-touching filter (softened 2026-04-28):** Candidates touching a
page edge with `rect_ratio < 0.80` used to be hard-rejected as
shadow/artifact. They are now kept with a confidence downgrade, plus
the existing edge-touch downgrade. Anchor: 1947-02-27 p8 top-right
"EYRES AND HATTON" was rejected even though it's a real ad below the
running-head rule.

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

**Page-pitch adoption (anomaly path).** `place_standard()` now
adopts a per-page pitch when none of the page's detected gaps fit
the issue-pitch acceptance window — the cluster test in
`_maybe_adopt_page_pitch` (CV < 0.10, ≥4 gaps, ≥25% off ref pitch).
On adoption, R3 is *not* used for `num_cols` or grid centre — the
detected boundary span is, since R3 is exactly what's unreliable on
these pages. Trigger case: an embedded landscape scan placed
full-width on a portrait PDF (R3 inflates to the full page, but
real content sits in a narrow band). Standard pages are
unaffected — the adoption block only fires when the issue-pitch
window finds zero usable gaps.

**Asymmetric content-band slack.** `place_standard()` clips grid
boundaries to the R3 content band, but allows each side to extend
slightly past it:
- **Binding side: `pitch * 0.5`.** The last column on the binding
  side is often narrowed by page curvature into the spine — R3 cuts
  off real content that's bent into the gutter.
- **Clean side: `pitch * 0.2`.** On ad-heavy pages R3 is biased
  inward because edge ads don't register as "content" for R3's
  body-text-driven extent measurement. The clean slack is kept
  small because there's no spine-curvature reason to expect content
  beyond R3 on the clean side; it only kicks in when the grid
  scoring already favours an offset that places a boundary in this
  range (i.e. supported by detected boundaries near the edge). Added
  2026-04-27 to recover lost rightmost columns on pages like
  1947-01-30 P5 (grid built 8th boundary at 88.21 just outside
  R3=87.26; previously dropped → 6 cols, now kept → 7 cols).

The clean slack is *insufficient* for cases where the entire
outermost column is missing from the detected-boundary set with no
target signal at all (e.g. 1947-02-06 P8: leftmost column would sit
~7%/0.7×pitch past R3 with no detected boundary to anchor it). Those
are body-text-detection failures, not placement failures — see
"Pending: ad-heavy page detection" notes for the option-(b) work
needed there.

---

### 10. Multi-strip consensus (retired — folded into `column_pipeline`)

**File:** previously `split_page._detect_consensus()`. Removed
2026-04-26 as part of refactor-1 Part 1 (opportunity #9).

**What:** Older parallel orchestrator for the same idea as strategy #9.
Now removed — `split_page.py`'s CLI invokes
`column_pipeline.detect_strips → cluster_boundaries → place_columns`
directly, eliminating the parallel implementation that had drifted from
the live pipeline.

**Production suitability:** Done — CLI and live pipeline share the same
detection chain. No drift risk remains.

---

### 11. Grid projection from interior columns

**File:** `column_pipeline.place_standard()` (the parallel
`split_page._project_grid_edges()` was removed 2026-04-26)
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

**File:** `column_pipeline._merge_narrow()` (the parallel
`split_page._remove_narrow_columns()` was removed 2026-04-26)
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

### 13. Best-grid selection (retired — never reached in live pipeline)

**File:** previously `split_page._select_best_grid()`. Removed
2026-04-26 as part of refactor-1 Part 1.

**What:** Combinatorial subset selection by CV when too many boundaries
were detected. Only ever reached from the now-retired
`_detect_consensus` orchestrator. The live pipeline (`column_pipeline`)
caps and selects boundaries during `cluster_boundaries`, so this
function had no caller after the CLI rewrite.

**Production suitability:** Done — function removed alongside its sole
caller.

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

**Production suitability:** Keep. Cheap insurance — runs in Phase A
before extraction, so phantoms are dropped before any per-column
detector wastes work on them.

---

### 14b. Edge-column multi-signal validation (v2c)

**File:** `validate_columns.py`, `validate_columns_v2()` — invoked from
`process_issue.py` after `detect_body_text` and `detect_headlines`.
**DPI:** n/a (operates on detection outputs)

**What:** Two phantom rules, OR'd, applied in a *single pass* to the
left and right edges. No iteration — the only candidate columns are
the original leftmost and rightmost, never a newly-exposed edge.

  *Rule A — empty edge.* All of:
    - `body_height_pct < 20` (sum of body-region heights in the
      column, as % of page height)
    - max ad horizontal-overlap fraction `< 0.30`
    - max headline horizontal-overlap fraction `< 0.30`
  Catches scan-bleed / edge-rule "columns" that have ink but no
  real content.

  *Rule B — out-of-volume page edge.* Both of:
    - `body_height_pct / interior_median_body < 0.85`
    - column extends past `text_area` edge by `> 1.0%` of page width
      (raised to `1.5%` when interior median body height is below
      `25%` — sparse-body gate, see below)
  Catches strips of an underlying page in a bound volume that have
  slipped out of register and are physically visible past this
  page's body band. The text in the strip is real — but it belongs
  to a different page. Body ratio alone misfires on real-but-sparse
  edge columns; text_area extension alone misfires when text_area
  was estimated narrowly. Together they specifically describe
  "real text where this page's content shouldn't reach."

**Conservatism rules (v2c, 2026-04-27):**

  *No iteration.* Only the leftmost and rightmost columns are ever
  candidates — peeling and re-evaluating a newly-exposed edge against
  a shrinking interior median produces runaway drops (1947-12-24 P8:
  7c → 3c when 4 columns peeled sequentially against a collapsing
  median). The user's principle: "left and right cols are the only
  ones that will be in question."

  *Ad anchor.* If any detected display ad covers ≥ 50% of a candidate
  column's width, the column is never dropped, regardless of body or
  headline signals. A typeset ad block on a column proves the column
  is real (1947-04-17 P7: a 7-col headline ad + 4-col cartoon ad;
  body coverage was tiny but the column was anchored by ads).

  *Symmetric-drop tiebreaker.* If both edges qualify for dropping,
  only the side with lower body coverage (the more phantom-y of the
  two) is dropped. A symmetric 8→6 drop almost always loses at least
  one real column unless the page is a 2-wide-+-4-narrow editorial
  layout, which is rare outside page 2 (1947-04-17 P3 originally
  dropped both edges → 6c; with the tiebreaker, drops only the
  weaker right edge → 7c).

  *Sparse-body gate.* When the page's interior median body height is
  below `25%`, the page is ad-dominated and the body-ratio signal in
  Rule B is noisy. The text_area extension threshold is raised to
  `1.5%` to compensate. Rule A is unchanged because its ad and
  headline signals are independent of body sparsity.

When a column is dropped, `process_issue` re-extracts PNGs against
the new boundaries, filters and renumbers per-column data in
`page_analysis.json` (`body_text`, `body_text_charts`, `h_rules`,
`large_type`), and tags the page with `col_v2_drop_left/right`
quality flags.

**Signal:** A real column hits at least one positive content signal
*and* aligns with the page-profile-derived body band. Phantom edges
fail Rule A (no signals at all) or Rule B (real but mis-aligned text
from a different page).

**Why both v1 and v2:** v1 (#14a) is cheap and runs pre-extraction,
saving downstream work on obvious phantoms. v2 is the safety net for
phantoms with ink but no real content (Rule A) and for out-of-volume
page-edge bleed (Rule B). Threshold rationale (1947-01-09):
  - Rule A: phantom edges sat at 4.5–11.3 % body height; real
    columns at ~51 %. 20 % cut-off separates them.
  - Rule B: every kept real edge had body ratio ≥ 0.85 OR no
    text_area extension; phantoms had ratio 0.13–0.78 with
    extension 1.5–5.7 %. Both signals required.

**Bias by design.** v2c is biased toward keeping columns. The
operator preference: false positives (lost real cols) cost more than
false negatives (kept phantoms). Manual cleanup of a phantom edge
column via the CLI is cheap; a real column lost without trace is
expensive.

**Edge-only by design** (same as v1): an interior near-empty column
is more likely a real column with sparse content than a segmentation
error.

**What v2 does not address:** placement-stage missed columns. On
ad-heavy pages with very little body text (1947-01-30 P5; 1947-02-06
P8), the boundary detector itself can fail to place a column whose
content is entirely ads — body-text gaps are the primary placement
signal. v2 cannot recover a column that was never placed; these
cases need either an ad-driven placement extension (future work) or
manual CLI correction.

**Production suitability:** Keep. Rule A caught the 1947-01-09 p1
anomaly residue; Rule B caught the four 8c-instead-of-7c pages
(P3/P5/P7 right, P8 left) on the same issue. v2c (single-pass + ad
anchor + symmetric tiebreaker + sparse-body gate) tightened the
1947 batch from 79 drops with several false positives down to a
smaller, higher-precision drop set.

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
| 14a | Edge-column ink validation (v1) | **Keep** | Pre-extraction phantom drop |
| 14b | Edge-column multi-signal validation (v2c) | **Keep** | Post-detection phantom drop: Rule A (empty edge: body+ad+headline all fail) OR Rule B (out-of-volume page edge: low body ratio + extends past text_area). Single-pass; ad-anchor protection; symmetric-drop tiebreaker; sparse-body gate. Biased toward keeping columns. |
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

- **2026-04-28 — `detect_ads` boundary extension + edge-filter
  softening.** `_extend_to_rules` helper added: post-contour, each
  multi-col candidate's bbox snaps outward to the nearest ≥80%-ink rule
  on each side within 6%, capped at 6% per side, guarded against
  crossing other ads. Mirrors `detect_single_col_ads`'s boundary-search,
  generalised to all four sides. Edge-touching `rect_ratio < 0.80`
  candidates are no longer hard-rejected as artifacts; they pass with
  a confidence downgrade and inherit the existing edge-touch downgrade.
  Driven by 1947-02-27 p8 where the page running-head rectangle bonded
  with three ads' outer borders, leaving inner sub-shapes that stopped
  short of the visible frame and triggered `edge_horiz_low_rr` /
  `edge_vert_low_rr` rejections on legitimate display ads. Net effect
  on 1947-02-27: 28 → 29 ads, with 23 of the 29 picking up small
  outward bbox corrections. Also added `verbose=False` flag to
  `_detect_ads_pass` for stderr-JSONL diagnostic logging
  (byte-identical when off).
- **2026-04-27 — Clean-side placement slack in `place_standard`.**
  Added `clean_slack = pitch * 0.2` so the grid-clipping limits
  extend slightly past R3 on the clean side as well as the binding
  side (which had `bind_slack = pitch * 0.5`). Driven by the v2c
  inspection finding that ad-heavy pages have R3 biased inward —
  edge ads don't register as "content" for R3's body-text-driven
  extent measurement. Verification on three issues:
  - 1947-01-30 P5: 6c → 7c (rightmost col at 88.21 was previously
    clipped by R3=87.26; new col contains a real Salada Tea/Coffee
    ad headline plus body text — visually verified).
  - 1947-01-30 P3: minor position shift (still 7c, no longer needs
    validator drop_right intervention).
  - 1947-02-06 P5: gains a v2 drop_right flag (slack admitted an 8th
    candidate, validator correctly rejected it as phantom — system
    working as designed).
  - 1947-02-06 P8: still 6c — leftmost real column would sit ~7%
    (~0.67×pitch) past R3 with no detected-boundary signal to anchor
    it, beyond what any reasonable slack value can recover. Documented
    as a body-text-detection (option-b) case in the pending notes.
  - 1947-11-06: byte-identical PRE/POST (no regression).
  Slack is opportunistic: a grid offset that places a boundary in
  the slack region only wins if there's a detected-boundary target
  near it, so the change is conservative on pages where R3 is the
  true content edge.
- **2026-04-27 — Validator v2c: conservatism rules.** After the 1947
  batch rerun (42 issues, 79 v2b drops), inspection revealed several
  false positives where real columns were dropped. Four bias-toward-
  keeping changes to `validate_columns_v2`:
  1. **No iteration.** Replaced the 4-pass peel-and-recheck loop
     with a single pass evaluating only the original leftmost and
     rightmost columns. The peel was eating real columns as the
     interior median collapsed (1947-12-24 P8: 7c → 3c with four
     sequential drops; under v2c, 7c → 6c, single right drop).
  2. **Ad anchor.** A column where any detected ad covers ≥ 50 % of
     the column width is never dropped, regardless of body or
     headline signals. A typeset ad block on a column proves it is
     real. (1947-04-17 P7: was being dropped despite a 7-col headline
     ad covering both edges; v2c keeps it.)
  3. **Symmetric-drop tiebreaker.** If both edges qualify for
     dropping, only the side with lower body coverage is dropped.
     A symmetric 8 → 6 drop almost always loses a real column.
     (1947-04-17 P3: was 8 → 6; under v2c, 8 → 7, dropping only the
     weaker right edge.)
  4. **Sparse-body gate.** When interior median body height is below
     25 %, the page is ad-dominated and `body_ratio` (Rule B) is
     noisy. The text_area extension threshold is raised from 1.0 %
     to 1.5 %.
  Operator preference: false positives (lost real columns) cost more
  than false negatives (kept phantoms); v2c is biased accordingly.
  v2 does *not* address placement-stage missed columns on ad-heavy
  pages — that's a separate concern. The clean-side placement slack
  added later the same day recovered 1947-01-30 P5; 1947-02-06 P8
  remains a body-text-detection (option-b) case.
- **2026-04-27 — Validator v2b: out-of-volume page edge rule.**
  Adds a second phantom rule to `validate_columns_v2` alongside
  the existing "empty edge" rule. Catches strips of an underlying
  page in a bound volume that have slipped out of register and
  show real, well-formed body text past the photographed page's
  body band. Trigger: body coverage < 0.85 of interior median
  *and* column extends past `text_area` edge > 1.0 % of page width.
  Both signals required — body ratio alone misfires on real-but-
  sparse edge cols; text_area extension alone misfires when
  text_area was estimated narrowly. Threads `text_area` from the
  page profile into the validator at the v2 call site.
  Verification: 1947-01-09 P3/P5/P7 right and P8 left correctly
  drop from 8c→7c. 1947-09-25 and 1947-11-06 byte-identical.
- **2026-04-27 — Validator v2 + page-pitch adoption for anomaly
  pages.** Two complementary changes against the
  empty-edge-column failure mode:
  1. **`validate_columns_v2`** (strategy #14b) runs after
     `detect_body_text` and `detect_headlines`. Drops an edge
     column only if it fails all three positive signals
     (body_height_pct < 20, ad_overlap < 0.30, hl_overlap < 0.30).
     Iterative edge peeling, 4-pass cap. `process_issue` re-extracts
     PNGs and renumbers per-column data when a drop fires; tags the
     page with `col_v2_drop_left/right` flags. Edge-only by design.
  2. **Page-pitch adoption in `place_standard`** (strategy #9
     refinement). When the issue-pitch acceptance window finds no
     usable gaps, check whether this page's detected gaps form a
     coherent grid at a different pitch (`_maybe_adopt_page_pitch`:
     CV < 0.10, ≥4 gaps, ≥25% off ref pitch). On adoption, derive
     `num_cols` and grid centre from the detected boundary span —
     not R3, which is exactly what's unreliable on these pages.
     Trigger: embedded landscape scan placed full-width on a
     portrait PDF (1947-01-09 p1 is the canonical case — R3 inflates
     to 93.5%, but content sits at 38.7-73.5%). Without this, even
     v2 can't fully recover: the issue-pitch grid stamps too many
     phantom columns, and v2's 4-pass cap leaves residue.
  Verification: 1947-01-09 p1 went from 12 phantom-heavy columns
  spanning 11-77% to 6 columns at 38.84-71.72%, aligning to
  detected boundaries within 0.13%. All other 1947-01-09 pages
  byte-identical pre/post (the adoption block only fires when the
  issue-pitch window finds zero gaps).
- **2026-04-27 — `process_issue` pipeline split: place → reconcile →
  detect.** Restructures the per-page work in `process_issue.py` into
  three explicit phases:
  1. **Pass 2 Phase A** — place columns + cheap edge ink validation
     (`validate_edge_columns`). No PNG extraction, no body_text or
     headline detection. Stores boundaries + context in
     `page_layouts[page_num]`.
  2. **Pass 3** — cross-page recto/verso left-edge reconciliation.
     Updates boundaries in `page_layouts` only. The previous
     implementation re-extracted PNGs and re-saved `page_meta.json`
     here, which left `page_analysis.json` (already written from the
     pre-pass-3 boundaries) inconsistent with the new layout. The
     1947-batch artefact this caused: 1947-01-09 p1 had 6 body_text
     charts on disk, but `page_layouts` and `page_meta.json` recorded
     8 columns — a Frankenstein page where two of the eight columns
     were phantom margin/scan-bleed.
  3. **Pass 2 Phase C** — extract column PNGs, run
     `detect_headlines` and `detect_body_text` on the **final**
     boundaries, save `page_analysis.json`. Body-text charts and the
     layout in `page_meta.json` / `page_layouts` are now guaranteed
     consistent. This is the precondition for the body-text-aware
     column validator (next commit) — without consistency, the
     validator's signal would be operating on stale data.
  No detection logic changed. DB output is byte-identical for clean
  pages (1947-09-25, 1947-01-09 layouts verified). The only
  observable difference is `page_analysis.json` content on
  pass-3-outlier pages: charts now reflect the post-pass-3 layout.
  Verification: 1947-01-09 p1 post-restructure has 8 charts (was 6),
  with body counts 140/100/1380/1580/1560/1540/40/40 of 1747 — i.e.
  cols 0,1,6,7 all <10% body; cols 2-5 all >75% body. The phantom-
  margin signal the validator-v2 will key on is now visible in the
  per-column body_count.
- **2026-04-27 — Issue-level parallel batch driver + coordinator
  pattern.** Adds `archive.py` (`process_archive`) and `db_writer.py`
  (`DBWriter`/`DirectDBWriter`/`ProxyDBWriter`). Detection logic is
  unchanged; only the orchestration layer above `process_issue` and
  the DB-write layer below it. Three structural shifts worth
  recording:
  1. **UUIDs on `detected_ads`** alongside the integer `id` (schema
     migration `migrations/001_detected_ads_uuid.sql`). Workers stamp
     each ad with a uuid at detection time, eliminating the round-trip
     for an auto-increment id. Integer `id` retained as a debug handle.
  2. **Coordinator-owns-DB pattern.** A single thread in the parent
     process owns the only writing connection. Workers send writes
     through `mp.Queue` via `ProxyDBWriter`; the coordinator drains
     and dispatches to a `DirectDBWriter`. Eliminates write contention
     by construction — no retry-on-busy logic needed. SQLite's
     concurrent-readers guarantee covers worker-side reads. WAL mode
     enabled at batch start so reads don't briefly stall against
     writes.
  3. **`process_issue` now takes `writer` and `skip_aggregates`
     kwargs.** Default behaviour unchanged when `writer=None` (a
     `DirectDBWriter` is constructed against `db_path`).
     `compute_era_patterns` and `_update_viewer_data` (cross-issue
     aggregates) are skipped per-issue under the batch driver and run
     once at end-of-batch.
  Verification: 2-issue serial vs parallel run on 1947-11-06 +
  1937-01-14, byte-identical across `detected_ads`/`page_layouts`/
  `page_geometry` (excluding `id`/`uuid`/`created_at`). 4-issue ×
  4-worker stress run completed clean. Wall time on 2 issues: 31.9s
  parallel vs 57.3s serial (1.79× on 2 workers).
- **2026-04-27 — `mvtm` CLI walking skeleton + `hand_edited` write-path
  respect.** Refactor 1 Part 2 first three commits land:
  1. `mvtm_cli.py` introduces an LLM-facing umbrella CLI. Uniform
     JSON envelope `{ok, command, transaction_id, result, errors}`
     on stdout (compact), human progress logs on stderr. Frozen
     error codes: `validation_error`, `not_found`, `pipeline_error`,
     `would_clobber_hand_edit`. Per-stage CLIs (`split_page.py`,
     `detect_ads.py`, …) remain as human diagnostic tools and are
     not affected.
  2. `mvtm show <Y> <M> <D> <page>` — read-only inspection of one
     page (layout, geometry, ads, headlines/body_text/h_rules/
     large_type, file pointers). Chart-heavy keys
     (`profile_chart`, `composite_profile`, `strip_profiles`,
     `headline_chart`, `body_text_charts`, per-headline
     `row_chart`/`col_charts`) are excluded — viewer-only data.
     Add `--include-charts` if a real consumer surfaces.
  3. `mvtm recompute-layers <Y> <M> <D> <page> [--layers L,L]` —
     re-runs the post-detection layers (`headlines`, `body_text`)
     for one page and splices the regenerated keys into
     `page_analysis.json` without perturbing other keys. Reproduces
     `process_issue.py:559-602` exactly: same kwargs, same
     conditional-set pattern (so `headline_chart=null` round-trips).
     `ad_zones` reconstructed from DB ads via
     `get_ad_exclusion_zones`. PDF must already be cached at
     `/tmp/issue_<date>/<file>.pdf`; otherwise `not_found`. No DB
     writes. The shared-functions principle is load-bearing — if
     `process_issue`'s detector kwargs change, this CLI must change
     in the same commit.
  4. **Migration 002** adds `hand_edited INTEGER DEFAULT 0` to
     `page_layouts`, `detected_ads`, and `page_geometry`, plus a
     new `cli_history(id, ts, command, table_name, row_key_json,
     before_json, after_json)` audit table. `cli_history.py`
     provides a `record_change()` helper (groundwork — no caller
     yet, lands with the first mutator).
  5. **`DirectDBWriter` is the seam for hand-edit respect.**
     `delete_issue_layouts`/`delete_issue_ads` scope their DELETEs
     with `AND hand_edited=0`; `record_layout`/`record_geometry`
     short-circuit if a hand-edited row exists at the same
     `(year, month, day, page)` key. Skip events print a
     `[skip P3 layout delete: hand-edited]` line each. Ads are
     preserved-with-coexistence: hand-edited ads survive the
     issue wipe but fresh detections still insert (distinct uuids).
     The duplicate-on-the-page risk is accepted until an LLM
     mutator pattern emerges that addresses it (uuid-based update
     vs full re-detect). `process_issue` itself is unchanged — the
     skip logic lives entirely in the writer.
  Verification: serial-vs-parallel byte-identical smoke test (2
  issues, 71 ads / 16 layouts / 16 geometry rows) still passes
  after the writer change. Manual hand-edit test on
  `1947-11-06 P3`: bogus `(num_columns=99, confidence=0.42,
  hand_edited=1)` row preserved across a fresh `process_issue`
  run; reset to `hand_edited=0` and re-run overwrites correctly.
  `mvtm show` reflects `layout.hand_edited == true` for the
  flagged row.
- **2026-04-26 — Refactor-1 Part 1: split_page CLI drift eliminated.**
  `split_page.py`'s parallel column-detection (`_detect_consensus`,
  `_project_grid_edges`, `_remove_narrow_columns`, `_select_best_grid`,
  `_validate`) removed (~760 lines). The CLI now invokes
  `column_pipeline.detect_strips → cluster_boundaries → place_columns`
  directly, then `validate_edge_columns`, then `extract_columns`. The
  three kept exports (`PageResult`, `extract_columns`, `_save_metadata`)
  are still imported by `process_issue`. Strategies #10 and #13 marked
  retired; #11 and #12 updated to remove the parallel-implementation
  pointers. 1947-11-06 regression: zero deltas vs baseline (32 ads,
  8 page_layouts, 8 page_geometry, identical column widths/CV).
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
