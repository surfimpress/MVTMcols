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
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, asdict
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from page_profile import profile_page
from page_context import build_context
from column_pipeline import detect_strips, cluster_boundaries, place_columns
from pdf_utils import (
    # open_clean_pdf as _open_clean,  # unused — kept commented for revival convenience
    get_clip_pixmap,
    get_page_size_pts,
    try_embedded_bitmap_pil,
)
from coordinates import pct_to_px


# ── Configuration ────────────────────────────────────────────────────────────

DEFAULT_DPI = 450
BUFFER_VW = 1.0  # 1% of page width added each side of column crop


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
#
# Detection (boundaries → clusters → placement) lives in column_pipeline.py.
# This module's CLI calls into that shared chain so behaviour matches the
# main pipeline. The kept functions below (extract_columns, _save_metadata,
# _log_to_db) are imported by process_issue.py and form the column-export
# half of the page split.


def extract_columns(pdf_path, boundaries, page_number, dpi, output_dir,
                    buffer_vw=BUFFER_VW, ads_with_uuids=None):
    """
    Extract each column as a PNG using the detected boundaries.

    Boundaries are the column RULES — columns are the spaces between
    adjacent rules. The first boundary is the left edge of column 1,
    and the last boundary is the right edge of the last column.
    Anything outside those is margin/binding/facing page bleed.

    With N boundaries you get N-1 columns.

    If ads_with_uuids is provided (list of dicts with uuid, x_pct,
    y_pct, x_end_pct, y_end_pct), each column PNG is written as RGBA
    with the overlapping ad region punched out (alpha=0) and labelled
    with the first 6 chars of the ad uuid at the centre of the
    clipped hole. The buffered crop window is preserved so labels and
    holes line up with the visible PNG content.

    Returns list of ColumnResult.
    """
    if len(boundaries) < 2:
        return []

    # P-shared: full-page render is cached once via get_clip_pixmap;
    # each column slice reuses it instead of re-rasterising the page.
    pw, ph = get_page_size_pts(pdf_path, page_number, dpi)

    # Fast path: when the source page is a single 1-bit (JBIG2) image
    # and the bitmap gate is set, fetch it once at native resolution
    # and crop each column from it. mode='1' for no-ads columns, 'LA'
    # for columns with ads (alpha needed for hole-punching). Falls
    # through to the legacy fitz-clip path when bitmap_pil is None.
    bitmap_pil = try_embedded_bitmap_pil(pdf_path, page_number)

    columns = []
    col_num = 0

    ads_with_uuids = ads_with_uuids or []

    # Load font once for all columns. Arial 24pt is already used
    # elsewhere in the codebase for ad annotations.
    label_font = None
    if ads_with_uuids:
        try:
            label_font = ImageFont.truetype(
                "/System/Library/Fonts/Supplemental/Arial.ttf", 24)
        except OSError:
            label_font = ImageFont.load_default()

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

        stem = Path(pdf_path).stem
        col_filename = f"{stem}_col{col_num}.png"
        col_path = os.path.join(output_dir, col_filename)

        if bitmap_pil is not None:
            # Bitmap path: native-resolution mode='1' crop, no alpha
            # holes for ads. The viewer's overlay layers carry the
            # ad rectangles, so punching them into the column raster
            # is redundant and would force us off the bilevel path.
            bw, bh = bitmap_pil.size
            cx1 = pct_to_px(crop_left, bw)
            cx2 = pct_to_px(crop_right, bw)
            column_pil = bitmap_pil.crop((cx1, 0, cx2, bh))
            column_pil.save(col_path, optimize=True)
        elif not ads_with_uuids:
            # Legacy path, no ads: keep original opaque-PNG fast path.
            pix = get_clip_pixmap(pdf_path, page_number, dpi, clip)
            pix.save(col_path)
        else:
            # Legacy path with ads: round-trip pixmap through PIL to
            # get RGBA. PyMuPDF's get_pixmap doesn't expose alpha=True
            # in this version, so build the alpha plane ourselves.
            pix = get_clip_pixmap(pdf_path, page_number, dpi, clip)
            img = Image.frombytes("RGB", (pix.width, pix.height),
                                  pix.samples).convert("RGBA")
            arr = np.array(img)
            iw, ih = pix.width, pix.height

            col_w_pct = crop_right - crop_left
            draw = ImageDraw.Draw(img)

            for ad in ads_with_uuids:
                # Sliver guard: a real ad occupies ~the full column width
                # or none of it. If the ad's intersection with this
                # column (unbuffered) is less than half a column wide,
                # the ad lives in a neighbouring column and is leaking
                # in via its bbox edge — skip rather than punch a hole
                # through legitimate body text.
                col_ox1 = max(ad["x_pct"], left)
                col_ox2 = min(ad["x_end_pct"], right)
                if col_ox2 <= col_ox1:
                    continue
                col_xov = (col_ox2 - col_ox1) / width
                if col_xov < 0.5:
                    continue

                # Convert ad pct coords to this column's pixel coords
                # using the buffered crop window.
                ax_pct_in_col = ad["x_pct"] - crop_left
                ax_end_pct_in_col = ad["x_end_pct"] - crop_left

                if col_w_pct <= 0:
                    continue
                ax1 = int(round(ax_pct_in_col / col_w_pct * iw))
                ax2 = int(round(ax_end_pct_in_col / col_w_pct * iw))
                ay1 = int(round(ad["y_pct"] / 100.0 * ih))
                ay2 = int(round(ad["y_end_pct"] / 100.0 * ih))

                # Clip to image bounds.
                ax1c = max(0, min(iw, ax1))
                ax2c = max(0, min(iw, ax2))
                ay1c = max(0, min(ih, ay1))
                ay2c = max(0, min(ih, ay2))
                if ax2c <= ax1c or ay2c <= ay1c:
                    continue

                # Punch hole: zero the alpha channel.
                arr[ay1c:ay2c, ax1c:ax2c, 3] = 0

                # Re-build PIL image from the modified array so the
                # text we draw next sits on top of the transparent
                # region (otherwise draw uses the stale img buffer).
                img = Image.fromarray(arr, mode="RGBA")
                draw = ImageDraw.Draw(img)

                cx = (ax1c + ax2c) // 2
                cy = (ay1c + ay2c) // 2
                draw.text(
                    (cx, cy), f"#{ad['uuid'][:6]}",
                    fill=(80, 80, 80, 255),
                    font=label_font, anchor="mm",
                    stroke_width=1,
                    stroke_fill=(255, 255, 255, 255),
                )
                arr = np.array(img)

            img.save(col_path)

        columns.append(ColumnResult(
            index=col_num - 1,
            left_vw=round(left, 2),
            right_vw=round(right, 2),
            width_vw=round(width, 2),
            peak_darkness=boundaries[i]["peak_darkness"],
            confidence=boundaries[i]["confidence"],
            image_path=col_path,
        ))

    return columns


def split_page(pdf_path, page_number=0, dpi=DEFAULT_DPI, output_dir=None,
               db_path=None, expected_columns=None, prior_boundaries=None,
               prior_page_type=None, ad_exclusion_zones=None,
               year=None, gazette_page=None):
    """
    Single-page CLI wrapper around the column-detection pipeline.

    Routes through the same chain process_issue uses:
        profile_page → build_context → detect_strips →
        cluster_boundaries → place_columns → extract_columns

    This is the entry point for invoking detection on one PDF without
    issue-level orchestration. When `year` / `gazette_page` are not
    supplied they're inferred from the filename (YYYY-MM-DD-PP.pdf).
    Without a year we fall back to era-default priors via build_context.

    Args:
        pdf_path:           Path to single-page PDF.
        page_number:        Zero-indexed page within the PDF.
        dpi:                Render resolution for column images.
        output_dir:         Where to save column PNGs. Defaults to <stem>_columns/.
        db_path:            SQLite database to log results. Optional.
        expected_columns:   Override the column count derived from priors.
        prior_boundaries:   Deprecated. Ignored — handled by issue-level
                            pitch establishment in process_issue.
        prior_page_type:    Deprecated. Ignored.
        ad_exclusion_zones: Optional list of (x1, x2, y1, y2) tuples in
                            page-pct coordinates. If not provided the CLI
                            runs without ad zones (single-page mode).

    Returns:
        PageResult with all columns and quality flags.
    """
    import re

    t0 = time.time()
    pdf_path = str(pdf_path)

    # Set up output directory
    if output_dir is None:
        stem = Path(pdf_path).stem
        output_dir = os.path.join(os.path.dirname(pdf_path) or ".", f"{stem}_columns")
    os.makedirs(output_dir, exist_ok=True)

    # ── Infer year / gazette_page from filename ─────────────────────
    # Filename pattern: YYYY-MM-DD-PP.pdf
    if year is None or gazette_page is None:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})",
                      Path(pdf_path).stem)
        if m:
            if year is None:
                year = int(m.group(1))
            if gazette_page is None:
                gazette_page = int(m.group(4))
    if gazette_page is None:
        gazette_page = 1  # default to recto when filename doesn't tell us

    # ── Profile the page ───────────────────────────────────────────
    try:
        page_prof = profile_page(pdf_path, page_number,
                                 gazette_page=gazette_page)
    except Exception:
        page_prof = None

    # Pull pixel dimensions from the shared render cache. Earlier this
    # opened a fresh _open_clean doc and rendered a full-page pixmap just
    # to read .w / .h — wasteful since extract_columns runs at the same
    # canonical DPI that detect_strips / detect_ads have already cached
    # for this page.
    try:
        pw_pts, ph_pts = get_page_size_pts(pdf_path, page_number, dpi)
    except Exception as e:
        return PageResult(
            pdf_path=pdf_path, page_number=page_number, dpi=dpi,
            page_width_px=0, page_height_px=0, num_columns=0,
            columns=[], detection_row=0, quality_flags=[],
            error=f"pdf_open_failed: {e}", elapsed_seconds=time.time() - t0,
        )
    page_w_px = int(round(pw_pts * dpi / 72.0))
    page_h_px = int(round(ph_pts * dpi / 72.0))

    quality_flags = []
    if page_prof and page_prof.get("quality_flags"):
        quality_flags = list(page_prof["quality_flags"])

    # ── Build context (same path as process_issue) ─────────────────
    # No issue_pitch / issue_columns: build_context falls back to era
    # priors (or 11.0% / 7 cols if the database/era is unavailable).
    ctx = build_context(
        gazette_page=gazette_page,
        year=year if year is not None else 1900,
        db_path=db_path or "data/mvtm.db",
        profile=page_prof,
        ads=None,
        issue_pitch=None,
        issue_columns=expected_columns,
    )

    # CLI override: if caller supplied ad_exclusion_zones explicitly,
    # use those instead of an empty list.
    if ad_exclusion_zones:
        ctx.ad_zones = list(ad_exclusion_zones)

    # ── Pipeline: detect → cluster → place ─────────────────────────
    raw, strip_profiles, _dark_thresh = detect_strips(pdf_path, ctx, dpi=dpi)
    clustered = cluster_boundaries(
        raw, strip_profiles=strip_profiles, ad_zones=ctx.ad_zones,
    )

    if not clustered:
        return PageResult(
            pdf_path=pdf_path, page_number=page_number, dpi=dpi,
            page_width_px=page_w_px, page_height_px=page_h_px,
            num_columns=0, columns=[], detection_row=[],
            quality_flags=quality_flags + ["no_boundaries_detected"],
            error="no_column_boundaries_found",
            elapsed_seconds=round(time.time() - t0, 2),
        )

    final = place_columns(clustered, ctx)

    # ── Validate edge columns (same as process_issue) ──────────────
    try:
        from validate_columns import validate_edge_columns
        final, dropped = validate_edge_columns(final, pdf_path,
                                               page_number=page_number)
        for side, ink, med, ratio in dropped:
            quality_flags.append(
                f"dropped_edge_{side}(ink={ink:.0f}/{med:.0f},"
                f"ratio={ratio:.2f})"
            )
    except Exception:
        pass  # non-fatal

    # ── Extract columns ────────────────────────────────────────────
    columns = extract_columns(
        pdf_path, final, page_number, dpi, output_dir,
    )

    elapsed = time.time() - t0
    result = PageResult(
        pdf_path=pdf_path, page_number=page_number, dpi=dpi,
        page_width_px=page_w_px, page_height_px=page_h_px,
        num_columns=len(columns), columns=columns,
        detection_row=[], quality_flags=quality_flags,
        error=None, elapsed_seconds=round(elapsed, 2),
    )

    if db_path:
        _log_to_db(result, db_path)

    meta_path = os.path.join(output_dir, "page_meta.json")
    _save_metadata(result, meta_path)

    return result


# ── Database logging ─────────────────────────────────────────────────────────

def _log_to_db(result, db_path):
    """Log page-splitting results to the SQLite database."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
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
    print("  Detection: column_pipeline (detect_strips → cluster → place)")

    if result.error:
        print(f"  ERROR: {result.error}")

    if result.quality_flags:
        print(f"  Flags: {', '.join(result.quality_flags)}")
    else:
        print("  Quality: good")

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
