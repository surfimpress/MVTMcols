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

def _detect_consensus(pdf_path, page_number, dpi, page_prof=None):
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

        # No post-hoc filtering needed — clip_x_frac ensures all
        # results are within the text area.
        for r in results:
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

    # Cap at max boundaries
    MAX_BOUNDARIES = 9  # 9 boundaries = 8 columns max
    if len(boundaries) > MAX_BOUNDARIES:
        boundaries = _select_best_grid(boundaries, MAX_BOUNDARIES)

    # ── Grid projection from interior columns ────────────────────────
    # Use detected interior boundaries as ground truth. Project each
    # interior column's width across the page to predict where the
    # outer edges should be. Aggregate predictions weighted by how
    # well they match the other detected boundaries.
    if len(boundaries) >= 4:
        clean_side = page_prof.get("clean_side", "right") if page_prof else "right"
        boundaries = _project_grid_edges(
            boundaries,
            text_left_pct=text_left_frac * 100,
            text_right_pct=text_right_frac * 100,
            clean_side=clean_side,
        )

    # Cap at 9 boundaries (8 columns max)
    if len(boundaries) > 9:
        # Keep the most confident, preserving position order
        scored = sorted(boundaries,
                       key=lambda b: b.get("weighted_score", 0)
                                   + (10 if b["confidence"] in ("high", "medium") else 0),
                       reverse=True)[:9]
        boundaries = sorted(scored, key=lambda b: b["x_pct"])

    # Remove columns narrower than 50% of the median interior width.
    # These are false boundaries from ad borders or noise.
    # Use median so a single narrow outlier doesn't drag down the threshold.
    if len(boundaries) >= 3:
        positions = [b["x_pct"] for b in boundaries]
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        median_width = float(np.median(widths))
        min_acceptable = median_width * 0.65
        boundaries = _remove_narrow_columns(boundaries, min_width_pct=min_acceptable)

        # After narrow removal, check for interior gaps that are ~2x the pitch.
        # These were created by the narrow merge and need re-interpolation.
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

    return boundaries, CONSENSUS_ROWS, _validate(boundaries)


def _project_grid_edges(boundaries, tolerance=2.0, text_left_pct=0,
                        text_right_pct=100, clean_side="right"):
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

    # Add all projected left edges (sorted rightmost first for insert)
    for pct, conf in sorted(left_edges, reverse=True):
        if 0 < pct < positions[0]:
            result.insert(0, make_edge_boundary(pct, conf))

    # Add all projected right edges
    for pct, conf in sorted(right_edges):
        if positions[-1] < pct < 100:
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


def _regularise_grid(boundaries):
    """
    Fill in missing column boundaries using the detected grid pitch.

    The newspaper's column grid is always evenly spaced within a page.
    Detected boundaries stay at their detected positions — they mark
    where the actual printed rules are. This function only ADDS missing
    boundaries by interpolation where a gap is too wide for one column.

    Returns the augmented boundary list.
    """
    if len(boundaries) < 3:
        return boundaries

    positions = [b["x_pct"] for b in boundaries]
    widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]

    if not widths:
        return boundaries

    # Find the dominant pitch: the most common column width.
    # Wide columns have missed boundaries — exclude them.
    # Strategy: find the tightest cluster of widths. The minimum width
    # is the best single-column indicator, and widths within 30% of
    # the minimum are also single columns.
    min_w = min(widths)
    single_col_widths = [w for w in widths if w < min_w * 1.3]
    if not single_col_widths:
        single_col_widths = [min_w]
    pitch = float(np.mean(single_col_widths))

    if pitch < 3.0:
        return boundaries

    # Are the widths already regular enough? If CV < 0.15, don't touch.
    cv = float(np.std(widths) / np.mean(widths)) if np.mean(widths) > 0 else 0
    if cv < 0.15:
        return boundaries

    # Walk through adjacent boundary pairs. If a gap is wider than
    # 1.5x the pitch, interpolate the missing boundaries within it.
    # Detected boundaries NEVER move — they stay at their detected x_pct.
    result = [boundaries[0]]

    for i in range(len(boundaries) - 1):
        gap = positions[i + 1] - positions[i]
        # How many columns fit in this gap?
        # A gap of 1.4x pitch or wider contains a missing boundary.
        ratio = gap / pitch
        if ratio < 1.4:
            missing_count = 0
        else:
            # round() rounds 1.5 to 2, but 1.4-1.49 rounds to 1.
            # We want 1.4+ to always mean "at least 2 columns here".
            cols_in_gap = max(2, round(ratio))
            missing_count = cols_in_gap - 1

        if missing_count > 0:
            # Subdivide this gap evenly
            step = gap / (missing_count + 1)
            for m in range(1, missing_count + 1):
                interp_x = positions[i] + m * step
                result.append({
                    "x_pct": round(interp_x, 2),
                    "peak_darkness": 0,
                    "row_std": 0,
                    "valley_depth": 0,
                    "confidence": "interpolated",
                    "strips_hit": 0,
                    "total_strips": 0,
                    "consensus": 0,
                    "weighted_score": 0,
                })

        result.append(boundaries[i + 1])

    # Cap total boundaries at 8 (= 7 columns max)
    if len(result) > 8:
        # Keep the detected ones and trim interpolated from edges
        detected = [b for b in result if b["confidence"] != "interpolated"]
        interpolated = [b for b in result if b["confidence"] == "interpolated"]
        result = detected + interpolated[:8 - len(detected)]
        result.sort(key=lambda b: b["x_pct"])

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
               db_path=None):
    """
    Full page-splitting pipeline.

    Args:
        pdf_path:     Path to single-page PDF.
        page_number:  Zero-indexed page within the PDF.
        dpi:          Render resolution for column images.
        output_dir:   Where to save column PNGs. Defaults to <stem>_columns/.
        db_path:      SQLite database to log results. Optional.

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
        pdf_path, page_number, dpi, page_prof
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
