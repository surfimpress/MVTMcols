"""
Facing page sliver detection for the Almonte Gazette pipeline.

Detects the boundary between the newspaper page content and the
facing page sliver on the binding side. Uses the print margin
(white gap between last column and the sliver) as the primary signal.

The sliver is identified by four signals:
1. No outer margin (text meets photograph edge)
2. No vertical rule at the inner boundary
3. Wide inner gap (print margin + gutter)
4. Width less than the expected column pitch

Usage:
    from detect_sliver import find_binding_edge

    edge = find_binding_edge("page.pdf", binding_side="right", pitch=10.2)
    # edge = {"margin_start_pct": 82.5, "margin_end_pct": 86.0,
    #         "sliver_present": True, "sliver_start_pct": 90.0, ...}
"""

import fitz
import numpy as np
from scipy.ndimage import gaussian_filter1d


def _open_clean(pdf_path):
    """Open a PDF and strip red overlay lines."""
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc


def find_binding_edge(pdf_path, page_number=0, binding_side="right",
                      pitch=None, last_grid_boundary=None, render_dpi=150):
    """
    Find the newspaper page boundary on the binding side.

    Looks for the print margin (white gap) beyond the last column,
    and detects whether a facing page sliver exists beyond it.

    Args:
        pdf_path:            Path to the PDF.
        page_number:         Zero-indexed page.
        binding_side:        "left" or "right".
        pitch:               Expected column pitch (% of page width).
        last_grid_boundary:  Where the grid says the last column ends (%).
        render_dpi:          DPI for analysis (150 sufficient).

    Returns:
        dict with margin position, sliver detection, and confidence.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pix = page.get_pixmap(dpi=render_dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = np.mean(img, axis=2)
    else:
        grey = img.reshape(pix.h, pix.w).astype(float)
    h, w = grey.shape
    inv = 255.0 - grey
    doc.close()

    # Column-wise darkness profile from body rows
    body = inv[int(h * 0.2):int(h * 0.8), :]
    col_profile = body.mean(axis=0)
    heavy = gaussian_filter1d(col_profile, sigma=10)

    # Paper baseline from the interior
    center = heavy[int(w * 0.3):int(w * 0.7)]
    paper_baseline = float(np.percentile(center, 10))

    # Margin threshold: close to paper baseline
    margin_thresh = paper_baseline + 5.0

    if binding_side == "right":
        # Search from the last grid boundary rightward for the margin
        if last_grid_boundary:
            search_start = int(last_grid_boundary / 100 * w)
        else:
            search_start = int(w * 0.70)

        # Walk leftward from search_start to find where content ends
        # (the right edge of the last real column)
        content_end_px = search_start
        for x in range(search_start, int(w * 0.60), -1):
            if heavy[x] > margin_thresh + 10:
                content_end_px = x
                break

        # Walk rightward from content_end to find the margin
        # (darkness drops below margin threshold)
        margin_start_px = content_end_px
        for x in range(content_end_px, w):
            if heavy[x] < margin_thresh:
                margin_start_px = x
                break

        # Find the end of the margin (where darkness rises again = sliver)
        margin_end_px = margin_start_px
        sliver_start_px = None
        for x in range(margin_start_px, w):
            if heavy[x] > margin_thresh + 10:
                margin_end_px = x
                sliver_start_px = x
                break
        if sliver_start_px is None:
            margin_end_px = w  # margin extends to page edge, no sliver

        # Check for binding darkness between margin and sliver
        if margin_end_px > margin_start_px + 5:
            gap_darkness = float(heavy[margin_start_px:margin_end_px].min())
        else:
            gap_darkness = 0

    else:  # binding_side == "left"
        # Mirror: search from the first grid boundary leftward
        if last_grid_boundary:
            search_start = int(last_grid_boundary / 100 * w)
        else:
            search_start = int(w * 0.30)

        content_end_px = search_start
        for x in range(search_start, int(w * 0.40)):
            if heavy[x] > margin_thresh + 10:
                content_end_px = x
                break

        margin_start_px = content_end_px
        for x in range(content_end_px, 0, -1):
            if heavy[x] < margin_thresh:
                margin_start_px = x
                break

        margin_end_px = margin_start_px
        sliver_start_px = None
        for x in range(margin_start_px, 0, -1):
            if heavy[x] > margin_thresh + 10:
                margin_end_px = x
                sliver_start_px = x
                break
        if sliver_start_px is None:
            margin_end_px = 0

        if margin_start_px > margin_end_px + 5:
            gap_darkness = float(heavy[margin_end_px:margin_start_px].min())
        else:
            gap_darkness = 0

    # Convert to percentages
    margin_start_pct = round(margin_start_px / w * 100, 1)
    margin_end_pct = round(margin_end_px / w * 100, 1)
    content_end_pct = round(content_end_px / w * 100, 1)

    sliver_present = sliver_start_px is not None
    sliver_start_pct = round(sliver_start_px / w * 100, 1) if sliver_present else None

    # Estimate sliver width if present
    sliver_width = None
    if sliver_present and pitch:
        if binding_side == "right":
            sliver_width = round(100 - sliver_start_pct, 1)
        else:
            sliver_width = round(sliver_start_pct, 1)

    # Is the sliver narrower than the pitch? (signal 4)
    sliver_is_partial = (sliver_width is not None and pitch is not None
                         and sliver_width < pitch)

    # Margin gap width
    margin_gap = abs(margin_end_pct - margin_start_pct)

    # Confidence: higher if margin is clear and wide
    if margin_gap > 2.0 and gap_darkness < paper_baseline + 3:
        confidence = "high"
    elif margin_gap > 1.0:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "binding_side": binding_side,
        "content_end_pct": content_end_pct,
        "margin_start_pct": margin_start_pct,
        "margin_end_pct": margin_end_pct,
        "margin_gap_pct": round(margin_gap, 1),
        "gap_darkness": round(gap_darkness, 1),
        "sliver_present": sliver_present,
        "sliver_start_pct": sliver_start_pct,
        "sliver_width_pct": sliver_width,
        "sliver_is_partial": sliver_is_partial,
        "confidence": confidence,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python detect_sliver.py <page.pdf> [--side left|right] [--pitch N]")
        sys.exit(1)

    pdf = sys.argv[1]
    side = "right"
    pitch = None
    for i, arg in enumerate(sys.argv):
        if arg == "--side" and i + 1 < len(sys.argv):
            side = sys.argv[i + 1]
        if arg == "--pitch" and i + 1 < len(sys.argv):
            pitch = float(sys.argv[i + 1])

    result = find_binding_edge(pdf, binding_side=side, pitch=pitch)
    for k, v in result.items():
        print(f"  {k}: {v}")
