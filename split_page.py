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

    # Collect boundaries from every strip.
    # Include ALL confidence levels for consensus voting —
    # a position that recurs across strips is real even if
    # individual detections are low-confidence.
    all_positions = []

    for strip_idx, grid_y in enumerate(CONSENSUS_ROWS):
        try:
            results = find_column_boundaries(
                pdf_path, x=1, y=grid_y, w=10, h=1,
                page_number=page_number, dpi=dpi,
                darkness_threshold=dark_thresh,
            )
        except Exception:
            continue

        # Keep inner-page boundaries that pass quality filters.
        # Low-confidence boundaries are only included if they have
        # low row_std (consistent vertical line) or good valley depth.
        for r in results:
            if not (8 < r.page_pct < 92):
                continue
            if r.confidence in ("high", "medium"):
                all_positions.append({
                    "pct": r.page_pct,
                    "confidence": r.confidence,
                    "row_std": r.row_std,
                    "valley_depth": r.valley_depth,
                    "darkness": r.peak_darkness,
                    "strip": grid_y,
                })
            elif r.row_std < std_thresh or r.valley_depth > 40:
                # Low confidence but structurally promising
                all_positions.append({
                    "pct": r.page_pct,
                    "confidence": r.confidence,
                    "row_std": r.row_std,
                    "valley_depth": r.valley_depth,
                    "darkness": r.peak_darkness,
                    "strip": grid_y,
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

    # Score each cluster by:
    # 1. How many distinct strips contributed (breadth)
    # 2. Weighted confidence score (quality)
    # A boundary appearing in 3+ strips with any confidence is likely real.
    # A boundary appearing in 2 strips but both high-confidence is also real.
    num_strips = len(CONSENSUS_ROWS)
    conf_weights = {"high": 3, "medium": 2, "low": 1}

    boundaries = []
    for cluster in clusters:
        strips_hit = len(set(p["strip"] for p in cluster))
        weighted_score = sum(conf_weights.get(p["confidence"], 0) for p in cluster)
        # Use the detection with lowest row_std as representative
        best = min(cluster, key=lambda p: p["row_std"])
        mean_pct = np.mean([p["pct"] for p in cluster])

        # Accept if:
        # - appears in 3+ strips (strong spatial consensus), OR
        # - appears in 2+ strips with decent confidence score
        accept = (strips_hit >= 3) or (strips_hit >= 2 and weighted_score >= 4)

        if accept:
            boundaries.append({
                "x_pct": round(float(mean_pct), 2),
                "peak_darkness": best["darkness"],
                "row_std": best["row_std"],
                "valley_depth": best["valley_depth"],
                "confidence": best["confidence"],
                "strips_hit": strips_hit,
                "total_strips": num_strips,
                "consensus": round(strips_hit / num_strips, 2),
                "weighted_score": weighted_score,
            })

    # Sort by position
    boundaries.sort(key=lambda b: b["x_pct"])

    # ── Prune to best regular grid ───────────────────────────────────
    # The Gazette never had more than 7 columns (= 6 boundaries).
    # If we have more, keep the subset that forms the most regular grid.
    # Also filter out very narrow columns (< 5% of page width) which
    # are almost always false positives from ad borders or binding shadow.
    MAX_BOUNDARIES = 6

    if len(boundaries) > MAX_BOUNDARIES:
        boundaries = _select_best_grid(boundaries, MAX_BOUNDARIES)
    else:
        # Even under the limit, remove boundaries that create
        # implausibly narrow columns
        boundaries = _remove_narrow_columns(boundaries, min_width_pct=5.0)

    return boundaries, CONSENSUS_ROWS, _validate(boundaries)


def _remove_narrow_columns(boundaries, min_width_pct=5.0):
    """Remove boundaries that create columns narrower than min_width_pct."""
    if len(boundaries) < 2:
        return boundaries

    # Build column widths from boundaries
    edges = [0.0] + [b["x_pct"] for b in boundaries] + [100.0]
    widths = [edges[i+1] - edges[i] for i in range(len(edges)-1)]

    # Find narrow columns and remove the weaker of their two boundaries
    to_remove = set()
    for i, w in enumerate(widths):
        if w < min_width_pct and 0 < i < len(widths) - 1:
            # Narrow interior column: remove the boundary with lower score
            left_b = boundaries[i - 1] if i > 0 else None
            right_b = boundaries[i] if i < len(boundaries) else None
            if left_b and right_b:
                left_score = left_b.get("weighted_score", 0)
                right_score = right_b.get("weighted_score", 0)
                if left_score <= right_score:
                    to_remove.add(i - 1)
                else:
                    to_remove.add(i)
            elif left_b:
                to_remove.add(i - 1)
            elif right_b:
                to_remove.add(i)

    return [b for i, b in enumerate(boundaries) if i not in to_remove]


def _select_best_grid(boundaries, max_n):
    """
    From a set of candidate boundaries, select the subset of at most
    max_n that forms the most regular column grid.

    Strategy: try all combinations of max_n boundaries and score each
    by how evenly spaced the resulting columns are. With <=15 candidates
    and max_n=6, this is at most C(15,6) = 5005 combinations — fast enough.
    """
    from itertools import combinations

    if len(boundaries) <= max_n:
        return boundaries

    # If too many candidates for brute force, pre-filter
    if len(boundaries) > 15:
        # Keep top 15 by weighted_score
        boundaries = sorted(boundaries,
                           key=lambda b: b.get("weighted_score", 0),
                           reverse=True)[:15]
        boundaries.sort(key=lambda b: b["x_pct"])

    best_score = float("inf")
    best_combo = None

    for combo in combinations(range(len(boundaries)), max_n):
        selected = [boundaries[i] for i in combo]
        edges = [0.0] + [b["x_pct"] for b in selected] + [100.0]
        widths = [edges[i+1] - edges[i] for i in range(len(edges)-1)]

        # Skip if any column is too narrow
        if min(widths) < 5.0:
            continue

        # Score: standard deviation of column widths (lower = more regular)
        score = float(np.std(widths))

        if score < best_score:
            best_score = score
            best_combo = selected

    if best_combo is None:
        # Fallback: just take the top max_n by consensus score
        boundaries.sort(key=lambda b: b.get("weighted_score", 0), reverse=True)
        return sorted(boundaries[:max_n], key=lambda b: b["x_pct"])

    return best_combo


def _validate(boundaries):
    """
    Check boundaries for quality issues. Returns list of flag strings.
    """
    flags = []

    if not boundaries:
        flags.append("no_boundaries_detected")
        return flags

    # Filter to inner page area (10%–90%) for analysis
    inner = [b for b in boundaries if 10 < b["x_pct"] < 90]

    if not inner:
        flags.append("no_inner_boundaries")
        return flags

    # Confidence distribution
    high = sum(1 for b in inner if b["confidence"] == "high")
    low = sum(1 for b in inner if b["confidence"] == "low")
    if high == 0:
        flags.append("no_high_confidence_boundaries")
    if low > high:
        flags.append("mostly_low_confidence")

    # Column count plausibility
    num_cols = len(inner) + 1
    if num_cols < 3:
        flags.append(f"few_columns_{num_cols}")

    # Column width regularity
    positions = sorted(b["x_pct"] for b in inner)
    if len(positions) >= 2:
        widths = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        width_std = np.std(widths)
        width_mean = np.mean(widths)
        if width_mean > 0 and width_std / width_mean > 0.3:
            flags.append("irregular_column_widths")

    return flags


def extract_columns(pdf_path, boundaries, page_number, dpi, output_dir,
                    buffer_vw=BUFFER_VW):
    """
    Extract each column as a PNG using the detected boundaries.

    Returns list of ColumnResult.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    # Build column regions from boundaries
    # Columns are the spaces BETWEEN boundaries, plus the edges
    edges_vw = [0.0] + [b["x_pct"] for b in boundaries] + [100.0]
    columns = []

    for i in range(len(edges_vw) - 1):
        left = edges_vw[i]
        right = edges_vw[i + 1]
        width = right - left

        # Skip very narrow columns (< 3% of page width) — likely artifacts
        if width < 3.0:
            continue

        # Add buffer
        crop_left = max(0, left - buffer_vw)
        crop_right = min(100, right + buffer_vw)
        crop_width = crop_right - crop_left

        # Convert to PDF points
        x0 = pw * crop_left / 100
        y0 = 0
        x1 = pw * crop_right / 100
        y1 = ph

        clip = fitz.Rect(x0, y0, x1, y1)
        pix = page.get_pixmap(clip=clip, dpi=dpi)

        # Save
        stem = Path(pdf_path).stem
        col_filename = f"{stem}_col{i + 1}.png"
        col_path = os.path.join(output_dir, col_filename)
        pix.save(col_path)

        columns.append(ColumnResult(
            index=i,
            left_vw=round(left, 2),
            right_vw=round(right, 2),
            width_vw=round(width, 2),
            peak_darkness=boundaries[i]["peak_darkness"] if i < len(boundaries) else 0,
            confidence=boundaries[i]["confidence"] if i < len(boundaries) else "n/a",
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
            num_columns=0, columns=[], detection_row=used_row,
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
