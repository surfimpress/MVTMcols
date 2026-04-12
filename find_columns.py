"""
Detect likely column boundaries in a grid rectangle of a scanned PDF page.

Automatically strips red overlay lines (1 0 0 RG) before rendering.

Uses a two-pass approach:
Pass 1: Identify candidate columns by mean vertical darkness.
Pass 2: Validate candidates by measuring row-by-row consistency
and checking for the valley-spike-valley pattern
(whitespace either side of a ruled line).

Usage:
results = find_column_boundaries("page.pdf", x=7, y=6)
for r in results:
    print(f"{r.page_pct:.1f}% across page, confidence={r.confidence}")
"""

import fitz
import numpy as np
from dataclasses import dataclass

@dataclass
class ColumnBoundary:
    local_x: int            # pixel position within the crop
    local_pct: float        # % across the crop
    page_pct: float         # % across the full page
    peak_darkness: float    # mean darkness at the candidate column
    row_std: float          # row-by-row std (low = consistent = likely rule)
    valley_depth: float     # how much lighter the flanking whitespace is
    confidence: str         # "high", "medium", "low"

def _open_clean(pdf_path):
    """Open a PDF and strip red overlay lines from all pages."""
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc

def find_column_boundaries(
    pdf_path,
    x, y, w=1, h=1,
    page_number=0,
    dpi=450,
    darkness_threshold=140,
    strip_width=5,
    valley_width=15,
    max_row_std=35,
    clip_x_frac=None,
):
    """
    Detect column boundaries in a grid rectangle or clipped region.

    Args:
        pdf_path:            Path to the PDF.
        x, y, w, h:         Grid rectangle (1-indexed, 10% squares).
        page_number:         Zero-indexed page number.
        dpi:                 Render resolution.
        darkness_threshold:  Minimum mean darkness (0-255) for a candidate.
        strip_width:         Pixel width of the strip for pass 2 analysis.
        valley_width:        Pixels either side to check for whitespace valleys.
        max_row_std:         Maximum row std to consider a candidate consistent.
        clip_x_frac:         Optional (start, end) as fractions of page width
                             (0.0-1.0). Overrides x/w for the horizontal clip.
                             page_pct output maps back to PDF page percentages.

    Returns:
        List of ColumnBoundary objects, sorted by confidence then position.
    """
    # Render the crop
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    if clip_x_frac:
        clip_x0_frac, clip_x1_frac = clip_x_frac
        clip = fitz.Rect(
            pw * clip_x0_frac,
            ph * (y - 1) / 10,
            pw * clip_x1_frac,
            ph * (y - 1 + h) / 10,
        )
    else:
        clip_x0_frac = (x - 1) / 10
        clip_x1_frac = (x - 1 + w) / 10
        clip = fitz.Rect(
            pw * clip_x0_frac,
            ph * (y - 1) / 10,
            pw * clip_x1_frac,
            ph * (y - 1 + h) / 10,
        )
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    doc.close()

    # Convert to numpy greyscale
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n == 4:
        img = img.reshape(pix.h, pix.w, 4)[:, :, :3]
    elif pix.n == 3:
        img = img.reshape(pix.h, pix.w, 3)
    else:
        img = img.reshape(pix.h, pix.w)

    if img.ndim == 3:
        grey = np.mean(img, axis=2)
    else:
        grey = img.astype(float)

    img_h, img_w = grey.shape

    # Work with the middle 60% of rows to avoid headers/borders
    row_lo = int(img_h * 0.2)
    row_hi = int(img_h * 0.8)
    middle = grey[row_lo:row_hi, :]

    # Invert so dark = high values
    inv = 255.0 - middle

    # Pass 1: mean darkness per column
    col_means = inv.mean(axis=0)

    # Find local peaks above threshold, using only strip_width as
    # the minimum margin (no arbitrary edge exclusion)
    candidates = []
    margin = strip_width
    for cx in range(margin, img_w - margin):
        if col_means[cx] < darkness_threshold:
            continue
        # Must be a local maximum within strip_width
        window = col_means[cx - strip_width:cx + strip_width + 1]
        if col_means[cx] < window.max():
            continue
        # Avoid duplicates: skip if a stronger candidate is nearby
        if candidates and cx - candidates[-1][0] < strip_width * 2:
            if col_means[cx] > candidates[-1][1]:
                candidates[-1] = (cx, col_means[cx])
            continue
        candidates.append((cx, col_means[cx]))

    # Pass 2: validate each candidate
    results = []
    for cx, peak in candidates:
        # Row-by-row consistency
        s_lo = max(0, cx - strip_width // 2)
        s_hi = min(img_w, cx + strip_width // 2 + 1)
        strip = inv[:, s_lo:s_hi]
        row_means = strip.mean(axis=1)
        row_std = np.std(row_means)

        # Valley-spike-valley: use whatever flanking space is available
        left_lo = max(0, cx - valley_width)
        left_hi = cx - strip_width // 2
        right_lo = cx + strip_width // 2 + 1
        right_hi = min(img_w, cx + valley_width + 1)

        flanks = []
        if left_hi > left_lo:
            flanks.append(col_means[left_lo:left_hi].mean())
        if right_hi > right_lo:
            flanks.append(col_means[right_lo:right_hi].mean())

        flank_mean = np.mean(flanks) if flanks else peak
        valley_depth = peak - flank_mean

        # Confidence scoring
        if row_std <= max_row_std and valley_depth > 80:
            confidence = "high"
        elif row_std <= max_row_std or valley_depth > 60:
            confidence = "medium"
        else:
            confidence = "low"

        # Map local pixel position back to PDF page percentage.
        # clip_x0_frac and clip_x1_frac define the clip region as
        # fractions of page width. cx/img_w gives position within clip.
        page_pct = (clip_x0_frac + cx / img_w * (clip_x1_frac - clip_x0_frac)) * 100

        results.append(ColumnBoundary(
            local_x=cx,
            local_pct=round(cx / img_w * 100, 1),
            page_pct=round(page_pct, 2),
            peak_darkness=round(peak, 1),
            row_std=round(row_std, 1),
            valley_depth=round(valley_depth, 1),
            confidence=confidence,
        ))

    # Sort: high confidence first, then by position
    order = {"high": 0, "medium": 1, "low": 2}
    results.sort(key=lambda r: (order[r.confidence], r.local_x))

    return results

def print_results(results):
    """Pretty-print the analysis results."""
    if not results:
        print("No column boundaries detected.")
        return
    for r in results:
        print(
            f"  {r.confidence.upper():6s}  "
            f"page {r.page_pct:5.1f}%  "
            f"(local x={r.local_x}, {r.local_pct}%)  "
            f"darkness={r.peak_darkness:.0f}  "
            f"row_std={r.row_std:.1f}  "
            f"valley_depth={r.valley_depth:.0f}"
        )

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python find_columns.py <pdf> <x> <y> [w] [h]")
        sys.exit(1)

    pdf = sys.argv[1]
    gx, gy = int(sys.argv[2]), int(sys.argv[3])
    gw = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    gh = int(sys.argv[5]) if len(sys.argv) > 5 else 1

    results = find_column_boundaries(pdf, gx, gy, gw, gh)
    print(f"Column boundaries in grid ({gx},{gy},{gw},{gh}):")
    print_results(results)
