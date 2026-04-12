# Layout Observations

Field notes from processing pages across the Almonte Gazette's 145-year run. Documents the variety of layouts, scan conditions, and edge cases encountered.

## Column Counts by Era

| Era | Typical columns | Notes |
|-----|----------------|-------|
| 1860s-1870s | 5 | Wider columns, dense classified ads. 1878-12-27 page 3 showed what appeared to be 8 columns but one was a sideways ad creating an irregular narrow column alongside 7 regular ones. |
| 1880s | 5 | Similar to 1870s |
| 1900s | 7 | Transition to narrower columns. 1905-12-29 page 3 was the cleanest detection — all 7 columns at exactly 11% width. |
| 1910s-1920s | 7 | Consistent 7-column grid at ~10-11% per column. The 1920-01-02 issue was our primary test case. |
| 1940s-1950s | 7 | Same grid persists. 1952-12-25 pages showed some irregularity from mixed ad content. |
| 1960s | 6 | Transition to fewer, wider columns beginning |
| 1980s | 3-5 | Modern broadsheet layout, variable column widths |
| 2000s | 3-4 | Modern layout with display ads and photos |

## Pages Processed

### 1878-12-27 page 3 (recto)
- **Layout:** 7 regular columns + 1 irregular (sideways ad)
- **Interior columns:** 10.4-10.9% — very regular
- **Binding:** Left (recto), darkness did NOT confirm (binding_confirmed=False)
- **Notable:** One column contained a sideways/rotated advertisement creating a narrower column. The regular grid underneath was 7 columns.
- **Clean side confidence:** 0.666
- **Column rules:** Clear, well-detected interior rules

### 1878-12-27 page 4 (verso)
- **Layout:** 7 regular columns at 10%
- **Interior columns:** 9.9-10.1% — exceptionally regular
- **Binding:** Right (verso), confirmed by darkness
- **Notable:** Best regularity seen in the 1870s. All interior columns within 0.2% of each other.
- **Clean side confidence:** 0.319 (surprisingly low for a clean page)

### 1905-12-29 page 3 (recto)
- **Layout:** 7 columns
- **Interior columns:** 10.6-11.3% — good regularity
- **Binding:** Right (darkness said right, but recto should be left — scan orientation may differ)
- **Notable:** Cleanest overall detection across all test pages

### 1920-01-02 pages 1-8
- **Layout:** 7 columns throughout
- **Interior columns:** 10.3-11.4% typical
- **Binding:** Varies. binding_confirmed=False on most pages — the darkness comparison frequently disagreed with the page number. This may indicate the scan orientation isn't standard for this volume.
- **Page 2:** Has "His Master's Voice" display ad spanning lower half of page. Interior column rules still detected well above the ad. This is the page where we refined the grid projection and dead-reckoning approaches.
- **Page 1 (front page):** Masthead area complicated detection in upper strips
- **Pages 5, 8:** Binding shadow on right side. Column-spanning elements (headlines, ads) caused missed boundaries in some strips.

### 1943-12-30 page 8
- **Layout:** Advertising page — irregular, no clean column grid
- **Notable:** First test page. Dense ads with thick borders confused detection. Not representative of editorial pages.

### 1952-12-25 page 3 (recto)
- **Layout:** 7 columns expected
- **Interior columns:** 11.5-12.1% for confirmed interior
- **Binding:** Left (recto), confirmed by darkness
- **Notable:** Right side (clean) had narrow columns 6 and 7 (7.4%, 5.9%) suggesting the grid projection didn't extend far enough or text_area was too conservative on the clean side.

### 1952-12-25 page 4 (verso)
- **Layout:** Mixed content, irregular
- **Interior columns:** Wide variation (5-15%)
- **Binding:** Right (verso), confirmed
- **Clean side confidence:** 0.197 — very low, suggesting damage or unusual content on the left edge
- **Notable:** Most irregular column widths of any page tested. Likely heavy ad content disrupting the grid.

## Scan Conditions

### Binding shadow
- Present on all bound-volume scans
- Severity varies widely even within the same issue
- The binding_confirmed flag (darkness agrees with page number) is True on some pages and False on others in the same issue — this suggests either the scan orientation varies or the shadow is too weak to reliably detect by darkness alone

### Facing page sliver
- Appears on the binding side (gutter edge)
- Visible on some pages, absent on others
- Creates a second peak in the darkness profile beyond the last column
- Must not be mistaken for an additional column

### Page damage
- Tears, stains, and foxing visible on older pages
- Affects the darkness profile and can shift the text_area detection
- Manifests as reduced confidence scores on the affected edge

### Column-spanning elements
- Display ads (e.g. His Master's Voice) span 2-4 columns
- Headlines occasionally span 2 columns
- These break the vertical rule at the spanning height
- The multi-strip consensus handles most cases but occasionally votes down a real boundary

### 1937-01-14 (full issue, 8 pages, 7 columns at 11.9% pitch)

Grounding pages: 4 (CV=0.013) and 1 (CV=0.027). Pitch established from these two.

- **Page 1** (recto): Excellent. Clean 7-column grid.
- **Page 2** (verso): Good grid but has a **local layout variation** — the left two columns are at 1.5× standard width (deliberate editorial choice, wider editorial column). This is a layout intelligence item: some pages use wider editorial columns. Need to handle per-page column width variations within the overall grid.
- **Page 3** (recto): Very poor detection — every column wrong. Large display ad disrupts the grid completely. The anchored transposition placed columns at regular intervals but the anchor point was wrong, shifting the entire grid. This is why p3 should NOT be the default grounding page.
- **Page 4** (verso): Excellent. One of the two grounding pages. Near-perfect regularity.
- **Page 5** (recto): Workable grid but needs margin of error. A giant display ad occupies the lower half of the page with its own column grid (different pitch from the editorial grid). The detection picks up the ad's column structure which distorts results in the lower strips. The editorial grid in the upper half is correct.
- **Page 6** (verso): Excellent.
- **Page 7** (recto): Good on the left, poor on the right. A **photograph spanning three columns** in the top right disrupts detection. The photo obliterates column rules in that zone across the upper strips, and the consensus loses those boundaries. Multi-column photos are a distinct challenge from multi-column ads — they have no borders, just sustained mid-tone darkness.
- **Page 8** (verso): Good with minor inaccuracy over ads. The editorial grid is correct but where ads sit, the column boundaries drift slightly. Acceptable for extraction — the text content is captured.

### 1897-07-09 (full issue, 8 pages, 8 columns at 10.3% pitch)

Grounding pages: 8 (CV=0.022) and 2 (CV=0.030). Pass 1 showed a recto/verso split: recto pages detected 7 columns, verso pages detected 8. The pitch establishment correctly chose 8 columns from the verso pages. All recto pages re-processed to 8 columns via anchored transposition.

### 1974-05-23 (10 pages, heavy ads — 53 display ads detected)

First issue from the 1970s. Much denser advertising than earlier decades.

- **P3, P9** (recto): 7-8 columns detected at ~11% pitch — clean editorial pages, good detection
- **P5, P10**: Detection failed — likely all-ad pages with no clear editorial column grid
- **P7**: 5 columns with some at 21% — heavy ad content disrupting grid
- **Verso pages**: Anchored transposition produced 18% columns (wrong pitch) because pitch establishment selected 5 columns instead of 7
- **Key finding**: 53 ads extracted — the 1970s have 5× the ad density of 1890s issues. Ad detection is working well but the pitch establishment needs to handle the case where recto editorial pages clearly show 7 columns while the orchestrator picked 5.
- **Likely column count**: 7 at ~11%, same as earlier decades. Pages 5 and 10 may be special advertising supplement pages with a different grid.

## Key Patterns

### Page 2 editorial layout evolution

A recurring layout pattern on page 2:
- **1937-1952 (pure form):** Two wide editorial columns (each 1.5× standard pitch) on the left, followed by 4 regular columns. Total = 7 grid widths.
- **1965 (modified form):** Same wide editorial columns at the TOP of the page. Below that, the layout shifts to pairs of standard-width ads with a single text column tucked to the right. The underlying grid pitch remains the same throughout.
- **Not present in 1929** — the transition to this layout happened between 1929 and 1937.
- **Post-1965:** "Jigsaw" layouts emerge where different vertical zones use different column arrangements, all on the same underlying grid. The editorial wide-column tradition persists at the top of page 2 but the lower portion becomes increasingly ad-driven.

This signals the broader transition from rigid column grids to zone-based layouts, which accelerates through the 1970s-2000s.

## Key Patterns

1. **Interior column rules are always reliable** — the valley-spike-valley detection with row consistency works across all eras
2. **Outer edges are always problematic** — no vertical rules exist at the outer edges of the first and last columns
3. **The grid is always regular within a page** — even when detection finds irregular widths, the physical printing grid was uniform
4. **Column count changes come in long runs** — years or decades at the same count, not page-to-page variation
5. **Sideways/rotated ads create apparent extra columns** — these are not part of the regular grid but appear as narrow irregular columns in detection
6. **The clean edge (non-binding) is more reliable than the binding edge** — lower noise, no shadow, no sliver
