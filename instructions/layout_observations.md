# Layout Observations

Field notes from processing pages across the Almonte Gazette's 145-year
run (1862–2007). Documents the variety of layouts, scan conditions, and
edge cases encountered. This is corpus knowledge that doesn't live in
the code.

When a new issue surfaces a noteworthy pattern, append it here — even one
sentence is worth recording. See "Update history" at the bottom.

---

## Column counts by era

| Era | Typical columns | Pitch | Notes |
|-----|----------------|-------|-------|
| 1860s–1870s | 5 | wide  | Wider columns, dense classified ads. 1878-12-27 page 3 showed what looked like 8 columns but one was a sideways ad creating an irregular narrow column alongside 7 regular ones. |
| 1880s | 5–6 | wide | Similar to 1870s; 6-column variants emerge. |
| 1890s | 7–8 | ~10% | 1897-07-09 detected as 8c at 10.3% pitch; 1898-10-07 detected as 8c at ~11%. Era runs at 7–8 columns; **low contrast on early scans triggers Tier 1 ad detection** — see strategy #6 in `detection_methods_review.md`. |
| 1900s | 7 | ~11% | Stabilises on 7-column grid. 1905-12-29 page 3 was the cleanest detection on record — all 7 columns at exactly 11% width. |
| 1910s–1920s | 7 | ~11% | Consistent 7-column grid. 1920-01-02 was the primary test case during early R&D. |
| 1930s–1940s | 7 | ~12% | Same grid persists. 1937-01-14 and 1947-11-06 are canonical regression issues. |
| 1950s | 7→6 | ~12% | Transition starts; 1952-12-25 still 7c but with mixed ad disruption. |
| 1960s | 6 | wider | Transition to fewer, wider columns. |
| 1970s | 7 | ~11% | 1974-05-23: 7c editorial, but heavy ads (53 detected) and supplement pages disrupt some pages. |
| 1980s | n/a — modular | n/a | **Modular broadsheet, not a column grid.** Articles are bounded rectangles with local column counts; no page-level rules. See `post1980_layout_observations.md` for the full characterisation (`detected_ads` / `page_layouts` aggregates from the 1980 trial run are misleading — those cuts were "beyond useless" and were deleted). |
| 2000s | n/a — modular | n/a | Same modular paradigm as 1980s, with heavier classifieds and full-page display ads. See `post1980_layout_observations.md`. |

Column counts in `LayoutDB.era_patterns` (run `python3 layout_intelligence.py
data/mvtm.db` for the live aggregate) confirm the ranges above.

---

## Canonical regression issues

These four are the standing regression set. Every refactor commit on the
`refactor-1` branch reprocesses all four and diffs row counts in
`page_layouts`, `page_geometry`, `detected_ads`, `layout_templates`,
`era_patterns` against the pre-change baseline. Zero deltas across all
five tables = behaviour preserved.

- **1898-10-07** — 1890s low-contrast scan; exercises Tier 1 ad detection
- **1920-01-02** — clean 7-column 1920s; primary R&D issue
- **1937-01-14** — 7-column 1930s; includes the page-2 editorial wide-
  column template (now in `column_pipeline.place_page2_editorial`)
- **1947-11-06** — 7-column 1940s; current detection target

When adding new test material, prefer extending this set rather than
replacing it.

---

## Pages processed

### 1878-12-27 page 3 (recto)
- **Layout:** 7 regular columns + 1 irregular (sideways ad)
- **Interior columns:** 10.4–10.9% — very regular
- **Binding:** Left (recto). Darkness check did NOT confirm
  (`binding_confirmed=False`) — at the time this was a worry, but with
  the move to recto/verso-from-page-number (strategy #2 in the methods
  review), darkness is now confirmation only.
- **Notable:** A sideways/rotated ad created a narrower column. The
  regular grid underneath was 7 columns.
- **Clean side confidence:** 0.666

### 1878-12-27 page 4 (verso)
- **Layout:** 7 regular columns at 10%
- **Interior columns:** 9.9–10.1% — exceptionally regular
- **Binding:** Right (verso), confirmed by darkness
- **Notable:** Best regularity seen in the 1870s. All interior columns
  within 0.2% of each other.
- **Clean side confidence:** 0.319 (surprisingly low for a clean page)

### 1897-07-09 (full issue, 8 pages, 8 columns at 10.3% pitch)
- Grounding pages: 8 (CV=0.022) and 2 (CV=0.030).
- Pass 1 showed a recto/verso split: recto pages detected 7, verso 8.
  The pitch establishment correctly chose 8 from the verso pages and
  re-processed all recto pages to 8 columns via anchored transposition.

### 1898-10-07 (full issue, 8 pages, 8 columns)
- **Notable:** This is the issue that motivated Tier 1 multi-pass ad
  detection. Pass 1 (strict adaptive threshold) missed several bordered
  ads on faint scans where the border was just above the threshold.
  Adding Pass 2 (block_size=31, C=8, 5×5 close) recovered them.
  Triggered when `page_profile.contrast < 145`.
- **Use:** Canonical "low-contrast 1890s" regression page.

### 1905-12-29 page 3 (recto)
- **Layout:** 7 columns
- **Interior columns:** 10.6–11.3% — good regularity
- **Binding:** Right per darkness; recto should be left — at the time
  this was logged as "scan orientation may differ"; now resolved by
  page-number-based binding determination.
- **Notable:** Cleanest overall detection across all test pages.

### 1920-01-02 (8 pages, 7 columns)
- **Layout:** 7 columns throughout
- **Interior columns:** 10.3–11.4% typical
- **Notable:**
  - Page 2: "His Master's Voice" display ad spans the lower half. Interior
    column rules still detected well above the ad. This is the page where
    we refined grid projection and dead-reckoning approaches.
  - Page 1 (front): Masthead complicates upper-strip detection.
  - Pages 5, 8: Binding shadow on right side. Column-spanning elements
    caused missed boundaries in some strips.
  - `binding_confirmed=False` was common on this issue under the old
    darkness-comparison logic; resolved by page-number-based binding.

### 1937-01-14 (8 pages, 7 columns at 11.9% pitch)
Grounding pages: 4 (CV=0.013) and 1 (CV=0.027). Pitch established from
these two.
- **Page 1** (recto): Excellent. Clean 7-column grid.
- **Page 2** (verso): Good grid but the **left two columns are at 1.5×
  standard width** — a deliberate editorial wide-column convention. This
  is the page that motivated the page-2 editorial template (now in
  `column_pipeline.place_page2_editorial`, persisted as a row in
  `layout_templates`).
- **Page 3** (recto): Very poor detection. A large display ad disrupts
  the grid completely. The anchored transposition placed columns at
  regular intervals but the anchor point was wrong, shifting the entire
  grid. **This is why p3 should NOT be the default grounding page.**
- **Page 4** (verso): Excellent. One of the two grounding pages.
- **Page 5** (recto): A giant display ad in the lower half has its own
  column grid (different pitch from editorial). Detection picks up the
  ad's columns, distorting lower strips. Editorial grid in upper half is
  correct.
- **Page 6** (verso): Excellent.
- **Page 7** (recto): A **photograph spanning three columns** in the top
  right disrupts detection. The photo obliterates column rules in that
  zone across the upper strips, and the consensus loses those boundaries.
  Multi-column photos are a distinct challenge from multi-column ads —
  they have no borders, just sustained mid-tone darkness.
- **Page 8** (verso): Good with minor inaccuracy over ads. Editorial grid
  correct; column boundaries drift slightly where ads sit.

### 1943-12-30 page 8
- **Layout:** Advertising page — irregular, no clean column grid.
- **Notable:** First test page from early R&D. Dense ads with thick
  borders confused detection. Not representative of editorial pages.

### 1947-11-06 (full issue, canonical regression)
- **Layout:** 7 columns at ~11% pitch across the issue.
- **Notable:** Current standing regression target for refactor 1 commits.
  All pages produce CV=0.000 grids on the post-refactor-1 pipeline.

### 1947-01-09 — out-of-volume page edges on most editorial pages
- **Layout:** Issue is 7c at ~10.9% pitch (P3-P8 editorial). Scan
  conditions are messy: pages of the bound volume have slipped out
  of register, so the edge of an underlying page is physically
  visible past the photographed page on multiple sheets.
- **Pages affected:** P3 right, P5 right, P7 right (recto, clean
  edge); P8 left (verso, clean edge). Without v2b these came out
  as 8c with a phantom edge column whose content was a partial
  strip of a different page's text.
- **Identifying signature:** body coverage on the phantom edge sits
  at 0.13–0.78 of the interior median; column extends past
  `text_area` edge by 1.5–5.7 % of page width. The text in the
  strip is real (so v2 Rule A's body/ad/headline check passes),
  but the column is geometrically outside the body band of THIS
  page. v2 Rule B catches this.
- **Reference case for:** the v2b "out-of-volume page edge" rule
  in `validate_columns_v2`. P6 left (also low body ratio at 0.46,
  but inside text_area) is the protective contrast — v2b correctly
  keeps it.

### 1947-01-09 page 1 (anomaly — landscape scan in portrait PDF)
- **Layout:** 6 columns at 5.48% pitch, content band 38.84-71.72%.
- **Notable:** The embedded image is placed full PDF width
  (`pdf_image_rect` x = 4.76-100%, y = 3.37-53.17%) but the actual
  newspaper content occupies only the upper-half landscape strip. R3
  detection is fooled — it reports the full page (93.5% wide) — but
  Pass 1 ink boundary detection gets the correct content band.
- **Reference case for:** the page-pitch adoption path in
  `place_standard` (`_maybe_adopt_page_pitch`). When the issue-pitch
  acceptance window finds zero usable gaps, the code now adopts the
  page's own pitch and uses the detected boundary span — not R3 —
  for column count and grid centre. Before this fix, the page came
  out as 12 phantom-heavy columns spanning 11-77% of page width.
- **Aspect-ratio anomaly check** is on the wishlist: a portrait PDF
  with a landscape-shaped embedded image is detectable up front and
  could trigger an alternate placement path more reliably than the
  current "no usable gaps in window" trigger.

### 1952-12-25 page 3 (recto)
- **Layout:** 7 columns expected
- **Interior columns:** 11.5–12.1% for confirmed interior
- **Notable:** Right side (clean) had narrow columns 6 and 7 (7.4%, 5.9%),
  suggesting grid projection didn't extend far enough or text_area was
  too conservative on the clean side.

### 1952-12-25 page 4 (verso)
- **Layout:** Mixed content, irregular
- **Interior columns:** Wide variation (5–15%)
- **Clean side confidence:** 0.197 — very low, suggesting damage or
  unusual content on the left edge
- **Notable:** Most irregular column widths of any page tested. Heavy ad
  content disrupts the grid.

### 1974-05-23 (10 pages, heavy ads — 53 display ads detected)
First issue from the 1970s. Much denser advertising than earlier decades.
- **P3, P9** (recto): 7–8 columns at ~11% — clean editorial pages,
  good detection.
- **P5, P10:** Detection failed — likely all-ad pages with no clear
  editorial column grid.
- **P7:** 5 columns with some at 21% — heavy ad content disrupts grid.
- **Verso pages:** Anchored transposition produced 18% columns (wrong
  pitch) because pitch establishment selected 5 columns instead of 7.
- **Key finding:** 53 ads — 1970s have ~5× the ad density of 1890s
  issues. Ad detection works well; the pitch establishment needs to
  handle the case where recto editorial pages clearly show 7 columns
  while the orchestrator picked 5.
- **Likely column count:** 7 at ~11%, same as earlier decades. P5 and
  P10 may be advertising-supplement pages with a different grid.

---

## Scan conditions

### Binding shadow
- Present on all bound-volume scans.
- Severity varies widely even within the same issue.
- The `binding_confirmed` flag (darkness agrees with page number) used to
  flip True/False unpredictably; under the page-number-based binding
  determination (strategy #2 in the methods review), it's now a
  confirmation signal only.

### Facing-page sliver
- Appears on the binding side (gutter edge).
- Visible on some pages, absent on others. Depends on binding tightness
  and scan orientation.
- Creates a second peak in the darkness profile beyond the last column;
  must not be mistaken for an additional column. **Now handled
  explicitly** by `detect_sliver.find_binding_edge()` (strategy #3) which
  produces a sliver bounding box that downstream detection treats as
  off-limits.

### Page damage
- Tears, stains, and foxing visible on older pages.
- Affects the darkness profile and can shift text-area detection.
- Manifests as reduced confidence scores on the affected edge.

### Column-spanning elements
- Display ads (e.g. His Master's Voice) span 2–4 columns.
- Headlines occasionally span 2 columns.
- Multi-column **photos** (1937-01-14 p7) have no border — sustained
  mid-tone darkness obliterates column rules in that zone.
- Multi-strip consensus handles most cases but occasionally votes down a
  real boundary; this is now logged as a quality flag rather than
  silently absorbed.

### Low contrast (1890s in particular)
- Faint ad borders sit just above Pass-1 adaptive threshold and are
  missed by strict parameters.
- `page_profile.contrast < 145` triggers Tier 1 (looser) ad detection
  (Pass 2 with block_size=31, C=8, 5×5 close).
- 1898-10-07 is the canonical example.

### Heavy advertising (1970s onward)
- 1974-05-23 detected 53 ads vs ~10 typical for 1890s–1940s.
- All-ad supplement pages can fail editorial detection entirely; the
  pipeline records the failure rather than fabricating a grid.

---

## Recurring layout patterns

### Page-2 editorial wide-column template
- **Pure form (1937–1952):** Two wide editorial columns (each 1.5×
  standard pitch) on the left, followed by 4 standard columns. Total =
  7 grid widths.
- **Modified form (1965 onward):** Same wide editorial columns at the
  TOP of the page. Below that, the layout shifts to pairs of standard-
  width ads with a single text column tucked to the right. Underlying
  grid pitch unchanged.
- **Not present in 1929** — the transition happened between 1929 and 1937.
- **Post-1965:** "Jigsaw" layouts emerge where different vertical zones
  use different column arrangements, all on the same underlying grid.
  The wide editorial column tradition persists at the top of page 2 but
  the lower portion becomes increasingly ad-driven.

This pattern is **now in code** as
`column_pipeline.place_page2_editorial`, with the template stored in the
`layout_templates` SQLite table. When updating this section, also check
whether the implementation needs an update — the two should track each
other.

---

## Key patterns

1. **Interior column rules are always reliable** — the valley-spike-valley
   detection with row consistency works across all eras (1865–2005).
2. **Outer edges are always problematic** — no vertical rules exist at the
   outer edges of the first and last columns. Grid projection from
   interior is the primary recovery method.
3. **The grid is regular within a page** — even when detection finds
   irregular widths, the physical printing grid was uniform.
4. **Column count changes come in long runs** — years or decades at the
   same count, not page-to-page variation. This is what makes
   `LayoutDB.get_prior` useful: the prior really is informative.
5. **Sideways/rotated ads create apparent extra columns** — these are not
   part of the regular grid but appear as narrow irregular columns in
   detection.
6. **The clean edge is more reliable than the binding edge** — lower
   noise, no shadow, no sliver. Anchor from the clean side; dead-reckon
   to the binding side.
7. **Multi-column photos are harder than multi-column ads** — borderless
   sustained darkness, no contour for ad detection to grab.
8. **Ad density grows ~5×** from 1890s to 1970s. Detection tuning that
   works on sparse early issues may need different parameters on dense
   modern ones — Tier 1 was specifically the *opposite* problem (early
   issues too faint), but Tier 1's parameter tuning is decoupled per-page
   via `page_profile`.

---

## Update history

This file evolves issue-by-issue. When a new pattern, era boundary, or
scan condition is observed, append it here.

- **2026-05-16 — Post-1980 visual characterisation (Phase 0).**
  Rendered and reviewed 10 issues across 1985/1990/1995/2000/2007.
  Established that the classical column-grid paradigm does not
  apply to any post-1980 page in the sample — these are modular
  broadsheets with article-as-rectangle layout, multi-column
  headlines local to each article, integrated photos, banner pull
  quotes, and dedicated classified pages. Full per-era observations,
  patterns, comparison to classical, and implications for cutter
  redesign are in
  [`post1980_layout_observations.md`](post1980_layout_observations.md).
  Updated the era table above to point readers at the new file
  instead of the previous (incorrect) "3–5 columns variable"
  shorthand. Source PDFs from 1985 are TCPDF-rewrapped A4 (lower
  fidelity); 1990–2007 are Adobe Paper Capture broadsheet
  (~19"×28", consistent fidelity).
- **2026-05-06 — Started 71-year corpus-cutting campaign (1862–1979).**
  Added `cut_corpus.py` supervisor — runs `archive.process_archive`
  one year at a time in a deterministic non-sequential order
  (seed=20260506), persisting state to
  `data/cut_corpus_state.json` and tee-logging both supervisor and
  worker output to `cut_corpus.log`. Year list = years ending in
  1/3/4/6/8/9 with zero `page_layouts` rows (55 years) + years
  with `0 < page_layouts < 20` (16 sample-experiment redos:
  1878, 1898, 1899, 1901, 1905, 1911, 1919, 1923, 1924, 1929,
  1941, 1943, 1945, 1959, 1965, 1974). Resume-safe: kill the
  supervisor and restart with no args to pick up at the next
  non-`done` year. When this campaign completes, the next big
  block of `page_layouts` rows is from this run.
- **2026-04-30 — Ingested 1946 (51 issues) and 1948 (52 issues).**
  Driven by the recurrence_lab spike (cross-year archetype testing
  needs adjacent years). All 103 issues processed cleanly via
  `archive.py --workers 4` with 0 failures. Distributions match the
  established 1940s pattern from 1947 — P1 always 7c, P2 mostly 6c
  with the 1937–1974 editorial template firing on the expected
  minority (~10% of P2s land at 5c via that template), interior pages
  predominantly 7c with the usual ad-heavy outliers dropping to 5–6c
  or losing a phantom edge column under v2c. No new failure modes
  surfaced.

  Era-prior aggregate impact: the 1940–1949 decade in
  `LayoutDB.era_patterns` jumps from ~370 samples (1947 only) to
  **1,246 samples** (1945-12-27 + 1946 + 1947 + 1948), making it the
  best-supported era after the 1980s in the corpus.

  Known liabilities from `project_ad_heavy_detection_pending.md`
  (1946-12-24 P8 dropped left phantom → 4c; a few 5c outliers on
  ad-heavy interior pages) recur as expected; not a regression. Not
  added to the canonical regression set — these were production runs,
  not refactor targets.
- **2026-04-27 — Added 1947-01-09 issue-wide notes on out-of-volume
  page edges.** P3/P5/P7 right and P8 left edges are partial strips
  of underlying pages, visible because the volume's pages slipped
  out of register during photography. Reference case for
  `validate_columns_v2`'s Rule B (low body ratio + text_area
  extension). P6 left as the protective contrast (low body ratio
  alone, inside text_area, kept).
- **2026-04-27 — Added 1947-01-09 p1 as the canonical anomaly-page
  reference (landscape scan placed full-width on a portrait PDF).**
  Documents the `pdf_image_rect` vs actual-content-band discrepancy
  that fools R3 detection, and the page-pitch adoption path in
  `place_standard` that recovers the correct 6-column layout. The
  aspect-ratio anomaly up-front check is noted as a wishlist
  refinement.
- **2026-04-26 — Comprehensive rewrite.** Brought forward to reflect
  changes since the original April 2026 notes:
  - Added 1898-10-07 and 1947-11-06 as canonical regression issues
    (alongside 1920-01-02 and 1937-01-14).
  - Reframed `binding_confirmed=False` mentions to note that
    page-number-based binding determination has resolved the
    underlying ambiguity.
  - Added a "Low contrast (1890s)" section under scan conditions
    documenting the Tier 1 ad-detection trigger (`contrast < 145`).
  - Added a "Heavy advertising (1970s)" section.
  - Noted that the page-2 editorial wide-column pattern is now
    implemented (`column_pipeline.place_page2_editorial` +
    `layout_templates` SQLite row).
- **(earlier)** — Original notes, April 2026 R&D sessions. Documented
  1878, 1897, 1905, 1920, 1937, 1943, 1952, 1974 issues.
