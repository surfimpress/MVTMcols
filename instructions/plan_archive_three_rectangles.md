# Plan: Three-Rectangle Page Parsing

## Context

The column extraction pipeline repeatedly fails at page edges because it conflates three distinct spatial layers. Margins, binding shadow, facing page slivers, and actual column rules are all analysed in the same pass, leading to false columns from shadow/bleed and clipped text from misplaced crop positions.

The user identified the correct model: every PDF page contains three nested rectangles. The processing must peel these inward — establishing each bounding box in PDF-page coordinates — before any column detection begins. Each bounding box is stored in SQLite per page, so coordinates are never recomputed or lost.

## The Three Rectangles (x-direction)

```
R1 (PDF page):      |  white  |  scanned image  |  white  |
R2 (scanned image): |  black binding  |  newspaper page  |  facing sliver + black  |
R3 (newspaper page):|  white margin  |  text columns  |  white margin  |
```

Each rectangle is a bounding box `(left, top, right, bottom)` in percentage of PDF page dimensions. R2 is inside R1. R3 is inside R2. Column rules are inside R3.

## Approach

### Step 1: Detect all three rectangles in `page_profile.py`

Add a `find_rectangles()` function that operates on the 150 DPI greyscale render. Returns bounding boxes for R1, R2, R3 as percentages of PDF page width/height.

**R1→R2 (white PDF margin to image):**
- Column-wise mean darkness across full width (middle 60% of rows)
- Walk inward from each edge: first column where smoothed darkness > noise floor + 3
- Signal: sharp step from pure white (0) to paper/shadow tone

**R2→R3 binding side (black shadow to newspaper page):**
- Determine binding side: which R2 edge is darker
- Walk inward from binding edge: find where gradient flattens to within paper baseline + 2σ
- Use Gaussian-smoothed profile (σ=10px at 150 DPI) to ride over noise

**R2→R3 facing-page side:**
- Walk inward from non-binding edge looking for dark-light-dark-light pattern
- The second light zone is the start of R3
- If no facing sliver present, transition is just shadow-then-paper

**R3 print margins (white margin to text columns):**
- Within R3, compute body darkness median from central 60%
- Threshold at paper_baseline + 0.3 × (body_median - paper_baseline)
- Scan inward from each R3 edge: first sustained run above threshold = text edge

**Output:** All bounding boxes stored as named fields:

```python
{
    "r1": {"left": 0.0, "right": 100.0, "top": 0.0, "bottom": 100.0},  # always full page
    "r2": {"left": 3.2, "right": 97.1, "top": 1.5, "bottom": 98.8},    # image within PDF
    "r3": {"left": 8.4, "right": 89.2, "top": 3.0, "bottom": 97.5},    # newspaper page
    "text_area": {"left": 10.1, "right": 87.8, "top": 5.0, "bottom": 95.0},  # columns live here
    "binding_side": "right",
}
```

All values in percentage of PDF page dimensions. These are the **sole coordinate reference** for all downstream steps.

### Step 2: Modify `find_columns.py` to accept a clip region

Add `clip_x_frac=(start, end)` parameter to `find_column_boundaries()`. When provided, the clip rectangle uses these fractions of page width instead of the grid system. The page_pct output maps local pixel positions back to **PDF page percentages** using the clip bounds:

```python
page_pct = clip_x_frac[0]*100 + (cx / img_w) * (clip_x_frac[1] - clip_x_frac[0]) * 100
```

This means boundary positions are always in the same coordinate space (% of PDF page width), regardless of what region was clipped.

~15 lines changed. Core detection algorithm untouched.

### Step 3: Modify `split_page.py` to use rectangle bounds

**In `_detect_consensus()`:**
- Extract `text_area.left` and `text_area.right` from the profile
- Pass as `clip_x_frac` to `find_column_boundaries` for each strip
- Remove the post-hoc percentage filter — boundaries are already within the text area
- After consensus: inject edge boundaries at `text_area.left` and `text_area.right` if no detected boundary is within 2% of those edges

**In `extract_columns()`:**
- Boundaries come in as PDF page percentages (thanks to Step 2)
- Convert directly to PDF points: `x0 = pw * boundary_pct / 100`
- No recomputation, no offset errors — the coordinate system is consistent from detection through to cropping
- Buffer is added to the crop but the boundary position itself is never adjusted

**In `_regularise_grid()`:**
- No changes needed — it operates on boundary positions and only interpolates missing ones. The positions are already in PDF page percentages.

### Step 4: Store bounding boxes in SQLite

Add columns to `page_layouts` table (or a new `page_geometry` table):

```sql
CREATE TABLE page_geometry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER, month INTEGER, day INTEGER, page INTEGER,
    r2_left REAL, r2_right REAL, r2_top REAL, r2_bottom REAL,
    r3_left REAL, r3_right REAL, r3_top REAL, r3_bottom REAL,
    text_left REAL, text_right REAL, text_top REAL, text_bottom REAL,
    binding_side TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Stored once per page alongside the layout results. Available for:
- QA review (visualise where each rectangle was detected)
- Intelligence layer (aggregate R3 bounds across an era)
- Downstream processing (column segmentation knows exact page geometry)

### Step 5: Testing and verification

1. Run on the 8 test pages from different eras (1865-2005)
2. For each page, dump the detected rectangles and verify:
   - R2 excludes PDF white margins
   - R3 excludes binding shadow and facing page sliver
   - text_area excludes print margins
   - Column boundaries fall within text_area
3. Extract columns and verify via tunnel URLs that no margin/shadow/bleed appears
4. Compare column widths — should be more regular since detection operates in a cleaner region

## Files to modify

| File | Change | Lines |
|------|--------|-------|
| `page_profile.py` | Add `find_rectangles()`, return bounding boxes | ~80 added |
| `find_columns.py` | Add `clip_x_frac` parameter, adjust page_pct mapping | ~15 changed |
| `split_page.py` | Use text_area bounds for clip, inject edge boundaries, simplify filtering | ~30 changed |
| `layout_intelligence.py` | Store geometry in SQLite | ~20 added |

## Key principle

Every spatial coordinate in the pipeline is a percentage of PDF page width/height. Bounding boxes are established once in `page_profile.py` and passed through unchanged. No function recomputes positions from pixels — it receives bounds and works within them. The coordinate chain is:

```
profile_page() → bounding boxes (% of PDF page)
    → find_column_boundaries(clip_x_frac) → boundary positions (% of PDF page)
        → extract_columns() → crop coordinates (PDF points from % × page_width)
```

One coordinate system. One source of truth. Stored in SQLite for every page.
