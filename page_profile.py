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


def find_rectangles(inv, h, w, gazette_page=None, pdf_image_rect=None):
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
        pdf_image_rect: Optional dict with left/right/top/bottom as % of page,
                        extracted from the PDF image placement structure.
                        When available, this is the authoritative R2 source —
                        exact coordinates rather than raster threshold guessing.

    Returns:
        dict with bounding boxes r2, r3, text_area (all in % of page dimensions),
        plus binding_side, clean_side, and page_type.
    """
    # ── Column-wise profiles within the scanned image ───────────────
    # Computed first so the no-pdf_image_rect R2 fallback below has
    # `smooth` available for its raster-threshold scan.
    #
    # Sample the middle 50% of the IMAGE's vertical extent (not the PDF
    # page). This avoids including PDF margin rows that dilute the signal
    # and create V-shaped gradients instead of flat troughs.
    if pdf_image_rect:
        img_top_px = max(0, int(pdf_image_rect.get("top", 0) / 100 * h))
        img_bot_px = min(h, int(pdf_image_rect.get("bottom", 100) / 100 * h))
    else:
        img_top_px = 0
        img_bot_px = h
    img_h = img_bot_px - img_top_px
    row_lo = img_top_px + int(img_h * 0.25)
    row_hi = img_top_px + int(img_h * 0.75)
    body_rows = inv[row_lo:row_hi, :]
    col_profile = body_rows.mean(axis=0)
    smooth = gaussian_filter1d(col_profile, sigma=5)

    # ── R1 → R2: PDF white margin to scanned image ──────────────────
    if pdf_image_rect:
        # Use the exact image placement from the PDF structure.
        # Convert percentages to pixel positions in the profile render.
        r2_left_px = int(pdf_image_rect["left"] / 100 * w)
        r2_right_px = int(pdf_image_rect["right"] / 100 * w)
        # Clamp to valid range
        r2_left_px = max(0, min(w - 1, r2_left_px))
        r2_right_px = max(0, min(w - 1, r2_right_px))
    else:
        # Fallback: raster threshold detection.
        # PDF margins are digitally white: inverted value near 0 (< 2).
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
    # Sample from the central 60% of the scanned image — well within
    # the newspaper content, avoiding binding shadow and facing page
    # sliver on both sides. All measurements stay in PDF page space.
    r2_span = r2_right_px - r2_left_px
    center_lo = r2_left_px + int(r2_span * 0.2)
    center_hi = r2_left_px + int(r2_span * 0.8)
    center_profile = smooth[center_lo:center_hi]

    # Paper baseline: the low end of the content darkness.
    # Use 25th percentile to get paper tone (below text/rule peaks).
    paper_baseline = float(np.percentile(center_profile, 25))
    paper_std = float(np.std(center_profile[center_profile < np.percentile(center_profile, 50)]))
    if paper_std < 1:
        paper_std = 1.0

    # The threshold where shadow ends and paper begins
    shadow_thresh = paper_baseline + 2.5 * paper_std

    # ── R2 → R3 and text_area ────────────────────────────────────
    #
    # Each side is detected independently using multiple signals:
    #   1. Margin region: contiguous near-zero region wider than a gutter
    #   2. Full-height spike: p25 profile shows features dark in 75% of rows
    #   3. Mean profile spike: large darkness in the mean profile
    # Best available signal wins. Results are clamped against each other.

    # ── Additional profile: p25 ───────────────────────────────────
    col_p25 = np.percentile(body_rows, 25, axis=0)
    col_p25 = gaussian_filter1d(col_p25.astype(float), sigma=3)

    # ── Otsu content floor ────────────────────────────────────────
    def _otsu_threshold(values):
        """Otsu's method via histogram for a 1D array."""
        hist, bin_edges = np.histogram(values, bins=64)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        total = hist.sum()
        if total == 0:
            return float(np.median(values))
        sum_total = (hist * bin_centers).sum()
        sum_bg = 0.0
        w_bg = 0
        best_var = -1
        best_thresh = bin_centers[0]
        for i in range(len(hist)):
            w_bg += hist[i]
            if w_bg == 0:
                continue
            w_fg = total - w_bg
            if w_fg == 0:
                break
            sum_bg += hist[i] * bin_centers[i]
            mean_bg = sum_bg / w_bg
            mean_fg = (sum_total - sum_bg) / w_fg
            between_var = w_bg * w_fg * (mean_bg - mean_fg) ** 2
            if between_var > best_var:
                best_var = between_var
                best_thresh = bin_centers[i]
        return float(best_thresh)

    content_floor = _otsu_threshold(center_profile)

    # ── Detection parameters ──────────────────────────────────────
    margin_thresh = min(max(paper_baseline * 0.4, 5), 15)
    min_margin_width = max(int(r2_span * 0.012), 8)
    spike_thresh = content_floor * 2
    search_extent = int(r2_span * 0.25)

    def _find_margin_regions(start_px, end_px):
        """Find contiguous runs below margin_thresh, wider than min_margin_width."""
        regions = []
        x = start_px
        while x <= end_px:
            if smooth[x] < margin_thresh:
                run_start = x
                while x <= end_px and smooth[x] < margin_thresh:
                    x += 1
                if (x - 1) - run_start + 1 >= min_margin_width:
                    regions.append((run_start, x - 1))
            else:
                x += 1
        return regions

    def _detect_edge(edge_px, center_px, direction):
        """Detect R3 and text_area for one side of the page.

        Uses the mean profile to find margin regions (positioning).
        Uses p25 to score confidence (validation only, never positioning).
        Mean spikes prevent boundaries sitting outside them (safety net).

        direction: +1 = left edge (walk rightward), -1 = right edge
        Returns (r3_px, text_area_px, method).
        """
        if direction > 0:
            search_end = min(edge_px + search_extent, center_px)
        else:
            search_end = max(edge_px - search_extent, center_px)

        r3 = edge_px
        ta = edge_px
        method = 'none'

        # ── Primary: margin region from mean profile ──────────────
        if direction > 0:
            regions = _find_margin_regions(edge_px, search_end)
            if regions:
                ms, me = regions[0]
                r3 = ms
                ta = me + 1
                method = 'margin'
        else:
            regions = _find_margin_regions(search_end, edge_px)
            if regions:
                ms, me = regions[-1]
                r3 = me
                ta = ms - 1
                method = 'margin'

        # ── Safety net: mean spike boundary ───────────────────────
        # If there's a mean spike (> 2x content_floor) between the
        # edge and our result, R3/TA cannot sit outside it.
        # This prevents boundaries landing in the binding shadow.
        spike_inner = edge_px
        x = edge_px
        end = search_end
        while (direction > 0 and x < end) or (direction < 0 and x > end):
            if smooth[x] >= spike_thresh:
                while ((direction > 0 and x < end) or
                       (direction < 0 and x > end)):
                    if smooth[x] < spike_thresh:
                        break
                    x += direction
                spike_inner = x
            else:
                x += direction

        if direction > 0:
            r3 = max(r3, spike_inner)
            ta = max(ta, spike_inner)
        else:
            r3 = min(r3, spike_inner)
            ta = min(ta, spike_inner)

        if method == 'none' and spike_inner != edge_px:
            method = 'spike'

        # ── Fallback: ta = r3 ─────────────────────────────────────
        if method == 'none':
            ta = r3

        return r3, ta, method

    # ── Run detection on both sides ───────────────────────────────
    r3_left_px, text_left_px, left_method = _detect_edge(r2_left_px, center_lo, +1)
    r3_right_px, text_right_px, right_method = _detect_edge(r2_right_px, center_hi, -1)

    # ── Enforce hierarchy ─────────────────────────────────────────
    r3_left_px = max(r3_left_px, r2_left_px)
    r3_right_px = min(r3_right_px, r2_right_px)
    text_left_px = max(text_left_px, r3_left_px)
    text_right_px = min(text_right_px, r3_right_px)

    # ── Build bounding boxes as % of page dimensions ─────────────────
    def bbox(left_px, right_px):
        return {
            "left": round(left_px / w * 100, 2),
            "right": round(right_px / w * 100, 2),
            "top": round(row_lo / h * 100, 2),
            "bottom": round(row_hi / h * 100, 2),
        }

    # R2: prefer the exact PDF structural coordinates.
    if pdf_image_rect:
        r2 = {
            "left": round(pdf_image_rect["left"], 2),
            "right": round(pdf_image_rect["right"], 2),
            "top": round(pdf_image_rect["top"], 2),
            "bottom": round(pdf_image_rect["bottom"], 2),
        }
    else:
        r2 = bbox(r2_left_px, r2_right_px)

    # R3 and text_area as percentages, clamped to enforce hierarchy
    # against the authoritative R2 values (avoids pixel rounding drift).
    r3_box = bbox(r3_left_px, r3_right_px)
    r3_box["left"] = max(r3_box["left"], r2["left"])
    r3_box["right"] = min(r3_box["right"], r2["right"])

    text_area = bbox(text_left_px, text_right_px)
    text_area["left"] = max(text_area["left"], r3_box["left"])
    text_area["right"] = min(text_area["right"], r3_box["right"])

    conf_map = {'margin': 0.9, 'spike': 0.8, 'none': 0.3}
    text_left_conf = conf_map[left_method]
    text_right_conf = conf_map[right_method]
    text_area["left_confidence"] = text_left_conf
    text_area["right_confidence"] = text_right_conf

    if clean_side == "left":
        text_area["clean_side_confidence"] = text_left_conf
        text_area["binding_side_confidence"] = text_right_conf
    else:
        text_area["clean_side_confidence"] = text_right_conf
        text_area["binding_side_confidence"] = text_left_conf

    # ── Analysis profile for visualization ─────────────────────────
    # Downsample the smooth darkness profile to 200 points (every 0.5%
    # of page width). This is the data the viewer charts.
    n_samples = 200
    profile_xs = np.linspace(0, w - 1, n_samples).astype(int)
    profile_chart = [
        {"pct": round(x / w * 100, 2), "val": round(float(smooth[x]), 2)}
        for x in profile_xs
    ]

    return {
        "r2": r2,
        "r3": r3_box,
        "text_area": text_area,
        "binding_side": binding_side,
        "clean_side": clean_side,
        "page_type": page_type,
        "binding_confirmed": binding_confirmed,
        "paper_baseline": round(paper_baseline, 1),
        "paper_std": round(paper_std, 1),
        "shadow_thresh": round(shadow_thresh, 1),
        "content_floor": round(content_floor, 1),
        "profile_chart": profile_chart,
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

    # ── Extract R2 from PDF structure ────────────────────────────────
    # The image placement rectangle in the PDF gives the exact position
    # of the scanned image within the PDF page — far more accurate than
    # raster analysis which is affected by smoothing and threshold choice.
    pdf_image_rect = None
    try:
        images = page.get_images(full=True)
        if images:
            rects_list = page.get_image_rects(images[0][0])
            if rects_list:
                ir = rects_list[0]
                # Convert from PDF points to percentage of page
                pdf_image_rect = {
                    "left": max(0, ir.x0 / pw * 100),
                    "right": min(100, ir.x1 / pw * 100),
                    "top": max(0, ir.y0 / ph * 100),
                    "bottom": min(100, ir.y1 / ph * 100),
                }
    except Exception:
        pass

    doc.close()

    # ── Detect nested rectangles ─────────────────────────────────────
    rects = find_rectangles(inv, h, w, gazette_page=gazette_page,
                            pdf_image_rect=pdf_image_rect)

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

        # Analysis chart data for viewer
        "profile_chart": rects.get("profile_chart"),
        "shadow_thresh": rects.get("shadow_thresh", 0),
        "content_floor": rects.get("content_floor", 0),
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
        print("  Quality: good")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python page_profile.py <page.pdf>")
        sys.exit(1)
    prof = profile_page(sys.argv[1])
    print_profile(prof)
