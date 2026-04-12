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

    # Margin threshold: the gap between the page and sliver drops
    # to near-paper-baseline. Use a threshold well below text level
    # to distinguish real gaps from gradual content fading.
    body_median = float(np.median(center))
    margin_thresh = paper_baseline + (body_median - paper_baseline) * 0.3

    # ── Search from the binding edge INWARD ────────────────────────
    # This is more reliable than searching from the grid boundary
    # outward, because the binding edge is a known position (R2).
    #
    # Pattern from edge inward:
    #   [scan edge] → sliver content (if present) → GAP → page content
    #
    # The gap (print margin + gutter) is the boundary we want.

    # ── Find the gap between main content and sliver ───────────────
    # Look for the deepest dip in the binding-side region between
    # the last grid boundary and the page edge. The gap drops to
    # near-paper-baseline — much lower than any column gutter.
    #
    # The gap is the print margin of the newspaper page. Everything
    # between the gap and the binding edge is sliver/shadow.

    if binding_side == "right":
        # Search the region from the last grid boundary to the edge
        if last_grid_boundary:
            search_start_px = max(int(w * 0.5),
                                  int((last_grid_boundary - (pitch or 10)) / 100 * w))
        else:
            search_start_px = int(w * 0.65)
        search_end_px = w

        region = heavy[search_start_px:search_end_px]
        if len(region) > 10:
            # Find the deepest point in this region
            min_idx = search_start_px + int(np.argmin(region))
            min_val = float(heavy[min_idx])

            # Is it a real gap? Must be significantly below body level
            if min_val < margin_thresh:
                # Walk outward from minimum to find the gap boundaries
                # Left side of gap (toward content)
                margin_start_px = min_idx
                for x in range(min_idx, search_start_px, -1):
                    if heavy[x] > body_median * 0.6:
                        margin_start_px = x
                        break

                # Right side of gap (toward sliver/edge)
                margin_end_px = min_idx
                for x in range(min_idx, search_end_px):
                    if heavy[x] > body_median * 0.6:
                        margin_end_px = x
                        break

                content_end_px = margin_start_px
                gap_darkness = min_val

                # Is there content beyond the gap? (sliver)
                beyond = heavy[margin_end_px:search_end_px]
                if len(beyond) > 5 and float(np.max(beyond)) > margin_thresh:
                    sliver_start_px = margin_end_px
                else:
                    sliver_start_px = None
            else:
                # No significant gap — no sliver
                content_end_px = search_end_px
                margin_start_px = search_end_px
                margin_end_px = search_end_px
                gap_darkness = 0
                sliver_start_px = None
        else:
            content_end_px = w
            margin_start_px = w
            margin_end_px = w
            gap_darkness = 0
            sliver_start_px = None

    else:  # binding_side == "left"
        if last_grid_boundary:
            search_end_px = min(int(w * 0.5),
                                int((last_grid_boundary + (pitch or 10)) / 100 * w))
        else:
            search_end_px = int(w * 0.35)
        search_start_px = 0

        region = heavy[search_start_px:search_end_px]
        if len(region) > 10:
            min_idx = search_start_px + int(np.argmin(region))
            min_val = float(heavy[min_idx])

            if min_val < margin_thresh:
                margin_end_px = min_idx
                for x in range(min_idx, search_end_px):
                    if heavy[x] > body_median * 0.6:
                        margin_end_px = x
                        break

                margin_start_px = min_idx
                for x in range(min_idx, search_start_px, -1):
                    if heavy[x] > body_median * 0.6:
                        margin_start_px = x
                        break

                content_end_px = margin_end_px
                gap_darkness = min_val

                beyond = heavy[search_start_px:margin_start_px]
                if len(beyond) > 5 and float(np.max(beyond)) > margin_thresh:
                    sliver_start_px = margin_start_px
                else:
                    sliver_start_px = None
            else:
                content_end_px = search_start_px
                margin_start_px = search_start_px
                margin_end_px = search_start_px
                gap_darkness = 0
                sliver_start_px = None
        else:
            content_end_px = 0
            margin_start_px = 0
            margin_end_px = 0
            gap_darkness = 0
            sliver_start_px = None

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
