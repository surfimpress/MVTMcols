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
import cv2
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

from pdf_utils import open_clean_pdf as _open_clean

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
    return_profile=False,
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

    # Pass 3: valley detection — column gaps as boundaries
    # When column rules are faint or absent, the gap between columns
    # reads as a dip toward white (low darkness). A valley is:
    #   - Local minimum below a low threshold (near white paper)
    #   - Content (darker) on both sides
    #   - Wide enough to be a real gap (> 3px), not noise
    # These are lower confidence than spike detections but when they
    # align across strips, the consensus builds.
    valley_max = min(darkness_threshold * 0.15, 15)  # must be genuinely white
    content_min = darkness_threshold * 0.35  # flanks must have some content
    min_valley_w = 3  # minimum width to avoid single-pixel noise

    # Find existing result positions to avoid duplicates
    existing_positions = set(r.local_x for r in results)

    for cx in range(margin + valley_width, img_w - margin - valley_width):
        val = col_means[cx]
        if val > valley_max:
            continue
        # Must be a local minimum within valley_width
        window = col_means[max(0, cx - valley_width):cx + valley_width + 1]
        if val > window.min() + 1:
            continue
        # Check width: how many adjacent pixels are also below threshold
        vw = 1
        lx, rx = cx - 1, cx + 1
        while lx >= 0 and col_means[lx] < valley_max:
            vw += 1; lx -= 1
        while rx < img_w and col_means[rx] < valley_max:
            vw += 1; rx += 1
        if vw < min_valley_w:
            continue
        # Centre of the valley
        valley_center = (lx + 1 + rx - 1) // 2
        # Skip if too close to an existing detection
        if any(abs(valley_center - ep) < strip_width * 3 for ep in existing_positions):
            continue
        # Content on both sides
        left_content = col_means[max(0, lx - valley_width):lx + 1].mean() if lx > 0 else 0
        right_content = col_means[rx:min(img_w, rx + valley_width)].mean() if rx < img_w else 0
        if left_content < content_min or right_content < content_min:
            continue

        page_pct = (clip_x0_frac + valley_center / img_w * (clip_x1_frac - clip_x0_frac)) * 100
        results.append(ColumnBoundary(
            local_x=valley_center,
            local_pct=round(valley_center / img_w * 100, 1),
            page_pct=round(page_pct, 2),
            peak_darkness=round(float(col_means[valley_center]), 1),
            row_std=0,
            valley_depth=round(float(left_content + right_content) / 2 - val, 1),
            confidence="valley",
        ))
        existing_positions.add(valley_center)

    # Sort: high confidence first, then by position
    order = {"high": 0, "medium": 1, "low": 2, "valley": 3}
    results.sort(key=lambda r: (order[r.confidence], r.local_x))

    if return_profile:
        # Downsample col_means to ~200 points mapped to page-%
        n_samples = min(200, img_w)
        step = max(1, img_w // n_samples)
        profile = []
        for lx in range(0, img_w, step):
            page_pct = (clip_x0_frac + lx / img_w * (clip_x1_frac - clip_x0_frac)) * 100
            profile.append({"pct": round(page_pct, 2), "val": round(float(col_means[lx]), 1)})
        return results, profile

    return results

def find_column_boundaries_morph(pdf_path, x=1, y=6, w=1, h=1,
                                 page_number=0, dpi=450,
                                 clip_x_frac=None,
                                 min_height_frac=0.3,
                                 binary_threshold=180):
    """
    Detect vertical column rules using morphological line extraction.

    Uses a tall, narrow morphological kernel to isolate vertical
    structures (column rules) from text and horizontal elements.
    More effective than Hough on heritage scans where rules are
    thin and faint.

    Args:
        pdf_path:          Path to the PDF.
        x, y, w, h:       Grid rectangle (1-indexed, 10% squares).
        page_number:       Zero-indexed page number.
        dpi:               Render resolution.
        clip_x_frac:       Optional (start, end) as fractions of page width.
        min_height_frac:   Minimum rule height as fraction of strip height.
        binary_threshold:  Threshold for binarisation (ink vs paper).

    Returns:
        List of ColumnBoundary objects from morphologically-detected rules.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    if clip_x_frac:
        clip_x0_frac, clip_x1_frac = clip_x_frac
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

    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        grey = img.reshape(pix.h, pix.w)

    img_h, img_w = grey.shape

    # Binarise: ink is dark → white in binary
    _, binary = cv2.threshold(grey, binary_threshold, 255, cv2.THRESH_BINARY_INV)

    # Morphological open with tall vertical kernel extracts only
    # structures that are at least min_height_frac of the strip tall
    # and 1 pixel wide — i.e., vertical rules
    kernel_h = max(10, int(img_h * min_height_frac))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_h))
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    # Sum vertically: columns with tall vertical ink get high values
    col_sums = v_lines.astype(float).sum(axis=0)
    min_sum = img_h * min_height_frac * 255 * 0.5  # at least half the kernel height

    # Find peaks in the column sum
    peaks = []
    for cx in range(5, img_w - 5):
        if col_sums[cx] < min_sum:
            continue
        # Local maximum within 10 pixels
        window = col_sums[max(0, cx-10):min(img_w, cx+11)]
        if col_sums[cx] < window.max():
            continue
        # Deduplicate
        if peaks and cx - peaks[-1][0] < 10:
            if col_sums[cx] > peaks[-1][1]:
                peaks[-1] = (cx, col_sums[cx])
            continue
        peaks.append((cx, float(col_sums[cx])))

    results = []
    for cx, strength in peaks:
        page_pct = (clip_x0_frac + cx / img_w * (clip_x1_frac - clip_x0_frac)) * 100

        # Confidence based on strength relative to maximum possible
        max_possible = img_h * 255
        ratio = strength / max_possible
        if ratio > 0.5:
            confidence = "high"
        elif ratio > 0.2:
            confidence = "medium"
        else:
            confidence = "low"

        results.append(ColumnBoundary(
            local_x=cx,
            local_pct=round(cx / img_w * 100, 1),
            page_pct=round(page_pct, 2),
            peak_darkness=round(strength, 1),
            row_std=0,
            valley_depth=round(ratio * 100, 1),
            confidence=confidence,
        ))

    results.sort(key=lambda r: r.page_pct)
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
