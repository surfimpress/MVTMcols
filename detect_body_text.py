"""
Detect body text regions in newspaper pages.

Scans down the centre of each placed column looking for the body text
rhythm: regular, tightly-spaced peaks with near-zero troughs (the
alternating pattern of text lines and inter-line white space).

Runs AFTER column placement so it has accurate column boundaries.
Results are stored per-column as vertical runs of body text.
"""

import fitz
import numpy as np
from coordinates import pct_to_px, px_to_pct


def detect_body_text(pdf_path, columns, page_number=0, dpi=300,
                     r2_top_pct=None, r2_bottom_pct=None):
    """
    Detect body text regions within placed columns.

    Args:
        pdf_path: Path to the PDF file
        columns: List of column dicts from page_meta.json, each with
                 left_vw and right_vw (% of page width)
        page_number: Zero-indexed page within the PDF
        dpi: Render resolution

    Returns:
        List of body text region dicts, each with:
            col_idx: column index (0-based)
            x1_pct, x2_pct: horizontal extent (% of page)
            y1_pct, y2_pct: vertical extent (% of page)
    """
    if not columns:
        return []

    # Render page
    doc = fitz.open(pdf_path)
    page = doc[page_number]
    pix = page.get_pixmap(dpi=dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = np.mean(img, axis=2)
    else:
        grey = img.reshape(pix.h, pix.w).astype(float)
    doc.close()

    h, w = grey.shape
    inv = 255.0 - grey

    # Constrain to R2 vertical extent
    y_min_px = pct_to_px(r2_top_pct or 0, h)
    y_max_px = min(pct_to_px(r2_bottom_pct or 100, h), h)

    # Detection parameters
    win = int(40 * dpi / 300)  # ~40px at 300 DPI, scales with resolution
    min_region_pct = 2.5  # minimum region height as % of page
    min_region_px = int(h * min_region_pct / 100)

    results = []
    charts = []

    for col in columns:
        left_px = pct_to_px(col['left_vw'], w)
        right_px = pct_to_px(col['right_vw'], w)
        col_w = right_px - left_px
        if col_w < 10:
            continue

        # Sample a fixed-width strip from the column centre.
        # Use a consistent width based on typical body text line
        # width (~60% of the narrowest column), not proportional
        # to each column's width. This ensures consistent detection
        # across standard and editorial-width columns.
        cx = (left_px + right_px) // 2
        # Find the narrowest column width as the reference
        min_col_w = min(pct_to_px(c['right_vw'] - c['left_vw'], w)
                        for c in columns)
        sample_hw = max(int(min_col_w * 0.24), 8)
        sx1 = max(0, cx - sample_hw)
        sx2 = min(w, cx + sample_hw)

        strip = inv[:, sx1:sx2].mean(axis=1)

        # Scan in overlapping windows, classify each as body text
        # Only within R2 vertical extent
        is_body = np.zeros(h, dtype=bool)
        for start in range(y_min_px, min(y_max_px, h) - win, win // 2):
            chunk = strip[start:start + win]

            # Find peaks and troughs
            peaks_v, peak_pos, troughs_v = [], [], []
            for j in range(1, len(chunk) - 1):
                if chunk[j] > chunk[j - 1] and chunk[j] > chunk[j + 1]:
                    peaks_v.append(chunk[j])
                    peak_pos.append(j)
                if chunk[j] < chunk[j - 1] and chunk[j] < chunk[j + 1]:
                    troughs_v.append(chunk[j])

            if len(peaks_v) >= 2 and troughs_v:
                spacing = float(np.mean(np.diff(peak_pos)))
                peak_mean = float(np.mean(peaks_v))
                trough_mean = float(np.mean(troughs_v))
                contrast = peak_mean - trough_mean
                # Body text: tight spacing + clear contrast between
                # text lines and gaps + peaks must be real ink
                # Scale spacing threshold with DPI
                max_spacing = 8 * dpi / 150
                if (spacing < max_spacing and contrast > 20 and peak_mean > 15):
                    is_body[start:min(start + win, h)] = True

        # Build chart: sample every row for full resolution sawtooth.
        # Only include rows within R2 extent to keep data size reasonable.
        col_chart = []
        for yi in range(y_min_px, y_max_px):
            col_chart.append({
                "y_pct": px_to_pct(yi, h),
                "val": round(float(strip[yi]), 1),
                "body": bool(is_body[yi]),
            })
        charts.append({
            "col_idx": col['index'],
            "x_pct": round((col['left_vw'] + col['right_vw']) / 2, 1),
            "chart": col_chart,
        })

        # Bridge small gaps: a headline or paragraph break within body
        # text shouldn't split the region. Fill gaps smaller than
        # ~5% of page height.
        max_gap_px = int(h * 0.05)
        gap_start = None
        for y in range(h):
            if not is_body[y]:
                if gap_start is None:
                    gap_start = y
            else:
                if gap_start is not None:
                    if y - gap_start <= max_gap_px:
                        is_body[gap_start:y] = True
                    gap_start = None

        # Extract contiguous body text runs
        in_run = False
        run_start = 0
        for y in range(h):
            if is_body[y]:
                if not in_run:
                    run_start = y
                    in_run = True
            else:
                if in_run:
                    if y - run_start >= min_region_px:
                        results.append({
                            'col_idx': col['index'],
                            'x1_pct': round(col['left_vw'], 1),
                            'x2_pct': round(col['right_vw'], 1),
                            'y1_pct': px_to_pct(run_start, h),
                            'y2_pct': px_to_pct(y, h),
                        })
                    in_run = False
        if in_run and h - run_start >= min_region_px:
            results.append({
                'col_idx': col['index'],
                'x1_pct': round(col['left_vw'], 1),
                'x2_pct': round(col['right_vw'], 1),
                'y1_pct': px_to_pct(run_start, h),
                'y2_pct': px_to_pct(h, h),
            })

    return results, charts
