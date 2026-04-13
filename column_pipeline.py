"""
Decomposed column detection pipeline.

Each function takes (data, context) and returns data.
No side effects, no database queries, no mutation of upstream results.
The PageContext carries all knowledge needed for decisions.

Pipeline stages:
    detect_strips()       → raw boundaries per strip
    cluster_boundaries()  → merge nearby detections
    place_columns()       → final positions by page type

Page type strategies:
    place_standard()          → project from interior + clean edge
    place_page2_editorial()   → arithmetic: start + 2×(1.5p) + 4×p
"""

import numpy as np
from find_columns import find_column_boundaries


# ── Stage 1: Detection ───────────────────────────────────────────────────────

CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]

STRIP_WEIGHTS = {
    3: 0.5, 4: 0.8, 5: 1.0, 6: 1.0, 7: 0.8, 8: 0.5, 9: 0.3,
}


def detect_strips(pdf_path, ctx, dpi=450):
    """
    Run find_column_boundaries on each strip within the text area.

    Returns list of raw detections, each with position, confidence,
    strip number, and weight.
    """
    clip_x = (ctx.text_area_left / 100, ctx.text_area_right / 100)
    dark_thresh = max(60, int(ctx.column_darkness_threshold))
    std_thresh = int(ctx.row_std_threshold)

    all_detections = []
    for grid_y in CONSENSUS_ROWS:
        strip_weight = STRIP_WEIGHTS.get(grid_y, 0.5)
        strip_y_start = (grid_y - 1) * 10
        strip_y_end = grid_y * 10

        try:
            results = find_column_boundaries(
                pdf_path, x=1, y=grid_y, w=10, h=1,
                page_number=0, dpi=dpi,
                darkness_threshold=dark_thresh,
                clip_x_frac=clip_x,
            )
        except Exception:
            continue

        for r in results:
            # Skip boundaries inside ad exclusion zones
            in_ad = False
            for ax1, ax2, ay1, ay2 in ctx.ad_zones:
                if ax1 < r.page_pct < ax2 and ay1 < strip_y_end and ay2 > strip_y_start:
                    in_ad = True
                    break
            if in_ad:
                continue

            # Accept high/medium confidence, or low with good structure
            if r.confidence in ("high", "medium"):
                weight = strip_weight
            elif r.row_std < std_thresh or r.valley_depth > 40:
                weight = strip_weight * 0.5
            else:
                continue

            all_detections.append({
                "pct": r.page_pct,
                "confidence": r.confidence,
                "row_std": r.row_std,
                "valley_depth": r.valley_depth,
                "darkness": r.peak_darkness,
                "strip": grid_y,
                "weight": weight,
            })

    return all_detections


# ── Stage 2: Clustering ──────────────────────────────────────────────────────

def cluster_boundaries(detections, merge_distance=2.0):
    """
    Cluster nearby detections and compute weighted positions.

    Returns list of boundary dicts with position, confidence,
    strip count, weighted score, and drift.
    """
    if not detections:
        return []

    detections.sort(key=lambda d: d["pct"])
    clusters = [[detections[0]]]
    for d in detections[1:]:
        if d["pct"] - clusters[-1][-1]["pct"] < merge_distance:
            clusters[-1].append(d)
        else:
            clusters.append([d])

    boundaries = []
    for cluster in clusters:
        strips_hit = len(set(d["strip"] for d in cluster))
        weighted_score = sum(d["weight"] for d in cluster)
        total_w = sum(d["weight"] for d in cluster)

        if total_w > 0:
            wmean = sum(d["pct"] * d["weight"] for d in cluster) / total_w
        else:
            wmean = np.mean([d["pct"] for d in cluster])

        best = min(cluster, key=lambda d: d["row_std"])

        if strips_hit >= 2:
            drift = max(d["pct"] for d in cluster) - min(d["pct"] for d in cluster)
        else:
            drift = 0.0

        # Accept if reasonable support
        if weighted_score >= 1.5 or strips_hit >= 3:
            boundaries.append({
                "x_pct": round(float(wmean), 2),
                "confidence": best["confidence"],
                "row_std": best["row_std"],
                "valley_depth": best["valley_depth"],
                "peak_darkness": best["darkness"],
                "strips_hit": strips_hit,
                "weighted_score": round(weighted_score, 2),
                "drift": round(drift, 2),
            })

    return sorted(boundaries, key=lambda b: b["x_pct"])


# ── Stage 3: Place columns by page type ──────────────────────────────────────

def place_columns(boundaries, ctx):
    """
    Place final column positions based on page type and context.

    Dispatches to the appropriate strategy.
    """
    if ctx.is_page_2 and ctx.page_2_template:
        return place_page2_editorial(boundaries, ctx)
    else:
        return place_standard(boundaries, ctx)


def place_page2_editorial(boundaries, ctx):
    """
    Place columns for page 2 editorial layout.

    Arithmetic only: start from clean-side text_area edge,
    step by 1.5× pitch twice, then by pitch for remaining columns.
    No detection needed — the positions come from the context.

    The expected_boundaries in the context already contain
    the correct positions. We just use them directly.
    """
    return _boundaries_from_positions(ctx.expected_boundaries)


def place_standard(boundaries, ctx):
    """
    Place columns for a standard page.

    Strategy:
    1. Remove narrow boundaries (< 65% of pitch)
    2. Build clean-edge grid from text_area + pitch
    3. Score clean-edge grid against detected boundaries
    4. If good match (2+ hits): use clean-edge grid
    5. Otherwise: use best anchor from detected boundaries
    6. Constrain to expected column count
    """
    if not boundaries:
        # No detected boundaries — use expected positions from context
        return _boundaries_from_positions(ctx.expected_boundaries)

    # Remove narrow gaps (ad borders)
    positions = [b["x_pct"] for b in boundaries]
    if len(positions) >= 3:
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        median_w = float(np.median(widths))
        min_acceptable = median_w * 0.65
        boundaries = _merge_narrow(boundaries, min_acceptable)
        positions = [b["x_pct"] for b in boundaries]

    # Build clean-edge grid from context
    clean_grid = ctx.expected_boundaries

    # Score clean-edge grid against detected boundaries (clean side only)
    if ctx.clean_side == "left":
        clean_det = [p for p in positions if p < 55]
    else:
        clean_det = [p for p in positions if p > 45]

    clean_hits = 0
    for det in clean_det:
        if clean_grid:
            nearest = min(clean_grid, key=lambda g: abs(g - det))
            if abs(nearest - det) < 2.0:
                clean_hits += 1

    # Try anchor-based grids from detected boundaries
    best_anchor_grid = None
    best_anchor_score = -1

    for b in boundaries[:5]:
        anchor = b["x_pct"]
        grid = [anchor]
        x = anchor - ctx.pitch
        while x > 0:
            grid.append(round(x, 2))
            x -= ctx.pitch
        x = anchor + ctx.pitch
        while x < 100:
            grid.append(round(x, 2))
            x += ctx.pitch
        grid.sort()

        # Trim to expected column count
        if len(grid) > ctx.num_columns + 1:
            anchor_idx = min(range(len(grid)), key=lambda i: abs(grid[i] - anchor))
            start = max(0, anchor_idx - ctx.num_columns // 2)
            end = start + ctx.num_columns + 1
            if end > len(grid):
                end = len(grid)
                start = max(0, end - ctx.num_columns - 1)
            grid = grid[start:end]

        hits = sum(1 for p in positions
                   if min(abs(g - p) for g in grid) < 2.0)
        score = hits
        if score > best_anchor_score:
            best_anchor_score = score
            best_anchor_grid = grid

    # Prefer clean-edge if it has 2+ hits
    if clean_hits >= 2:
        return _boundaries_from_positions(clean_grid)
    elif best_anchor_grid and best_anchor_score >= 2:
        return _boundaries_from_positions(best_anchor_grid)
    else:
        # Fallback to expected
        return _boundaries_from_positions(ctx.expected_boundaries)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _boundaries_from_positions(positions):
    """Convert a list of x_pct positions to boundary dicts."""
    return [{
        "x_pct": round(p, 2),
        "peak_darkness": 0, "row_std": 0, "valley_depth": 0,
        "confidence": "placed",
        "strips_hit": 0, "weighted_score": 0, "drift": 0,
    } for p in positions if 0 < p < 100]


def _merge_narrow(boundaries, min_width):
    """Merge boundaries closer than min_width, keeping the stronger one."""
    if len(boundaries) < 2:
        return boundaries
    result = [boundaries[0]]
    for b in boundaries[1:]:
        if b["x_pct"] - result[-1]["x_pct"] < min_width:
            if b.get("weighted_score", 0) > result[-1].get("weighted_score", 0):
                result[-1] = b
        else:
            result.append(b)
    return result
