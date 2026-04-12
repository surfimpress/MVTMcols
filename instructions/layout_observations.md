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

## Key Patterns

1. **Interior column rules are always reliable** — the valley-spike-valley detection with row consistency works across all eras
2. **Outer edges are always problematic** — no vertical rules exist at the outer edges of the first and last columns
3. **The grid is always regular within a page** — even when detection finds irregular widths, the physical printing grid was uniform
4. **Column count changes come in long runs** — years or decades at the same count, not page-to-page variation
5. **Sideways/rotated ads create apparent extra columns** — these are not part of the regular grid but appear as narrow irregular columns in detection
6. **The clean edge (non-binding) is more reliable than the binding edge** — lower noise, no shadow, no sliver
