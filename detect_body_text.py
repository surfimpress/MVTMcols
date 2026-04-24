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
    y_min_px = int((r2_top_pct or 0) / 100 * h)
    y_max_px = int((r2_bottom_pct or 100) / 100 * h)
    y_max_px = min(y_max_px, h)

    # Detection parameters
    win = int(40 * dpi / 300)  # ~40px at 300 DPI, scales with resolution
    min_region_pct = 2.5  # minimum region height as % of page
    min_region_px = int(h * min_region_pct / 100)

    results = []

    for col in columns:
        left_px = int(col['left_vw'] / 100 * w)
        right_px = int(col['right_vw'] / 100 * w)
        col_w = right_px - left_px
        if col_w < 10:
            continue

        # Sample centre 30% of column width
        cx = (left_px + right_px) // 2
        sample_hw = max(int(col_w * 0.15), 5)
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
                            'y1_pct': round(run_start / h * 100, 1),
                            'y2_pct': round(y / h * 100, 1),
                        })
                    in_run = False
        if in_run and h - run_start >= min_region_px:
            results.append({
                'col_idx': col['index'],
                'x1_pct': round(col['left_vw'], 1),
                'x2_pct': round(col['right_vw'], 1),
                'y1_pct': round(run_start / h * 100, 1),
                'y2_pct': round(h / h * 100, 1),
            })

    return results
