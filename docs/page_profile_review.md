# Working Notes: page_profile.py Review

## Physical reality of what we're detecting

A scanned newspaper page has this cross-section from left to right (recto example):

```
PDF margin | BINDING SHADOW | [SLIVER] | [GAP] | PAGE MARGIN | TEXT COLUMNS | PAGE MARGIN | EDGE SHADOW/ARTIFACTS | PDF margin
           ^R2                                   ^R3           ^text_area     text_area^    R3^                      R2^
```

Key physical facts:
- The **page margin** is the wide white area between the page edge and the first/last column of text
- In newspaper typesetting, the page margin is a consistent, measurable feature — it's a design element
- The margin is **paper white** — near zero darkness — because no ink was applied there
- Column gutters (gaps between columns) are also white but NARROW (1-3% of page)
- The page margin is WIDE (2-6% of page) — significantly wider than a column gutter
- Binding shadow is extremely dark (150-250) and has a characteristic shape
- The sliver (facing page content) sits between binding shadow and the inter-page gap

## The invariant hierarchy

```
R2_left <= R3_left <= text_area_left < text_area_right <= R3_right <= R2_right
```

This MUST always hold. Currently enforced only partially (text_area inside R3), but R3 inside R2 is assumed not verified.

## Bugs and issues found

### 1. `_find_trough` direction=-1 has confusing variable naming

```python
if direction > 0:
    search_start = edge_px
    search_end = min(edge_px + search_extent, center_px)
else:
    search_end = max(edge_px - search_extent, center_px)
    search_start = search_end
    search_end_orig = search_end
    search_end = edge_px
```

This is extremely confusing. `search_end` is reassigned 3 times. `search_end_orig` is set but never used. The final result is:
- `search_start = max(edge_px - search_extent, center_px)` 
- `search_end = edge_px`

Which means we search from the inner limit to the edge. But the variable naming is a mess and the `search_end_orig` is dead code.

### 2. `max_trough_val` is defined TWICE

Line 153: `max_trough_val = min(paper_baseline * 0.5, 15)`
Line 225: `max_trough_val = min(paper_baseline * 0.5, 15)` (identical duplicate)

### 3. `spike_thresh` is computed but never used

Line 151: `spike_thresh = max(content_level * 1.5, paper_baseline + 30, 60)`

This was for the old spike-hunting approach. Now `_find_trough` doesn't use it. But spike clamping uses `giant_thresh = content_floor * 2` (a different value). Dead code.

### 4. spike_clamping for-loop doesn't walk through spikes

```python
for x in range(r2_left_px, outer_left_limit):
    if smooth[x] >= giant_thresh:
        while x < outer_left_limit and smooth[x] >= giant_thresh * 0.5:
            x += 1
        left_spike_px = x
```

The inner `while` modifies `x`, but the outer `for` loop resets `x` on the next iteration. So the walk-through doesn't actually skip pixels. Also uses `giant_thresh * 0.5` which was the bug that caused the old spike walk to traverse content.

### 5. `max_trough_val = min(paper_baseline * 0.5, 15)` — hardcoded assumptions

`paper_baseline` is the 25th percentile of the central content darkness. For heritage scans this is typically 30-40, giving max_trough_val = 15 (capped). For modern scans it could be 80+, giving max_trough_val = 15 (still capped). 

The cap at 15 is arbitrary. For very dark or noisy scans, the margin white might be at 20-25, not near zero. The 15 cap would reject valid troughs.

For clean scans with paper_baseline near 0, the threshold becomes 0, which is too strict.

### 6. Content floor (Otsu) is computed on center_profile which may include spike contamination

The center region is 20-80% of R2. On pages with unusual layout (facing page sliver extending far), spikes could reach into this region. However, Otsu is relatively robust to this since it finds the optimal split.

### 7. The trough search extent is hardcoded at 20% of R2

`search_extent = int(r2_span * 0.20)`

For a typical page at ~95% R2 span, this is ~19% of page width. The margin trough typically sits within 5-15% of the edge. 20% is reasonable but could miss troughs on pages with very wide margins (early era) or catch false troughs in content on pages with narrow margins (modern era).

### 8. Wall finding walks to the wrong limit

In `_find_trough` with direction=-1, the R3 wall walk goes:
```python
for x in range(trough_px, edge_px + 1):
```
This walks from the trough minimum TOWARD the edge. It's looking for where values rise above wall_thresh — that's the inner wall of the margin. But for R3 we want the OUTER wall (toward the edge). 

Wait — for direction=-1 (right edge), the trough sits between the edge spike and the content. Walking from trough toward edge_px finds where the spike starts — that's R3 (the page boundary between spike and margin). Walking from trough toward center finds where content starts — that's text_area. This is correct conceptually.

### 9. No validation that R3 > R2 or that text_area is sensible

If the trough detection returns positions where r3_left_px < r2_left_px (shouldn't happen but could due to off-by-one), we'd violate the hierarchy. Should explicitly enforce.

### 10. The Otsu content_floor walk has the same gutter problem

Walking outward from centre, the first dip below content_floor catches a column gutter. This was observed and worked around by making content_floor a fallback only. But it means the content_floor walk is fundamentally unsuitable for text_area detection — it finds the first gutter, not the margin.

A better inside-out approach: instead of walking to the first sub-threshold point, look for a SUSTAINED drop (like the trough — a wide near-zero region, not a narrow gutter dip).

## What we know about the trough

The trough (page margin) has these properties:
1. **Width**: 2-6% of page width (20-75 pixels at 150 DPI). Column gutters are 1-2% (10-25px).
2. **Depth**: Near paper white — very low darkness values (0-10 on most pages)
3. **Shape**: U-shaped with relatively steep walls on both sides
4. **Location**: Always between the edge artifacts (shadow/spike) and the text content
5. **Uniqueness**: There's exactly one margin trough per side (if any)

## What would be more robust

### Approach: Find ALL near-zero regions, then pick the right one

Instead of starting from the edge and hoping to find the trough:
1. Scan the entire outer portion of the page for contiguous near-zero regions
2. A margin candidate must be: wide enough (> gutter width), deep enough (below threshold), in the outer 25%
3. Pick the one closest to the edge — that's the margin
4. Its walls give R3 (edge-side wall) and text_area (content-side wall)

This is more robust because:
- It doesn't depend on spike detection or walk order
- It naturally handles double spikes, no-spike edges, etc.
- The "width > gutter" filter distinguishes margins from gutters
- It works identically for both sides (no direction parameter needed)

### Minimum trough width from typesetting

A newspaper page margin is typically 10-15mm. At 150 DPI, that's 60-90 pixels. At the profile DPI, a margin should be at least 30 pixels wide (about 2.5% of page). Column gutters are 2-5mm (12-30 pixels, about 1% of page).

So: `min_trough_width = max(int(r2_span * 0.02), 10)` — about 2% of R2 span, minimum 10 pixels.

### Better wall_thresh

Rather than `min(paper_baseline * 0.5, 15)`, use the Otsu content_floor. The trough is below Otsu, the content is above it. The walls are where the profile crosses the Otsu threshold. This is adaptive and well-founded.

Wait — but Otsu on the central region won't have margin pixels in it (they're outside the 20-80% zone). So Otsu separates column gutters from content, not margin from content. That's actually still useful — the margin is definitely below any content_floor.

A better threshold for "is this a trough": the values must be near digital white. A simple absolute threshold like < 15 (or < paper_baseline * 0.3) for the minimum, combined with the width check, would be robust.

## Summary of issues to fix

1. Clean up `_find_trough` variable naming and dead code
2. Remove duplicate `max_trough_val` definition
3. Remove unused `spike_thresh` 
4. Fix spike clamping loop (for/while interaction)
5. Add minimum trough width requirement to distinguish from gutters
6. Enforce full hierarchy: R2 ≤ R3 ≤ text_area
7. Consider using contiguous-region-finding instead of directional walk
8. Make the threshold for "near-zero" adaptive but bounded
