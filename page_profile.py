"""
Per-page scan quality profiler for the Almonte Gazette pipeline.

Analyses a full PDF page at low resolution to establish:
- Three nested bounding boxes (PDF page → scanned image → newspaper page)
- Paper baseline and noise level
- Ink darkness range and density
- Binding shadow location and severity
- Adaptive thresholds for column detection

Every spatial coordinate is a percentage of PDF page dimensions.
Bounding boxes are the sole source of truth for all downstream steps.

Usage:
    from page_profile import profile_page

    prof = profile_page("1920-01-02-03.pdf")
    print(f"Text area: {prof['text_area']['left']:.1f}%-{prof['text_area']['right']:.1f}%")
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


def find_rectangles(inv, h, w):
    """
    Detect the three nested rectangles in a scanned newspaper PDF page.

    Every PDF page has:
      R1 (PDF page):       white PDF margin | scanned image | white PDF margin
      R2 (scanned image):  black binding | newspaper page | facing page sliver + black
      R3 (newspaper page): white print margin | text columns | white print margin

    Args:
        inv:  Inverted greyscale array (dark ink = high values), shape (h, w)
        h, w: Image dimensions at profile DPI

    Returns:
        dict with bounding boxes r2, r3, text_area (all in % of page dimensions),
        plus binding_side.
    """
    # Column-wise mean darkness, middle 60% of rows (avoid masthead/footer)
    row_lo = int(h * 0.2)
    row_hi = int(h * 0.8)
    body_rows = inv[row_lo:row_hi, :]
    col_profile = body_rows.mean(axis=0)
    smooth = gaussian_filter1d(col_profile, sigma=5)

    # ── R1 → R2: PDF white margin to scanned image ──────────────────
    # PDF margins are digitally white: inverted value near 0 (< 2).
    # Paper/shadow is always > 5. This is a clean, reliable threshold.
    PDF_WHITE_THRESH = 2.0

    r2_left_px = 0
    for x in range(w):
        if smooth[x] > PDF_WHITE_THRESH:
            r2_left_px = x
            break

    r2_right_px = w - 1
    for x in range(w - 1, -1, -1):
        if smooth[x] > PDF_WHITE_THRESH:
            r2_right_px = x
            break

    # ── Binding side detection ───────────────────────────────────────
    # Within R2, the binding side has much higher darkness (shadow).
    edge_w = max(5, int((r2_right_px - r2_left_px) * 0.05))

    left_dark = float(smooth[r2_left_px:r2_left_px + edge_w].mean())
    right_dark = float(smooth[r2_right_px - edge_w:r2_right_px].mean())
    binding_side = "left" if left_dark > right_dark else "right"

    # ── Paper baseline from interior of R2 ───────────────────────────
    # Sample from the central 40% of R2 — guaranteed to be newspaper
    # content, not shadow or facing page.
    r2_span = r2_right_px - r2_left_px
    center_lo = r2_left_px + int(r2_span * 0.3)
    center_hi = r2_left_px + int(r2_span * 0.7)
    center_profile = smooth[center_lo:center_hi]

    # Paper baseline: the low end of the content darkness.
    # Use 25th percentile to get paper tone (below text/rule peaks).
    paper_baseline = float(np.percentile(center_profile, 25))
    paper_std = float(np.std(center_profile[center_profile < np.percentile(center_profile, 50)]))
    if paper_std < 1:
        paper_std = 1.0

    # The threshold where shadow ends and paper begins
    shadow_thresh = paper_baseline + 2.5 * paper_std

    # ── R2 → R3: binding side ───────────────────────────────────────
    # Walk inward from the binding edge. Shadow is dark and tapers.
    # Newspaper page begins where darkness drops below shadow_thresh.
    if binding_side == "left":
        r3_left_px = r2_left_px
        for x in range(r2_left_px, center_lo):
            if smooth[x] < shadow_thresh:
                r3_left_px = x
                break
    else:
        r3_left_px = r2_left_px
        for x in range(r2_left_px, center_lo):
            if smooth[x] < shadow_thresh:
                r3_left_px = x
                break

    if binding_side == "right":
        r3_right_px = r2_right_px
        for x in range(r2_right_px, center_hi, -1):
            if smooth[x] < shadow_thresh:
                r3_right_px = x
                break
    else:
        r3_right_px = r2_right_px
        for x in range(r2_right_px, center_hi, -1):
            if smooth[x] < shadow_thresh:
                r3_right_px = x
                break

    # ── R2 → R3: facing-page side ───────────────────────────────────
    # The facing page sliver (if present) sits between the edge shadow
    # and the main page. Look for a dark valley (inter-page gap) after
    # any initial content from the facing page.
    #
    # Walk inward from the non-binding R2 edge. Track the pattern:
    #   shadow zone → [facing content → dark valley →] newspaper page
    # The newspaper page starts after the last dark zone before the
    # paper baseline is reached consistently.
    facing_side = "left" if binding_side == "right" else "right"

    if facing_side == "left":
        # Walk rightward from r2_left looking for the true R3 start
        # First pass: skip the initial shadow/facing zone
        in_dark = True
        last_dark_end = r2_left_px
        for x in range(r2_left_px, center_lo):
            is_dark = smooth[x] > shadow_thresh
            if in_dark and not is_dark:
                in_dark = False
            elif not in_dark and is_dark:
                # Re-entered dark zone — this is the inter-page gap
                in_dark = True
            elif not in_dark and not is_dark:
                last_dark_end = x
                # Check: have we had a sustained run of paper?
                # If 10+ pixels of paper, we've found R3
                run_start = x
                while x < center_lo and smooth[x] < shadow_thresh:
                    x += 1
                if x - run_start >= 10:
                    r3_left_px = run_start
                    break
    else:
        # Walk leftward from r2_right
        in_dark = True
        for x in range(r2_right_px, center_hi, -1):
            is_dark = smooth[x] > shadow_thresh
            if in_dark and not is_dark:
                in_dark = False
            elif not in_dark and is_dark:
                in_dark = True
            elif not in_dark and not is_dark:
                run_start = x
                while x > center_hi and smooth[x] < shadow_thresh:
                    x -= 1
                if run_start - x >= 10:
                    r3_right_px = run_start
                    break

    # ── Text area: print margins within R3 ───────────────────────────
    # The newspaper page has white print margins on each side before
    # the text columns begin. Find where content starts/ends.
    r3_profile = smooth[r3_left_px:r3_right_px]
    if len(r3_profile) > 0:
        body_median = float(np.median(r3_profile))
    else:
        body_median = paper_baseline

    text_thresh = paper_baseline + 0.3 * max(1, body_median - paper_baseline)

    # Walk inward from R3 edges: first sustained run (5+ px) above threshold
    text_left_px = r3_left_px
    run = 0
    for x in range(r3_left_px, r3_right_px):
        if smooth[x] > text_thresh:
            run += 1
            if run >= 5:
                text_left_px = x - 4
                break
        else:
            run = 0

    text_right_px = r3_right_px
    run = 0
    for x in range(r3_right_px, r3_left_px, -1):
        if smooth[x] > text_thresh:
            run += 1
            if run >= 5:
                text_right_px = x + 4
                break
        else:
            run = 0

    # ── Build bounding boxes as % of page dimensions ─────────────────
    def bbox(left_px, right_px):
        return {
            "left": round(left_px / w * 100, 2),
            "right": round(right_px / w * 100, 2),
            "top": round(row_lo / h * 100, 2),      # approximate — using body rows
            "bottom": round(row_hi / h * 100, 2),
        }

    return {
        "r2": bbox(r2_left_px, r2_right_px),
        "r3": bbox(r3_left_px, r3_right_px),
        "text_area": bbox(text_left_px, text_right_px),
        "binding_side": binding_side,
        "paper_baseline": round(paper_baseline, 1),
        "paper_std": round(paper_std, 1),
        "shadow_thresh": round(shadow_thresh, 1),
    }


def profile_page(pdf_path, page_number=0, profile_dpi=150):
    """
    Analyse a page and return a calibration profile with bounding boxes.

    Returns:
        dict with rectangle bounds, calibration data, and thresholds.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height
    pix = page.get_pixmap(dpi=profile_dpi)

    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = np.mean(img, axis=2)
    else:
        grey = img.reshape(pix.h, pix.w).astype(float)

    h, w = grey.shape
    inv = 255.0 - grey
    doc.close()

    # ── Detect nested rectangles ─────────────────────────────────────
    rects = find_rectangles(inv, h, w)

    # ── Body region statistics (within text area) ────────────────────
    ta = rects["text_area"]
    ta_left = int(ta["left"] / 100 * w)
    ta_right = int(ta["right"] / 100 * w)
    ta_top = int(h * 0.15)
    ta_bottom = int(h * 0.85)

    body = inv[ta_top:ta_bottom, ta_left:ta_right]

    if body.size == 0:
        # Fallback if text area detection failed
        body = inv[int(h*0.2):int(h*0.8), int(w*0.1):int(w*0.9)]

    body_mean = float(body.mean())
    body_std = float(body.std())

    ink_mask = body > 128
    ink_coverage = float(ink_mask.mean())

    ink_pixels = body[ink_mask]
    ink_mean = float(np.median(ink_pixels)) if len(ink_pixels) > 100 else 200.0
    ink_std = float(np.std(ink_pixels)) if len(ink_pixels) > 100 else 30.0

    paper_pixels = body[~ink_mask]
    paper_body_mean = float(np.median(paper_pixels)) if len(paper_pixels) > 100 else 0.0

    p5 = float(np.percentile(body, 5))
    p95 = float(np.percentile(body, 95))
    dynamic_range = p95 - p5

    # ── Column profile (within text area) ────────────────────────────
    col_profile = body.mean(axis=0)
    col_median = float(np.median(col_profile))
    col_p90 = float(np.percentile(col_profile, 90))
    col_max = float(col_profile.max())

    # ── Derived thresholds ───────────────────────────────────────────
    paper_baseline = rects["paper_baseline"]
    paper_std = rects["paper_std"]

    column_darkness_threshold = max(
        col_median + (col_p90 - col_median) * 0.3,
        paper_baseline + 20,
        60,
    )

    row_std_threshold = min(45, max(25, paper_std * 3 + 15))
    valley_depth_threshold = max(20, dynamic_range * 0.05)

    # ── Quality flags ────────────────────────────────────────────────
    flags = []
    if dynamic_range < 100:
        flags.append("low_contrast")
    if paper_baseline > 30:
        flags.append("show_through")
    if paper_std > 15:
        flags.append("noisy_paper")
    if rects["binding_side"]:
        flags.append(f"binding_shadow_{rects['binding_side']}")
    if ink_coverage < 0.05:
        flags.append("sparse_content")
    if ink_coverage > 0.40:
        flags.append("dense_content")

    return {
        # Page dimensions
        "pdf_path": pdf_path,
        "page_number": page_number,
        "width_px": w,
        "height_px": h,
        "profile_dpi": profile_dpi,

        # Bounding boxes (% of PDF page dimensions)
        "r2": rects["r2"],
        "r3": rects["r3"],
        "text_area": rects["text_area"],
        "binding_side": rects["binding_side"],

        # Backward compatible (maps to text area)
        "content_x_start_frac": ta["left"] / 100,
        "content_x_end_frac": ta["right"] / 100,

        # Paper
        "paper_mean": round(paper_baseline, 1),
        "paper_std": round(paper_std, 1),
        "paper_body_mean": round(paper_body_mean, 1),

        # Ink
        "ink_mean": round(ink_mean, 1),
        "ink_std": round(ink_std, 1),
        "ink_coverage": round(ink_coverage, 4),

        # Contrast
        "dynamic_range": round(dynamic_range, 1),
        "body_mean": round(body_mean, 1),
        "body_std": round(body_std, 1),

        # Column profile
        "col_profile_median": round(col_median, 1),
        "col_profile_p90": round(col_p90, 1),
        "col_profile_max": round(col_max, 1),

        # Derived thresholds
        "column_darkness_threshold": round(column_darkness_threshold, 0),
        "row_std_threshold": round(row_std_threshold, 0),
        "valley_depth_threshold": round(valley_depth_threshold, 0),

        # Quality
        "quality_flags": flags,
    }


def print_profile(prof):
    """Human-readable summary."""
    print(f"Page profile: {prof['pdf_path']} (page {prof['page_number']})")
    print(f"  Size: {prof['width_px']}x{prof['height_px']} at {prof['profile_dpi']} dpi")

    print(f"  R2 (image):     {prof['r2']['left']:.1f}% - {prof['r2']['right']:.1f}%")
    print(f"  R3 (newspaper): {prof['r3']['left']:.1f}% - {prof['r3']['right']:.1f}%")
    print(f"  Text area:      {prof['text_area']['left']:.1f}% - {prof['text_area']['right']:.1f}%")
    print(f"  Binding side:   {prof['binding_side']}")

    print(f"  Paper: baseline={prof['paper_mean']:.0f}  std={prof['paper_std']:.0f}")
    print(f"  Ink: mean={prof['ink_mean']:.0f}  coverage={prof['ink_coverage']*100:.1f}%")
    print(f"  Dynamic range: {prof['dynamic_range']:.0f}")
    print(f"  Thresholds: darkness>={prof['column_darkness_threshold']:.0f}  "
          f"row_std<={prof['row_std_threshold']:.0f}")
    if prof["quality_flags"]:
        print(f"  Flags: {', '.join(prof['quality_flags'])}")
    else:
        print(f"  Quality: good")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python page_profile.py <page.pdf>")
        sys.exit(1)
    prof = profile_page(sys.argv[1])
    print_profile(prof)
