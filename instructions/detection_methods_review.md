# Detection Methods Review

A catalogue of every boundary detection strategy tried in the Almonte Gazette column splitting pipeline, with an assessment of effectiveness and suitability for production use.

## 1. PDF White Margin Detection

**File:** `page_profile.py`, `find_rectangles()`

**What:** Finds R1→R2 transition — where the digital PDF margin ends and the scanned image begins.

**Signal:** PDF margins are digitally white (inverted value < 2). Paper tone is always > 5. This is the cleanest signal in the entire pipeline.

**Effectiveness:** Excellent. Works on every page tested (1865–2005). The threshold of 2.0 on inverted values is robust because it exploits a digital artifact (PDF canvas white) rather than a scan characteristic.

**Production suitability:** Keep as-is. No changes needed.

---

## 2. Binding Side Detection (Darkness Comparison)

**File:** `page_profile.py`, `find_rectangles()`

**What:** Determines which edge has the binding shadow by comparing mean darkness of left vs right edge strips.

**Signal:** The binding side is always darker due to shadow from the binding curvature.

**Effectiveness:** Unreliable. Fails when shadow is weak, when damage obscures one edge, or when a large dark ad sits near one edge. On 1920 page 2, binding was reported as "left" when the page is page 2 (verso = binding on right).

**What it lacks:** Does not use the page number, which deterministically tells you recto (odd = binding left) vs verso (even = binding right). The darkness comparison is an unnecessary guess when the answer is known.

**Production suitability:** Replace entirely with page-number-based recto/verso determination. The darkness profile can serve as a confirmation signal but should never override the page number.

---

## 3. Shadow Threshold (paper_baseline + 2.5σ)

**File:** `page_profile.py`, `find_rectangles()`

**What:** Sets the darkness level above which a pixel column is considered "shadow" rather than paper.

**Signal:** Statistical — based on the paper baseline and its variability in the page interior.

**Effectiveness:** Mixed. Works on clean scans with strong shadow. Fails on pages where the shadow is gradual (the threshold sits in the middle of the gradient rather than at the edge) or where paper noise is high (pushes threshold too high).

**What it lacks:** No concept of gradient shape. A proper shadow detector would look at the rate of change (derivative) not just an absolute threshold.

**Production suitability:** Useful as one input, but should not be the sole determinant of where shadow ends. The dead-reckoning approach (project from clean edge) eliminates the need to precisely detect the shadow boundary.

---

## 4. Text Area Edge Detection (Peak → Minimum → Rise)

**File:** `page_profile.py`, `_find_text_edge()`

**What:** Finds where the newspaper's print margin ends and text columns begin, using a heavily smoothed (σ=15) darkness profile.

**Signal:** The pattern: shadow peak → margin minimum → column content rise. The first local minimum after the shadow peak is the print margin.

**Effectiveness:** Works well on the non-binding edge where the pattern is clean. Struggles on the binding edge where the shadow gradient is longer and may merge with the margin. The 0.2 threshold factor (20% of the way from margin minimum to body median) catches the rising edge but is sensitive to the minimum's position.

**Confidence scoring:** Added in latest iteration. Combines margin depth (40%), peak clarity (30%), and transition sharpness (30%). Produces meaningful differentiation: clean edges score 0.7+, noisy edges score 0.2-0.4.

**What it lacks:** No awareness of recto/verso — applies the same strategy to both sides when the binding side is fundamentally different from the clean side. On the binding side, the minimum may be contaminated by shadow gradient.

**Production suitability:** Good for the clean (non-binding) edge. Should not be used for the binding edge in isolation — the dead-reckoning approach is more reliable there.

---

## 5. Column Rule Detection (Valley-Spike-Valley + Row Consistency)

**File:** `find_columns.py`, `find_column_boundaries()`

**What:** Detects vertical column rules by finding darkness peaks with whitespace on both sides and consistent vertical profile.

**Signal:** Two complementary signals:
- **Valley-spike-valley:** The column rule is a dark spike flanked by lighter gutters (whitespace). Measured as `peak - mean(flanks)`.
- **Row-by-row consistency:** A real printed rule has the same darkness at every row (low std deviation). Text has varying darkness row by row (high std).

**Effectiveness:** The best strategy for interior column rules. Works across all eras tested. The valley-spike-valley pattern is physically grounded — it exploits the actual structure of printed column rules flanked by gutters.

**What it lacks:** Cannot detect the outer edges of the first and last columns (no rule exists there). Sensitive to the darkness threshold parameter — too high misses faint rules, too low picks up text noise. The valley measurement fails when one flank is against a dark ad rather than white gutter.

**Production suitability:** Keep as the core interior boundary detector. Should be used within a pre-clipped region (text area only) rather than across the full page width.

---

## 6. Multi-Strip Consensus

**File:** `split_page.py`, `_detect_consensus()`

**What:** Runs column detection on 7 horizontal strips across the page height and keeps boundaries that appear consistently.

**Signal:** True column rules run the full height of the page. Ad borders, box outlines, and text edges only appear in some strips. Consistency across strips discriminates rules from noise.

**Effectiveness:** Good at filtering false positives from ad borders. The strip weighting (middle strips weighted higher) helps prioritise body-text regions over ad-heavy top/bottom zones.

**What it lacks:** Counterproductive for column-spanning elements (headlines, display ads). A rule that vanishes where a 2-column headline spans gets voted down. The current acceptance threshold (weighted_score ≥ 1.5 or strips_hit ≥ 3) doesn't account for why a boundary might be absent in some strips.

**Production suitability:** Keep for interior boundary detection, but add awareness of column-spanning elements. A boundary that appears in most strips but disappears in 1-2 should be investigated for spanning content, not discarded.

---

## 7. Grid Projection from Interior Columns

**File:** `split_page.py`, `_project_grid_edges()`

**What:** Uses each interior column's width to predict where all boundaries would fall if the grid were perfectly regular. Scores predictions by how well they match detected interior boundaries. Aggregates to determine outer edges.

**Signal:** The regularity of the printing press's column grid. If interior columns are 10.4% wide, the outer columns are also 10.4% wide.

**Effectiveness:** Excellent for predicting outer edge positions. Interior columns are the most reliably detected boundaries, and projecting their width outward gives precise edge predictions. The confidence scoring (based on interior match quality) correctly identifies which interior columns are most trustworthy.

**What it lacks:** Currently projects both directions equally, but the clean edge and the binding edge have very different reliability. Should project primarily FROM the clean edge TOWARD the binding edge. Also, the 1% buffer and 3% clamp values were tuned by trial and error.

**Production suitability:** The best strategy for outer edges. Should be combined with recto/verso knowledge to anchor from the clean side and dead-reckon toward the binding side.

---

## 8. Grid Regularisation (Gap Interpolation)

**File:** `split_page.py`, `_regularise_grid()` (currently disabled)

**What:** Finds the dominant column pitch and interpolates missing boundaries where gaps exceed 1.4× the pitch.

**Signal:** Column widths cluster around a single value. A gap wider than 1.4× that value contains a missed boundary.

**Effectiveness:** Mixed. The pitch detection (mean of narrowest widths) works well when the narrow widths genuinely represent single columns. Fails when the pitch calculation is polluted by wide columns (the mean gets pulled up, making genuine single-column gaps look normal).

**What it lacks:** Cannot distinguish between "a column rule was missed here" and "this is a legitimate 2-column spanning element." The 1.4× threshold was chosen arbitrarily.

**Production suitability:** Useful as a QA check after grid projection, not as a primary detection method. If projection predicts N columns but detection found N-1, regularisation can fill the gap. But it should be informed by the grid projection's confidence, not run independently.

---

## 9. Narrow Column Merging

**File:** `split_page.py`, `_remove_narrow_columns()`

**What:** Merges boundaries that are closer than 7% of page width, keeping the stronger one.

**Effectiveness:** Good at removing false duplicates (ad borders near a real column rule). The 7% threshold works for the Gazette's typical 10-14% column widths.

**What it lacks:** The threshold should be relative to the expected column width, not hardcoded. A newspaper with 8% columns would have legitimate boundaries closer than 7%.

**Production suitability:** Keep but make the threshold adaptive (e.g., 0.5× the median detected column width).

---

## 10. Best Grid Selection (Combinatorial)

**File:** `split_page.py`, `_select_best_grid()`

**What:** When more boundaries are detected than the maximum (8), tries all C(N, max) combinations and selects the most regular subset by CV (coefficient of variation) of column widths.

**Effectiveness:** Works mathematically but is computationally expensive for large candidate sets (capped at 15 candidates). The CV scoring correctly identifies regular grids.

**What it lacks:** Treats all boundaries equally when some have much higher confidence than others. A high-confidence boundary should never be dropped in favour of a more "regular" but lower-confidence alternative.

**Production suitability:** Useful as a last resort when too many boundaries are detected, but should weight boundary confidence into the scoring, not just regularity.

---

## Summary: Strategies for Production

| Strategy | Verdict | Role |
|----------|---------|------|
| PDF white margin | **Keep** | R1→R2 detection, always reliable |
| Binding side from page number | **New — implement** | Deterministic recto/verso, replaces darkness guessing |
| Column rule detection (valley-spike-valley) | **Keep** | Core interior boundary detector |
| Multi-strip consensus | **Keep with refinement** | Filter ad borders, needs spanning-element awareness |
| Grid projection from interior | **Keep — primary outer edge method** | Best approach for first/last column edges |
| Text area edge detection | **Keep for clean edge only** | Good on non-binding side, unreliable on binding side |
| Confidence scoring | **Keep and expand** | Attach to every detection for downstream use |
| Shadow threshold | **Demote** | Confirmation signal only, not primary detector |
| Grid regularisation | **Demote to QA** | Post-processing check, not primary detection |
| Narrow merging | **Keep, make adaptive** | Cleanup step |

## Key Architectural Insight

The clean edge (non-binding side) is highly reliable and should be the anchor point. From there, the interior column rules establish the grid pitch. The binding edge should be determined by dead reckoning from the clean edge using the established pitch — not by trying to detect it through shadow, noise, and facing-page bleed.
