"""
Split a gazette PDF page into individual column images.

Stage 1 of the factory pipeline. Takes a single-page PDF, detects
column boundaries, extracts each column as a PNG, and logs results
(including quality flags) to SQLite.

Designed to handle the full range of the Almonte Gazette (1861–2007):
- Variable column counts (4–8 columns across the run)
- Binding shadow at gutter edge
- Skewed or warped scans
- Damaged or missing content
- Full-page ads with no column rules

Usage:
    python split_page.py <page.pdf> [--output-dir DIR] [--dpi 450] [--db PATH]

    from split_page import split_page
    result = split_page("1920-01-02-03.pdf", output_dir="output/", dpi=450)
"""

import os
import sys
import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import fitz
import numpy as np

from find_columns import find_column_boundaries, ColumnBoundary, _open_clean
from page_profile import profile_page


# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_DPI = 450
BUFFER_VW = 1.0  # 1% of page width added each side of column crop

# Grid rows for multi-strip consensus (1-indexed, 10% blocks).
# Skip row 1 (masthead) and row 10 (bottom margin).
CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]

# A boundary must appear in this fraction of strips to be accepted
CONSENSUS_MIN_FRAC = 0.4  # 40% — appears in at least 3 of 7 strips

# Boundaries within this % of page width are considered the same position
CONSENSUS_MERGE_PCT = 2.0


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class ColumnResult:
    index: int           # 0-indexed column number
    left_vw: float       # left boundary as % of page width
    right_vw: float      # right boundary as % of page width
    width_vw: float      # column width as % of page width
    peak_darkness: float  # strength of boundary signal
    confidence: str       # high/medium/low
    image_path: str       # path to extracted PNG


@dataclass
class PageResult:
    pdf_path: str
    page_number: int
    dpi: int
    page_width_px: int
    page_height_px: int
    num_columns: int
    columns: list         # list of ColumnResult
    detection_row: object  # which grid row(s) were used for detection
    quality_flags: list    # list of quality warning strings
    error: str            # None if successful
    elapsed_seconds: float


# ── Core functions ───────────────────────────────────────────────────────────

def _detect_consensus(pdf_path, page_number, dpi, page_prof=None,
                      expected_columns=None, ad_exclusion_zones=None):
    """
    Multi-strip consensus column detection.

    Runs find_column_boundaries on every strip in CONSENSUS_ROWS,
    collects all detected boundaries, and keeps only those that
    appear consistently across strips. True column rules run the
    full page height; ad borders and text edges don't.

    Uses the page profile for adaptive thresholds when available.

    Returns (boundaries_as_dicts, strips_used, quality_flags).
    """
    # Adaptive threshold from profile — but enforce a minimum of 60
    # to avoid drowning in paper noise on faint scans.
    if page_prof:
        dark_thresh = max(60, int(page_prof["column_darkness_threshold"]))
        std_thresh = int(page_prof["row_std_threshold"])
    else:
        dark_thresh = 60
        std_thresh = 45

    # ── Strip weighting ────────────────────────────────────────────
    # Middle strips are most reliable for grid detection (body text,
    # fewer ads). Edge strips help measure skew but are noisy.
    STRIP_WEIGHTS = {
        3: 0.5,   # upper — ads, mastheads
        4: 0.8,   # upper-mid
        5: 1.0,   # mid — best body text
        6: 1.0,   # mid — best body text
        7: 0.8,   # lower-mid
        8: 0.5,   # lower — ads, footers
        9: 0.3,   # bottom — margin noise
    }

    # Text area bounds from profile — this is where column rules live.
    # Detection is clipped to this region so no margin/shadow/bleed
    # pixels ever enter the analysis.
    if page_prof and "text_area" in page_prof:
        text_left_frac = page_prof["text_area"]["left"] / 100
        text_right_frac = page_prof["text_area"]["right"] / 100
    elif page_prof:
        text_left_frac = page_prof["content_x_start_frac"]
        text_right_frac = page_prof["content_x_end_frac"]
    else:
        text_left_frac, text_right_frac = 0.05, 0.95

    clip_x = (text_left_frac, text_right_frac)

    # Collect boundaries from every strip with their weights.
    # find_column_boundaries is clipped to the text area — all returned
    # page_pct values are already in PDF page percentage coordinates.
    all_positions = []

    for strip_idx, grid_y in enumerate(CONSENSUS_ROWS):
        strip_weight = STRIP_WEIGHTS.get(grid_y, 0.5)

        try:
            results = find_column_boundaries(
                pdf_path, x=1, y=grid_y, w=10, h=1,
                page_number=page_number, dpi=dpi,
                darkness_threshold=dark_thresh,
                clip_x_frac=clip_x,
            )
        except Exception:
            continue

        # Filter out boundaries that fall within ad exclusion zones.
        # Each strip covers a 10% vertical band: grid_y means y=(grid_y-1)*10% to grid_y*10%.
        strip_y_start = (grid_y - 1) * 10
        strip_y_end = grid_y * 10

        for r in results:
            # Check if this boundary's x position falls within an ad zone
            # that overlaps this strip's y range
            if ad_exclusion_zones:
                in_ad = False
                for ax1, ax2, ay1, ay2 in ad_exclusion_zones:
                    # Does the ad's y range overlap this strip?
                    y_overlap = (ay1 < strip_y_end and ay2 > strip_y_start)
                    # Does the boundary's x position fall within the ad?
                    x_inside = ax1 < r.page_pct < ax2
                    if y_overlap and x_inside:
                        in_ad = True
                        break
                if in_ad:
                    continue
            if r.confidence in ("high", "medium"):
                all_positions.append({
                    "pct": r.page_pct,
                    "confidence": r.confidence,
                    "row_std": r.row_std,
                    "valley_depth": r.valley_depth,
                    "darkness": r.peak_darkness,
                    "strip": grid_y,
                    "weight": strip_weight,
                })
            elif r.row_std < std_thresh or r.valley_depth > 40:
                all_positions.append({
                    "pct": r.page_pct,
                    "confidence": r.confidence,
                    "row_std": r.row_std,
                    "valley_depth": r.valley_depth,
                    "darkness": r.peak_darkness,
                    "strip": grid_y,
                    "weight": strip_weight * 0.5,  # downweight low-conf
                })

    if not all_positions:
        return [], CONSENSUS_ROWS, ["no_boundaries_detected"]

    # Cluster positions: group detections within CONSENSUS_MERGE_PCT
    all_positions.sort(key=lambda p: p["pct"])
    clusters = []
    current_cluster = [all_positions[0]]

    for pos in all_positions[1:]:
        if pos["pct"] - current_cluster[-1]["pct"] < CONSENSUS_MERGE_PCT:
            current_cluster.append(pos)
        else:
            clusters.append(current_cluster)
            current_cluster = [pos]
    clusters.append(current_cluster)

    # Score each cluster using position-weighted contributions.
    # Middle strips count more than edge strips. The weighted score
    # determines whether a boundary is real (column rule) or noise
    # (ad border that only appears in one region of the page).
    num_strips = len(CONSENSUS_ROWS)
    max_possible_weight = sum(STRIP_WEIGHTS.get(r, 0.5) for r in CONSENSUS_ROWS)

    boundaries = []
    for cluster in clusters:
        strips_hit = len(set(p["strip"] for p in cluster))
        weighted_score = sum(p.get("weight", 0.5) for p in cluster)

        # Use the detection with lowest row_std as representative
        best = min(cluster, key=lambda p: p["row_std"])

        # Weighted mean position (middle strips' positions count more)
        total_w = sum(p.get("weight", 0.5) for p in cluster)
        if total_w > 0:
            wmean_pct = sum(p["pct"] * p.get("weight", 0.5) for p in cluster) / total_w
        else:
            wmean_pct = np.mean([p["pct"] for p in cluster])

        # Measure drift: how much does the position vary across strips?
        # High drift = skew/warp. Store for downstream buffer calculation.
        if strips_hit >= 2:
            pct_values = [p["pct"] for p in cluster]
            drift = float(max(pct_values) - min(pct_values))
        else:
            drift = 0.0

        # Accept if weighted score exceeds threshold.
        # This replaces the raw strip-count check with a position-aware one.
        accept = (weighted_score >= 1.5) or (strips_hit >= 3)

        if accept:
            boundaries.append({
                "x_pct": round(float(wmean_pct), 2),
                "peak_darkness": best["darkness"],
                "row_std": best["row_std"],
                "valley_depth": best["valley_depth"],
                "confidence": best["confidence"],
                "strips_hit": strips_hit,
                "total_strips": num_strips,
                "consensus": round(weighted_score / max_possible_weight, 2),
                "weighted_score": round(weighted_score, 2),
                "drift": round(drift, 2),
            })

    # Sort by position
    boundaries.sort(key=lambda b: b["x_pct"])

    # Always merge boundaries that are too close together
    boundaries = _remove_narrow_columns(boundaries, min_width_pct=7.0)

    # Remove false boundaries from ad borders: any column narrower
    # than 65% of the median width is an ad border, not a real rule.
    # This MUST run before projection so the projection extends from
    # clean boundaries, not from false ad-border positions.
    if len(boundaries) >= 3:
        positions = [b["x_pct"] for b in boundaries]
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        median_width = float(np.median(widths))
        min_acceptable = median_width * 0.65
        boundaries = _remove_narrow_columns(boundaries, min_width_pct=min_acceptable)

    # ── Grid projection from interior columns ────────────────────────
    # Project each interior column's width outward to predict where
    # the outer edges should be.
    if len(boundaries) >= 4:
        clean_side = page_prof.get("clean_side", "right") if page_prof else "right"
        binding_side = page_prof.get("binding_side", "left") if page_prof else "left"
        r3 = page_prof.get("r3", {}) if page_prof else {}
        spine_pct = r3.get("left", 0) if binding_side == "left" else r3.get("right", 100)
        boundaries = _project_grid_edges(
            boundaries,
            text_left_pct=text_left_frac * 100,
            text_right_pct=text_right_frac * 100,
            clean_side=clean_side,
            spine_pct=spine_pct,
            binding_side=binding_side,
            expected_columns=expected_columns,
        )

    # ── Split wide columns (> 1.6x median = double-width ad/headline) ──
    if len(boundaries) >= 3:
        positions = [b["x_pct"] for b in boundaries]
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        median_width = float(np.median(widths))
        insertions = []
        for i in range(len(positions) - 1):
            gap = positions[i+1] - positions[i]
            if gap > median_width * 1.6:
                mid = (positions[i] + positions[i+1]) / 2
                insertions.append({
                    "x_pct": round(mid, 2),
                    "peak_darkness": 0, "row_std": 0, "valley_depth": 0,
                    "confidence": "interpolated",
                    "strips_hit": 0, "total_strips": 0,
                    "consensus": 0, "weighted_score": 0, "drift": 0,
                })
        if insertions:
            boundaries.extend(insertions)
            boundaries.sort(key=lambda b: b["x_pct"])

    # ── Final cap: 9 boundaries max (8 columns) ─────────────────────
    MAX_BOUNDARIES = 9
    if len(boundaries) > MAX_BOUNDARIES:
        scored = sorted(boundaries,
                       key=lambda b: b.get("weighted_score", 0)
                                   + (10 if b["confidence"] in ("high", "medium") else 0),
                       reverse=True)[:MAX_BOUNDARIES]
        boundaries = sorted(scored, key=lambda b: b["x_pct"])

    return boundaries, CONSENSUS_ROWS, _validate(boundaries)


def _project_grid_edges(boundaries, tolerance=2.0, text_left_pct=0,
                        text_right_pct=100, clean_side="right",
                        spine_pct=None, binding_side="left",
                        expected_columns=None):
    """
    Predict outer edges by projecting interior column widths outward.

    Uses interior columns (numbered 3,5,2,4,6 in priority order —
    centre-out) as the most trusted width references. Each column's
    width is replicated across the page to predict where all boundaries
    should fall. Predictions are scored by how well they match other
    detected boundaries.

    The outer edges are placed exactly one column width beyond the
    outermost detected interior boundary. No buffer — pure grid logic.

    Detected interior boundaries are never moved.
    """
    if len(boundaries) < 4:
        return boundaries

    positions = [b["x_pct"] for b in boundaries]
    n = len(positions)

    # Interior columns: the spaces between detected boundaries.
    # With N boundaries we have N-1 columns, indexed 1 to N-1.
    # Priority order: centre columns first (most reliable),
    # then work outward. For 7 columns: 3,5,2,4,6 (skip 1 and 7
    # which are the outermost and unreliable).
    num_cols = n - 1
    if num_cols < 3:
        return boundaries

    # Build priority order: centre-out, skip first and last
    centre = num_cols // 2  # 0-indexed centre column
    priority = []
    for offset in range(num_cols):
        for col_idx in [centre - offset, centre + offset]:
            if 1 <= col_idx <= num_cols - 2:  # skip first (0) and last (num_cols-1)
                if col_idx not in priority:
                    priority.append(col_idx)

    # Project each interior column and score its predictions
    predictions = []
    for col_idx in priority:
        col_left = positions[col_idx]
        col_right = positions[col_idx + 1]
        col_width = col_right - col_left

        if col_width < 3.0:
            continue

        # Project this width leftward and rightward
        predicted = []
        x = col_left
        while x > -10:
            predicted.append(x)
            x -= col_width
        x = col_right
        while x < 110:
            predicted.append(x)
            x += col_width
        predicted.sort()

        # Score: how many detected boundaries match a predicted position?
        matches = 0
        total_error = 0
        for actual in positions:
            nearest = min(predicted, key=lambda p: abs(p - actual))
            err = abs(nearest - actual)
            if err < tolerance:
                matches += 1
                total_error += err

        if matches < 2:
            continue

        avg_error = total_error / matches
        confidence = matches / len(positions) * (1.0 / (1.0 + avg_error))

        # Predicted outer edges: extend outward in steps of col_width
        # until reaching the page edge. Collect ALL predicted positions
        # beyond the current boundary range.
        all_pred_left = []
        x = positions[0] - col_width
        while x > 0:
            all_pred_left.append(round(x, 2))
            x -= col_width

        all_pred_right = []
        x = positions[-1] + col_width
        while x < 100:
            all_pred_right.append(round(x, 2))
            x += col_width

        pred_left = round(max(all_pred_left), 2) if all_pred_left else 0
        pred_right = round(min(all_pred_right), 2) if all_pred_right else 100

        predictions.append({
            "source_col": col_idx + 1,  # 1-indexed
            "col_width": round(col_width, 2),
            "matches": matches,
            "total_boundaries": len(positions),
            "avg_error": round(avg_error, 3),
            "confidence": round(confidence, 3),
            "pred_left": round(pred_left, 2),
            "pred_right": round(pred_right, 2),
        })

    if not predictions:
        return boundaries

    # For each predicted position beyond the detected range, aggregate
    # across all interior column predictions. A position that multiple
    # columns agree on is highly confident.
    def make_edge_boundary(pct, conf):
        return {
            "x_pct": pct,
            "peak_darkness": 0, "row_std": 0, "valley_depth": 0,
            "confidence": "projected",
            "strips_hit": 0, "total_strips": 0,
            "consensus": conf, "weighted_score": 0, "drift": 0,
            "projection_confidence": conf,
        }

    # Collect all left and right predictions from all interior columns
    all_left = []
    all_right = []
    for p in predictions:
        conf = p["confidence"]
        col_width = p["col_width"]
        # Extend leftward in steps
        x = positions[0] - col_width
        while x > 0:
            all_left.append((round(x, 2), conf))
            x -= col_width
        # Extend rightward in steps
        x = positions[-1] + col_width
        while x < 100:
            all_right.append((round(x, 2), conf))
            x += col_width

    # Cluster predictions within tolerance and take weighted mean
    def cluster_predictions(preds, merge_dist=2.0):
        if not preds:
            return []
        preds.sort(key=lambda p: p[0])
        clusters = [[preds[0]]]
        for p in preds[1:]:
            if p[0] - clusters[-1][-1][0] < merge_dist:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        result = []
        for cluster in clusters:
            total_w = sum(c for _, c in cluster)
            if total_w > 0:
                wmean = sum(v * c for v, c in cluster) / total_w
                avg_conf = total_w / len(cluster)
                result.append((round(wmean, 2), round(avg_conf, 3)))
        return result

    left_edges = cluster_predictions(all_left)
    right_edges = cluster_predictions(all_right)

    result = list(boundaries)

    # Spine constraint: on the binding side, projected edges must not
    # extend past the R3 boundary (the spine).
    left_limit = 0
    right_limit = 100
    if spine_pct is not None:
        if binding_side == "left":
            left_limit = max(0, spine_pct)
        else:
            right_limit = min(100, spine_pct)

    # When expected_columns is known (from issue prior), calculate
    # exactly how many boundaries to add on each side.
    # N columns = N+1 boundaries. We have len(boundaries) detected.
    # Need to add (expected_columns + 1 - len(boundaries)) total.
    if expected_columns is not None:
        target_boundaries = expected_columns + 1
        needed = target_boundaries - len(result)
        if needed <= 0:
            # Already have enough or too many — don't add more
            pass
        else:
            # Distribute needed boundaries: add from the side with
            # more room (further from the page edge)
            left_room = positions[0] - left_limit
            right_room = right_limit - positions[-1]

            # Add from the side with more room first
            added = 0
            left_sorted = sorted(left_edges, reverse=True)
            right_sorted = sorted(right_edges)
            li, ri = 0, 0

            while added < needed:
                can_left = li < len(left_sorted) and left_limit < left_sorted[li][0] < positions[0]
                can_right = ri < len(right_sorted) and positions[-1] < right_sorted[ri][0] < right_limit

                if can_left and can_right:
                    # Add from whichever side has more room
                    if left_room >= right_room:
                        result.insert(0, make_edge_boundary(*left_sorted[li]))
                        li += 1
                    else:
                        result.append(make_edge_boundary(*right_sorted[ri]))
                        ri += 1
                elif can_left:
                    result.insert(0, make_edge_boundary(*left_sorted[li]))
                    li += 1
                elif can_right:
                    result.append(make_edge_boundary(*right_sorted[ri]))
                    ri += 1
                else:
                    break
                added += 1
    else:
        # No expected count — add all valid projected edges
        for pct, conf in sorted(left_edges, reverse=True):
            if left_limit < pct < positions[0]:
                result.insert(0, make_edge_boundary(pct, conf))

        for pct, conf in sorted(right_edges):
            if positions[-1] < pct < right_limit:
                result.append(make_edge_boundary(pct, conf))

    # Store projection stats for diagnostics
    if result:
        result[0]["_projection_stats"] = predictions

    return result


def _remove_narrow_columns(boundaries, min_width_pct=7.0):
    """
    Merge boundaries that are too close together.

    Two rules within min_width_pct of each other can't form a real
    column — one is a false detection (ad border, text edge). Keep
    the one with higher weighted_score.
    """
    if len(boundaries) < 2:
        return boundaries

    boundaries = sorted(boundaries, key=lambda b: b["x_pct"])
    result = [boundaries[0]]

    for b in boundaries[1:]:
        if b["x_pct"] - result[-1]["x_pct"] < min_width_pct:
            # Too close — keep the stronger one
            if b.get("weighted_score", 0) > result[-1].get("weighted_score", 0):
                result[-1] = b
        else:
            result.append(b)

    return result



def _select_best_grid(boundaries, max_n):
    """
    From a set of candidate boundaries, select the subset of at most
    max_n that forms the most regular column grid.

    Boundaries are column RULES. N boundaries give N-1 columns (the
    spaces between rules). We score by how evenly spaced the inter-
    boundary gaps are — a perfect grid has equal gaps.

    Strategy: try all combinations and pick the most regular.
    With <=15 candidates and max_n=8, this is manageable.
    """
    from itertools import combinations

    if len(boundaries) <= max_n:
        return boundaries

    # Pre-filter if too many for brute force
    if len(boundaries) > 15:
        boundaries = sorted(boundaries,
                           key=lambda b: b.get("weighted_score", 0),
                           reverse=True)[:15]
        boundaries.sort(key=lambda b: b["x_pct"])

    best_score = float("inf")
    best_combo = None

    for combo in combinations(range(len(boundaries)), max_n):
        selected = [boundaries[i] for i in combo]
        positions = [b["x_pct"] for b in selected]

        # Compute column widths (gaps between adjacent boundaries)
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]

        if not widths:
            continue

        # Skip if any column is too narrow
        if min(widths) < 5.0:
            continue

        # Score: coefficient of variation (std/mean) of column widths
        # Lower = more regular. Using CV instead of raw std so we don't
        # penalise grids that are regular but have wider columns.
        mean_w = np.mean(widths)
        if mean_w > 0:
            score = float(np.std(widths) / mean_w)
        else:
            score = float("inf")

        if score < best_score:
            best_score = score
            best_combo = selected

    if best_combo is None:
        boundaries.sort(key=lambda b: b.get("weighted_score", 0), reverse=True)
        return sorted(boundaries[:max_n], key=lambda b: b["x_pct"])

    return best_combo


def _validate(boundaries):
    """
    Check boundaries for quality issues. Returns list of flag strings.

    Boundaries are column rules. N boundaries → N-1 columns.
    """
    flags = []

    if not boundaries:
        flags.append("no_boundaries_detected")
        return flags

    if len(boundaries) < 2:
        flags.append("insufficient_boundaries")
        return flags

    # Confidence distribution
    high = sum(1 for b in boundaries if b["confidence"] == "high")
    low = sum(1 for b in boundaries if b["confidence"] == "low")
    if high == 0:
        flags.append("no_high_confidence_boundaries")
    if low > high:
        flags.append("mostly_low_confidence")

    # Column count (N-1 columns from N boundaries)
    num_cols = len(boundaries) - 1
    if num_cols < 3:
        flags.append(f"few_columns_{num_cols}")

    # Column width regularity
    positions = sorted(b["x_pct"] for b in boundaries)
    widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
    if len(widths) >= 2:
        width_mean = np.mean(widths)
        if width_mean > 0 and np.std(widths) / width_mean > 0.3:
            flags.append("irregular_column_widths")

    return flags


def extract_columns(pdf_path, boundaries, page_number, dpi, output_dir,
                    buffer_vw=BUFFER_VW):
    """
    Extract each column as a PNG using the detected boundaries.

    Boundaries are the column RULES — columns are the spaces between
    adjacent rules. The first boundary is the left edge of column 1,
    and the last boundary is the right edge of the last column.
    Anything outside those is margin/binding/facing page bleed.

    With N boundaries you get N-1 columns.

    Returns list of ColumnResult.
    """
    if len(boundaries) < 2:
        return []

    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    columns = []
    col_num = 0

    for i in range(len(boundaries) - 1):
        left = boundaries[i]["x_pct"]
        right = boundaries[i + 1]["x_pct"]
        width = right - left

        # Skip very narrow gaps (< 3% of page width)
        if width < 3.0:
            continue

        col_num += 1

        # Adaptive buffer: use drift to widen overlap on skewed pages.
        # Higher drift means more binding curvature, so text may
        # extend further past the rule position.
        left_drift = boundaries[i].get("drift", 0)
        right_drift = boundaries[i + 1].get("drift", 0)
        max_drift = max(left_drift, right_drift)
        adaptive_buffer = buffer_vw + max_drift * 0.5

        crop_left = max(0, left - adaptive_buffer)
        crop_right = min(100, right + adaptive_buffer)

        # Convert to PDF points
        x0 = pw * crop_left / 100
        y0 = 0
        x1 = pw * crop_right / 100
        y1 = ph

        clip = fitz.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(clip=clip, dpi=dpi)

        # Save
        stem = Path(pdf_path).stem
        col_filename = f"{stem}_col{col_num}.png"
        col_path = os.path.join(output_dir, col_filename)
        pix.save(col_path)

        columns.append(ColumnResult(
            index=col_num - 1,
            left_vw=round(left, 2),
            right_vw=round(right, 2),
            width_vw=round(width, 2),
            peak_darkness=boundaries[i]["peak_darkness"],
            confidence=boundaries[i]["confidence"],
            image_path=col_path,
        ))

    doc.close()
    return columns


def split_page(pdf_path, page_number=0, dpi=DEFAULT_DPI, output_dir=None,
               db_path=None, expected_columns=None, prior_boundaries=None,
               prior_page_type=None, ad_exclusion_zones=None):
    """
    Full page-splitting pipeline.

    Args:
        pdf_path:         Path to single-page PDF.
        page_number:      Zero-indexed page within the PDF.
        dpi:              Render resolution for column images.
        output_dir:       Where to save column PNGs. Defaults to <stem>_columns/.
        db_path:          SQLite database to log results. Optional.
        expected_columns: If known (from issue prior), the expected column count.
        prior_boundaries: List of boundary x_pct values from a known-good page
                         in the same issue. Used as fallback when detection is poor.
        prior_page_type:  "recto" or "verso" — the page type of the prior.
                         Should match this page's type (use separate templates).

    Returns:
        PageResult with all columns and quality flags.
    """
    t0 = time.time()
    pdf_path = str(pdf_path)

    # Set up output directory
    if output_dir is None:
        stem = Path(pdf_path).stem
        output_dir = os.path.join(os.path.dirname(pdf_path) or ".", f"{stem}_columns")
    os.makedirs(output_dir, exist_ok=True)

    # Profile the page for adaptive thresholds
    try:
        page_prof = profile_page(pdf_path, page_number)
    except Exception:
        page_prof = None

    # Open and measure the page
    try:
        doc = _open_clean(pdf_path)
    except Exception as e:
        return PageResult(
            pdf_path=pdf_path, page_number=page_number, dpi=dpi,
            page_width_px=0, page_height_px=0, num_columns=0,
            columns=[], detection_row=0, quality_flags=[],
            error=f"pdf_open_failed: {e}", elapsed_seconds=time.time() - t0,
        )

    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    # Get full-page pixel dimensions for reference
    full_pix = page.get_pixmap(dpi=dpi)
    page_w_px = full_pix.w
    page_h_px = full_pix.h
    doc.close()

    # Multi-strip consensus detection with adaptive thresholds
    best_boundaries, used_rows, quality_flags = _detect_consensus(
        pdf_path, page_number, dpi, page_prof,
        expected_columns=expected_columns,
        ad_exclusion_zones=ad_exclusion_zones,
    )

    # Add profile quality flags
    if page_prof and page_prof.get("quality_flags"):
        quality_flags = list(set(quality_flags + page_prof["quality_flags"]))

    if not best_boundaries:
        return PageResult(
            pdf_path=pdf_path, page_number=page_number, dpi=dpi,
            page_width_px=page_w_px, page_height_px=page_h_px,
            num_columns=0, columns=[], detection_row=used_rows,
            quality_flags=quality_flags,
            error="no_column_boundaries_found",
            elapsed_seconds=time.time() - t0,
        )

    # ── Prior fallback ────────────────────────────────────────────
    # If detection produced an irregular grid and we have a prior
    # with known-good boundary positions, fall back to the prior.
    # Mirror the positions if the prior is from a different page type
    # (recto vs verso).
    if prior_boundaries and len(best_boundaries) >= 2:
        positions = [b["x_pct"] for b in best_boundaries]
        this_page_type = page_prof.get("page_type") if page_prof else None

        # Get the prior's pitch (median column width)
        prior_widths = [prior_boundaries[i+1] - prior_boundaries[i]
                       for i in range(len(prior_boundaries) - 1)]
        prior_pitch = float(np.median(prior_widths))
        prior_num_cols = len(prior_boundaries) - 1

        # Anchored transposition: build the grid from the clean-side
        # text_area boundary, placing columns in the appropriate direction.
        # Then also try each detected boundary as an anchor. Pick the
        # grid that best matches detected boundaries.
        #
        # Clean side approach: the text_area boundary on the non-binding
        # side is the most reliable position on the page. For verso pages
        # (binding right, clean left), start from text_area left and place
        # columns rightward. For recto (binding left, clean right), start
        # from text_area right and place columns leftward.

        clean_side = page_prof.get("clean_side") if page_prof else None
        ta = page_prof.get("text_area", {}) if page_prof else {}

        # Build candidate grids from multiple anchor strategies
        candidate_grids = []

        # Strategy 1: Start from clean-side text_area boundary
        if clean_side and ta:
            if clean_side == "left":
                # Verso: clean side is left, build rightward
                start = ta.get("left", 0)
                grid = [round(start + i * prior_pitch, 2)
                        for i in range(prior_num_cols + 1)]
                grid = [g for g in grid if 0 < g < 100]
                candidate_grids.append(("clean_edge", grid))
            else:
                # Recto: clean side is right, build leftward
                start = ta.get("right", 100)
                grid = [round(start - i * prior_pitch, 2)
                        for i in range(prior_num_cols + 1)]
                grid = sorted([g for g in grid if 0 < g < 100])
                candidate_grids.append(("clean_edge", grid))

        # Strategy 2: Anchor at each detected boundary (existing approach)
        conf_order = {"high": 0, "medium": 1, "low": 2, "projected": 3,
                      "interpolated": 4, "edge": 5, "prior": 6}
        anchors = sorted(best_boundaries,
                        key=lambda b: conf_order.get(b["confidence"], 9))

        for anchor_b in anchors[:5]:  # top 5 by confidence
            anchor = anchor_b["x_pct"]
            grid = [anchor]
            x = anchor - prior_pitch
            while x > 0:
                grid.append(round(x, 2))
                x -= prior_pitch
            x = anchor + prior_pitch
            while x < 100:
                grid.append(round(x, 2))
                x += prior_pitch
            grid.sort()

            # Trim to prior_num_cols + 1 boundaries
            if len(grid) > prior_num_cols + 1:
                anchor_idx = min(range(len(grid)),
                               key=lambda i: abs(grid[i] - anchor))
                start = max(0, anchor_idx - prior_num_cols // 2)
                end = start + prior_num_cols + 1
                if end > len(grid):
                    end = len(grid)
                    start = max(0, end - prior_num_cols - 1)
                grid = grid[start:end]

            candidate_grids.append(("anchor", grid))

        # Score each candidate grid.
        # For clean_edge grids, only score against clean-side detections
        # (binding-side detections are unreliable due to facing page sliver).
        # For anchor grids, score against all detections.
        page_center = 50.0
        if clean_side == "left":
            clean_positions = [p for p in positions if p < page_center + 10]
        elif clean_side == "right":
            clean_positions = [p for p in positions if p > page_center - 10]
        else:
            clean_positions = positions

        best_grid = None
        best_score = -1
        best_source = ""

        for source, grid in candidate_grids:
            if len(grid) < 3:
                continue

            # Clean-edge grids score only against clean-side detections
            score_against = clean_positions if source == "clean_edge" else positions

            hits = 0
            total_dev = 0
            for det in score_against:
                nearest = min(grid, key=lambda g: abs(g - det))
                dev = abs(nearest - det)
                if dev < 2.0:
                    hits += 1
                    total_dev += dev

            score = hits - (total_dev * 0.1)

            if source == "clean_edge" and hits >= 2:
                # Clean-edge with 2+ hits is highly trusted — record it
                # separately so we can prefer it
                if not hasattr(score, '__self__'):  # just tracking
                    pass

            if score > best_score:
                best_score = score
                best_grid = grid
                best_source = source

        # Prefer clean_edge if it has 2+ hits — it starts from the most
        # reliable position on the page. Only fall back to anchor if
        # clean_edge matched fewer than 2 detected boundaries.
        clean_edge_grid = None
        clean_edge_hits = 0
        for source, grid in candidate_grids:
            if source != "clean_edge":
                continue
            score_against = clean_positions
            hits = sum(1 for d in score_against
                      if min(abs(g - d) for g in grid) < 2.0)
            if hits >= 2:
                clean_edge_grid = grid
                clean_edge_hits = hits
                break

        if clean_edge_grid and clean_edge_hits >= 2:
            best_grid = clean_edge_grid
            best_source = "clean_edge"
            best_score = clean_edge_hits

        # Use the anchored grid if it's better than the raw detection.
        # "Better" = more regular (lower CV) or more matching boundaries.
        if best_grid and len(best_grid) >= 3:
            grid_widths = [best_grid[i+1] - best_grid[i]
                          for i in range(len(best_grid) - 1)]
            grid_cv = float(np.std(grid_widths) / np.mean(grid_widths))

            det_widths = [positions[i+1] - positions[i]
                         for i in range(len(positions) - 1)]
            det_cv = float(np.std(det_widths) / np.mean(det_widths)) if det_widths else 1.0

            # Use anchored grid if it's significantly more regular
            if grid_cv < det_cv * 0.7 or det_cv > 0.15:
                best_boundaries = [{
                    "x_pct": p,
                    "peak_darkness": 0, "row_std": 0, "valley_depth": 0,
                    "confidence": "anchored_prior",
                    "strips_hit": 0, "total_strips": 0,
                    "consensus": 0, "weighted_score": 0, "drift": 0,
                } for p in best_grid]

                quality_flags.append(
                    f"anchored_prior({best_source},hits={best_score:.1f},"
                    f"grid_cv={grid_cv:.3f},det_cv={det_cv:.3f})"
                )

    # Extract columns
    columns = extract_columns(
        pdf_path, best_boundaries, page_number, dpi, output_dir
    )

    elapsed = time.time() - t0

    result = PageResult(
        pdf_path=pdf_path, page_number=page_number, dpi=dpi,
        page_width_px=page_w_px, page_height_px=page_h_px,
        num_columns=len(columns), columns=columns,
        detection_row=used_rows, quality_flags=quality_flags,
        error=None, elapsed_seconds=round(elapsed, 2),
    )

    # Log to database if requested
    if db_path:
        _log_to_db(result, db_path)

    # Save metadata alongside the columns
    meta_path = os.path.join(output_dir, "page_meta.json")
    _save_metadata(result, meta_path)

    return result


# ── Database logging ─────────────────────────────────────────────────────────

def _log_to_db(result, db_path):
    """Log page-splitting results to the SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS page_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_path TEXT,
            page_number INTEGER,
            dpi INTEGER,
            page_width_px INTEGER,
            page_height_px INTEGER,
            num_columns INTEGER,
            detection_row TEXT,
            quality_flags TEXT,
            error TEXT,
            elapsed_seconds REAL,
            columns_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO page_splits
        (pdf_path, page_number, dpi, page_width_px, page_height_px,
         num_columns, detection_row, quality_flags, error,
         elapsed_seconds, columns_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result.pdf_path, result.page_number, result.dpi,
        result.page_width_px, result.page_height_px,
        result.num_columns,
        json.dumps(result.detection_row),
        json.dumps(result.quality_flags),
        result.error,
        result.elapsed_seconds,
        json.dumps([asdict(c) for c in result.columns]),
    ))
    conn.commit()
    conn.close()


def _save_metadata(result, path):
    """Save page result metadata as JSON."""
    data = {
        "pdf_path": result.pdf_path,
        "page_number": result.page_number,
        "dpi": result.dpi,
        "page_size_px": [result.page_width_px, result.page_height_px],
        "num_columns": result.num_columns,
        "detection_row": result.detection_row,
        "quality_flags": result.quality_flags,
        "error": result.error,
        "elapsed_seconds": result.elapsed_seconds,
        "columns": [asdict(c) for c in result.columns],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Pretty printing ─────────────────────────────────────────────────────────

def print_result(result):
    """Print a human-readable summary."""
    print(f"Page: {result.pdf_path} (page {result.page_number})")
    print(f"  Size: {result.page_width_px} x {result.page_height_px} px at {result.dpi} dpi")
    print(f"  Detection: multi-strip consensus across {len(result.detection_row)} strips")

    if result.error:
        print(f"  ERROR: {result.error}")

    if result.quality_flags:
        print(f"  Flags: {', '.join(result.quality_flags)}")
    else:
        print(f"  Quality: good")

    print(f"  Columns: {result.num_columns}")
    for col in result.columns:
        print(f"    [{col.index + 1}] {col.left_vw:.1f}%–{col.right_vw:.1f}% "
              f"(width {col.width_vw:.1f}%)  "
              f"confidence={col.confidence}  "
              f"→ {os.path.basename(col.image_path)}")

    print(f"  Elapsed: {result.elapsed_seconds:.1f}s")


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Split a gazette page into columns")
    parser.add_argument("pdf", help="Path to page PDF")
    parser.add_argument("--output-dir", "-o", help="Output directory for column PNGs")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--db", help="SQLite database path for logging results")
    parser.add_argument("--page", type=int, default=0, help="Page number (0-indexed)")
    args = parser.parse_args()

    result = split_page(
        args.pdf,
        page_number=args.page,
        dpi=args.dpi,
        output_dir=args.output_dir,
        db_path=args.db,
    )
    print_result(result)
