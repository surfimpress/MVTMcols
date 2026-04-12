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


def find_rectangles(inv, h, w, gazette_page=None):
    """
    Detect the three nested rectangles in a scanned newspaper PDF page.

    Every PDF page has:
      R1 (PDF page):       white PDF margin | scanned image | white PDF margin
      R2 (scanned image):  black binding | newspaper page | facing page sliver + black
      R3 (newspaper page): white print margin | text columns | white print margin

    Args:
        inv:  Inverted greyscale array (dark ink = high values), shape (h, w)
        h, w: Image dimensions at profile DPI
        gazette_page: Page number from the gazette filename (1-indexed).
                      Odd = recto (binding left), even = verso (binding right).
                      If None, falls back to darkness comparison.

    Returns:
        dict with bounding boxes r2, r3, text_area (all in % of page dimensions),
        plus binding_side, clean_side, and page_type.
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

    # ── Binding side from page number (recto/verso) ────────────────
    # Odd pages = recto (right-hand page) → binding on LEFT
    # Even pages = verso (left-hand page) → binding on RIGHT
    # This is deterministic and always correct. If no page number
    # is provided, fall back to darkness comparison.
    edge_w = max(5, int((r2_right_px - r2_left_px) * 0.05))
    left_dark = float(smooth[r2_left_px:r2_left_px + edge_w].mean())
    right_dark = float(smooth[r2_right_px - edge_w:r2_right_px].mean())

    if gazette_page is not None:
        page_type = "recto" if gazette_page % 2 == 1 else "verso"
        binding_side = "left" if page_type == "recto" else "right"
        clean_side = "right" if page_type == "recto" else "left"
        # Confirm with darkness (log if they disagree but trust page number)
        darkness_says = "left" if left_dark > right_dark else "right"
        binding_confirmed = (binding_side == darkness_says)
    else:
        page_type = None
        binding_side = "left" if left_dark > right_dark else "right"
        clean_side = "right" if binding_side == "left" else "left"
        binding_confirmed = True  # no contradiction possible

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
    # The newspaper page has white print margins before the first column
    # and after the last column. There are NO vertical rules at the
    # outer edges — the text_area boundary comes from detecting the
    # grey block of column content against the white margin.
    #
    # Use a heavily smoothed profile (sigma=15) so individual text
    # lines blur into a uniform grey band. Then find the margin-to-text
    # transition: the local minimum (white margin) followed by a rise
    # into column content.

    heavy = gaussian_filter1d(col_profile, sigma=15)

    # Body darkness: median of the central 60% of R3
    r3_center_lo = r3_left_px + int((r3_right_px - r3_left_px) * 0.2)
    r3_center_hi = r3_left_px + int((r3_right_px - r3_left_px) * 0.8)
    r3_center_profile = heavy[r3_center_lo:r3_center_hi]
    if len(r3_center_profile) > 0:
        body_median = float(np.median(r3_center_profile))
    else:
        body_median = paper_baseline

    # The text edge threshold: halfway between the margin minimum
    # and the body content level. This catches the rising edge into text.
    # We find the margin minimum first, then set the threshold from it.

    # Left edge: the pattern is shadow_peak → margin_minimum → column_rise.
    # Find the shadow peak first (highest point in left 15% of R3),
    # then find the minimum AFTER the peak (the print margin),
    # then find where the profile rises from that minimum into column text.
    margin_search_end = r3_left_px + int((r3_right_px - r3_left_px) * 0.2)

    def _find_text_edge(heavy, search_start, search_end, body_median, direction="right"):
        """
        Find a text area edge and compute its confidence.

        Looks for shadow_peak → margin_minimum → column_rise pattern.
        Confidence is based on:
        - How deep the margin minimum is relative to body (deeper = clearer signal)
        - How sharp the rise from margin to content is (sharper = more confident)

        direction: "right" = searching left-to-right, "left" = right-to-left

        Returns (edge_px, confidence 0-1)
        """
        if direction == "right":
            region = heavy[search_start:search_end]
            if len(region) < 5:
                return search_start, 0.0

            peak_idx = search_start + int(np.argmax(region))
            peak_val = float(heavy[peak_idx])

            # First local minimum after peak
            min_idx = peak_idx
            for x in range(peak_idx + 1, search_end - 1):
                if heavy[x] <= heavy[x - 1] and heavy[x] <= heavy[x + 1]:
                    min_idx = x
                    break
            min_val = float(heavy[min_idx])

            thresh = min_val + 0.2 * (body_median - min_val)
            edge_px = min_idx
            for x in range(min_idx, search_end):
                if heavy[x] > thresh:
                    edge_px = x
                    break
        else:
            region = heavy[search_start:search_end]
            if len(region) < 5:
                return search_end, 0.0

            peak_idx = search_start + int(np.argmax(region))
            peak_val = float(heavy[peak_idx])

            min_idx = peak_idx
            for x in range(peak_idx - 1, search_start, -1):
                if heavy[x] <= heavy[x - 1] and heavy[x] <= heavy[x + 1]:
                    min_idx = x
                    break
            min_val = float(heavy[min_idx])

            thresh = min_val + 0.2 * (body_median - min_val)
            edge_px = min_idx
            for x in range(min_idx, search_start, -1):
                if heavy[x] > thresh:
                    edge_px = x
                    break

        # Confidence scoring:
        # 1. Margin depth: how much lower is the minimum than body_median?
        #    Full body_median drop = 1.0, no drop = 0.0
        depth_ratio = (body_median - min_val) / max(1, body_median) if body_median > 0 else 0
        depth_score = min(1.0, depth_ratio)

        # 2. Peak clarity: how much higher is the shadow peak than the margin?
        #    Strong shadow = clear separation. No shadow = we're guessing.
        if peak_val > min_val + 5:
            peak_score = min(1.0, (peak_val - min_val) / max(1, body_median))
        else:
            peak_score = 0.2  # weak: no clear shadow/margin separation

        # 3. Transition sharpness: how quickly does the profile rise from
        #    minimum to threshold? Sharp = confident, gradual = uncertain.
        rise_distance = abs(edge_px - min_idx)
        if rise_distance < 3:
            rise_score = 1.0   # very sharp
        elif rise_distance < 10:
            rise_score = 0.7
        elif rise_distance < 20:
            rise_score = 0.4
        else:
            rise_score = 0.2   # very gradual — low confidence

        confidence = (depth_score * 0.4 + peak_score * 0.3 + rise_score * 0.3)
        return edge_px, round(confidence, 3)

    # Left edge
    margin_search_end = r3_left_px + int((r3_right_px - r3_left_px) * 0.2)
    text_left_px, text_left_conf = _find_text_edge(
        heavy, r3_left_px, margin_search_end, body_median, direction="right"
    )

    # Right edge
    margin_search_start = r3_right_px - int((r3_right_px - r3_left_px) * 0.2)
    text_right_px, text_right_conf = _find_text_edge(
        heavy, margin_search_start, r3_right_px, body_median, direction="left"
    )

    # ── Build bounding boxes as % of page dimensions ─────────────────
    def bbox(left_px, right_px):
        return {
            "left": round(left_px / w * 100, 2),
            "right": round(right_px / w * 100, 2),
            "top": round(row_lo / h * 100, 2),
            "bottom": round(row_hi / h * 100, 2),
        }

    text_area = bbox(text_left_px, text_right_px)
    text_area["left_confidence"] = text_left_conf
    text_area["right_confidence"] = text_right_conf

    # Label confidence by side role
    if clean_side == "left":
        text_area["clean_side_confidence"] = text_left_conf
        text_area["binding_side_confidence"] = text_right_conf
    else:
        text_area["clean_side_confidence"] = text_right_conf
        text_area["binding_side_confidence"] = text_left_conf

    return {
        "r2": bbox(r2_left_px, r2_right_px),
        "r3": bbox(r3_left_px, r3_right_px),
        "text_area": text_area,
        "binding_side": binding_side,
        "clean_side": clean_side,
        "page_type": page_type,
        "binding_confirmed": binding_confirmed,
        "paper_baseline": round(paper_baseline, 1),
        "paper_std": round(paper_std, 1),
        "shadow_thresh": round(shadow_thresh, 1),
    }


def _extract_gazette_page(pdf_path):
    """Extract the gazette page number from the filename.

    Filenames follow the pattern: YYYY-MM-DD-PP.pdf
    Returns the page number (integer) or None if not parseable.
    """
    import re
    basename = pdf_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    # Match patterns like 1920-01-02-03 or 1920_p3
    m = re.search(r'-(\d{2})$', basename)
    if m:
        return int(m.group(1))
    m = re.search(r'_p(\d+)$', basename)
    if m:
        return int(m.group(1))
    return None


def profile_page(pdf_path, page_number=0, profile_dpi=150, gazette_page=None):
    """
    Analyse a page and return a calibration profile with bounding boxes.

    Args:
        pdf_path:      Path to the PDF.
        page_number:   Zero-indexed page within the PDF (usually 0).
        profile_dpi:   DPI for profiling render (150 default).
        gazette_page:  Gazette page number (1-indexed). If None,
                       extracted from the filename.

    Returns:
        dict with rectangle bounds, calibration data, and thresholds.
    """
    if gazette_page is None:
        gazette_page = _extract_gazette_page(pdf_path)
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
    rects = find_rectangles(inv, h, w, gazette_page=gazette_page)

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
        "clean_side": rects["clean_side"],
        "page_type": rects["page_type"],
        "gazette_page": gazette_page,
        "binding_confirmed": rects["binding_confirmed"],

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
