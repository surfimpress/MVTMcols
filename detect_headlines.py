"""
Detect multi-column headlines in newspaper pages.

A headline spans 2+ columns, meaning the column gutters (white gaps)
disappear within the headline region. Body text respects the gutters.

Approach:
  1. Render the page at moderate DPI
  2. Use detected column boundaries to know where gutters should be
  3. Scan vertical blocks: for each block, check how many gutters
     have content crossing them (gutter is "filled")
  4. Contiguous vertical runs of filled gutters = headline regions
  5. Determine the horizontal extent (which columns the headline spans)

Does NOT detect:
  - Single-column headlines (they don't cross gutters)
  - Headlines already captured as ad regions (caller should filter)
"""

import fitz
import numpy as np
from scipy.ndimage import gaussian_filter1d


def detect_headlines(pdf_path, column_boundaries, page_number=0,
                     dpi=150, ad_zones=None, r2_top_pct=None, r2_bottom_pct=None):
    """
    Detect multi-column headline regions.

    Args:
        pdf_path: Path to the PDF file
        column_boundaries: List of x_pct positions (the detected column
            boundaries from clustering). These define where gutters are.
        page_number: Zero-indexed page within the PDF
        dpi: Render resolution
        ad_zones: List of (x1_pct, x2_pct, y1_pct, y2_pct) ad regions
            to exclude from headline detection

    Returns:
        List of headline dicts, each with:
            x1_pct, x2_pct: horizontal extent (% of page)
            y1_pct, y2_pct: vertical extent (% of page)
            cols_spanned: number of columns the headline spans
            confidence: 0-1 score
    """
    if len(column_boundaries) < 2:
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
    inv = 255.0 - grey  # dark = high

    # Estimate pitch from boundary gaps
    pitch_estimate = None
    if len(column_boundaries) >= 2:
        gaps = [column_boundaries[i+1] - column_boundaries[i]
                for i in range(len(column_boundaries) - 1)]
        pitch_estimate = float(np.median(gaps))

    # Convert boundary positions to pixel x coordinates.
    # These are all detected column rules — every one is a gutter
    # between adjacent columns. Use all of them.
    gutter_xs = [int(b / 100 * w) for b in column_boundaries]
    if not gutter_xs:
        return [], [], {}

    ad_z = ad_zones or []

    # Parameters
    block_h = max(int(h * 0.01), 10)  # ~1% of page height per block
    gutter_hw = max(int(w * 0.005), 4)  # half-width of gutter sample zone

    # Scan the page in vertical blocks
    n_blocks = h // block_h
    n_gutters = len(gutter_xs)

    # First pass: measure gutter darkness at every block
    # Use half-block step to double the vertical resolution.
    # This catches fills that straddle block boundaries.
    step = block_h // 2
    n_blocks = (h - block_h) // step + 1
    gutter_dark = np.zeros((n_blocks, n_gutters))

    for bi in range(n_blocks):
        y1 = bi * step
        y2 = min(y1 + block_h, h)
        band = inv[y1:y2, :]
        for gi, gx in enumerate(gutter_xs):
            x1 = max(0, gx - gutter_hw)
            x2 = min(w, gx + gutter_hw)
            gutter_dark[bi, gi] = float(band[:, x1:x2].mean())

    # Compute per-gutter baseline
    gutter_baseline = np.percentile(gutter_dark, 25, axis=0)
    gutter_baseline = np.maximum(gutter_baseline, 5)

    # Fill threshold
    gutter_filled = np.zeros((n_blocks, n_gutters), dtype=bool)
    for gi in range(n_gutters):
        fill_thresh = max(gutter_baseline[gi] * 2, gutter_baseline[gi] + 40)
        gutter_filled[:, gi] = gutter_dark[:, gi] > fill_thresh

    # Find headline regions: contiguous vertical runs where 2+ adjacent
    # gutters are simultaneously filled.
    # Build a map of "spanning" blocks: blocks where consecutive gutters
    # are filled, indicating text crossing those column boundaries.
    headlines = []
    min_span_blocks = 2

    # For each pair of adjacent gutters, find vertical runs where both
    # or a contiguous group are filled
    # Actually, simpler: for each block, find groups of consecutive
    # filled gutters. Each group represents a headline span.

    for bi in range(n_blocks):
        filled = gutter_filled[bi]
        if not any(filled):
            continue

        # Find runs of consecutive filled gutters
        groups = []
        start = None
        for gi in range(n_gutters):
            if filled[gi]:
                if start is None:
                    start = gi
            else:
                if start is not None:
                    groups.append((start, gi - 1))
                    start = None
        if start is not None:
            groups.append((start, n_gutters - 1))

        for g_start, g_end in groups:
            if g_end - g_start < 1:
                continue  # need at least 2 filled gutters = 3 columns
            # Record this block's spanning info
            # We'll merge vertically in the next step

    # Better approach: for each gutter, find vertical runs of "filled",
    # then merge horizontally across gutters.
    # Start with per-gutter vertical runs.
    gutter_runs = []  # list of (gutter_idx, block_start, block_end)
    for gi in range(n_gutters):
        in_run = False
        run_start = 0
        for bi in range(n_blocks):
            if gutter_filled[bi, gi]:
                if not in_run:
                    run_start = bi
                    in_run = True
            else:
                if in_run:
                    run_len = bi - run_start
                    if run_len >= min_span_blocks:
                        gutter_runs.append((gi, run_start, bi - 1))
                    in_run = False
        if in_run:
            run_len = n_blocks - run_start
            if run_len >= min_span_blocks:
                gutter_runs.append((gi, run_start, n_blocks - 1))

    if not gutter_runs:
        return []

    # Merge overlapping runs across adjacent gutters into headline regions
    # Sort by block_start, then gutter_idx
    gutter_runs.sort(key=lambda r: (r[1], r[0]))

    merged = []
    for gi, b_start, b_end in gutter_runs:
        # Try to merge with an existing headline
        merged_flag = False
        for m in merged:
            # Overlapping vertically and adjacent horizontally?
            if (b_start <= m["b_end"] + 1 and b_end >= m["b_start"] - 1 and
                    gi >= m["g_min"] - 1 and gi <= m["g_max"] + 1):
                m["g_min"] = min(m["g_min"], gi)
                m["g_max"] = max(m["g_max"], gi)
                m["b_start"] = min(m["b_start"], b_start)
                m["b_end"] = max(m["b_end"], b_end)
                merged_flag = True
                break
        if not merged_flag:
            merged.append({
                "g_min": gi, "g_max": gi,
                "b_start": b_start, "b_end": b_end,
            })

    # Convert merged regions to output format
    for m in merged:
        # Horizontal extent: the headline spans from one pitch before
        # the first filled gutter to one pitch after the last.
        # gutter_xs indices map directly to column_boundaries indices.
        x1_idx = m["g_min"]
        x2_idx = m["g_max"]
        cols_spanned = x2_idx - x1_idx + 2

        # Extend one column width outward on each side.
        # If there's a boundary to the left/right, use it.
        # Otherwise, extend by one pitch from the outermost filled gutter.
        if x1_idx > 0:
            x1_pct = column_boundaries[x1_idx - 1]
        else:
            x1_pct = max(0, column_boundaries[x1_idx] - (pitch_estimate or 11))

        if x2_idx < len(column_boundaries) - 1:
            x2_pct = column_boundaries[x2_idx + 1]
        else:
            x2_pct = min(100, column_boundaries[x2_idx] + (pitch_estimate or 11))

        # Vertical extent: start from the gutter-fill blocks, then
        # extend up and down to capture the full text height.
        # Check adjacent rows for significant darkness at the same
        # horizontal extent.
        y1_px = m["b_start"] * step
        y2_px = min((m["b_end"] + 1) * step, h)
        x1_px = int(x1_pct / 100 * w)
        x2_px = int(x2_pct / 100 * w)
        extend_thresh = 15  # darkness above this = still part of headline

        # Extend upward
        while y1_px > 0:
            row_above = inv[max(0, y1_px - 3):y1_px, x1_px:x2_px]
            if row_above.size > 0 and float(row_above.mean()) > extend_thresh:
                y1_px -= 3
            else:
                break

        # Extend downward
        while y2_px < h:
            row_below = inv[y2_px:min(h, y2_px + 3), x1_px:x2_px]
            if row_below.size > 0 and float(row_below.mean()) > extend_thresh:
                y2_px += 3
            else:
                break

        y1_pct = round(max(0, y1_px) / h * 100, 1)
        y2_pct = round(min(y2_px, h) / h * 100, 1)

        # Skip if ANY overlap with an ad zone
        in_ad = False
        for az in ad_z:
            # Rectangles overlap if they are not separated on either axis
            if (x1_pct < az[1] and x2_pct > az[0] and
                    y1_pct < az[3] and y2_pct > az[2]):
                in_ad = True
                break
        if in_ad:
            continue

        # Confidence based on how many gutters are filled and how
        # consistently they're filled across the vertical extent
        total_blocks = m["b_end"] - m["b_start"] + 1
        total_gutters = m["g_max"] - m["g_min"] + 1
        fill_count = 0
        for bi in range(m["b_start"], m["b_end"] + 1):
            for gi in range(m["g_min"], m["g_max"] + 1):
                if gutter_filled[bi, gi]:
                    fill_count += 1
        fill_ratio = fill_count / (total_blocks * total_gutters)

        confidence = round(min(1.0, fill_ratio * (cols_spanned / 3)), 2)

        if cols_spanned >= 2:
            headlines.append({
                "x1_pct": round(x1_pct, 1),
                "x2_pct": round(x2_pct, 1),
                "y1_pct": y1_pct,
                "y2_pct": y2_pct,
                "cols_spanned": cols_spanned,
                "confidence": confidence,
            })

    # Remove headlines that overlap with each other — keep the one
    # with higher confidence (or larger area if tied).
    headlines.sort(key=lambda hl: (-hl["confidence"],
                                   -(hl["x2_pct"] - hl["x1_pct"]) *
                                    (hl["y2_pct"] - hl["y1_pct"])))
    kept = []
    for hl in headlines:
        overlaps = False
        for k in kept:
            if (hl["x1_pct"] < k["x2_pct"] and hl["x2_pct"] > k["x1_pct"] and
                    hl["y1_pct"] < k["y2_pct"] and hl["y2_pct"] > k["y1_pct"]):
                overlaps = True
                break
        if not overlaps:
            kept.append(hl)

    # Constrain to within R2 vertical extent (inside the scanned image)
    y_min = r2_top_pct if r2_top_pct is not None else 0
    y_max = r2_bottom_pct if r2_bottom_pct is not None else 100

    # ── Content classification ────────────────────────────────────
    # Analyse each gutter-fill region to decide whether it looks like
    # a headline. Rejects (too-tall, graphics, low-variance, body-text-
    # dominated) are simply discarded — large_type is the authority on
    # "is this a headline" and consumes gutter_fills as a separate signal.
    #
    # Headlines: short (< 9% of page height), text-like content
    #   (high row-variance = alternating text/gap rows), low graphics
    max_headline_height = 9.0  # % of page
    graphics_thresh = 0.35     # if >35% of region is very dark = graphics

    classified_headlines = []

    for hl in kept:
        if hl["y1_pct"] < y_min or hl["y2_pct"] > y_max:
            continue
        if hl["cols_spanned"] < 2:
            continue

        height_pct = hl["y2_pct"] - hl["y1_pct"]

        # Extract the region from the image, inset by 5% on each side
        # horizontally to exclude column rules and gutter noise at edges
        ry1 = int(hl["y1_pct"] / 100 * h)
        ry2 = int(hl["y2_pct"] / 100 * h)
        rx1_full = int(hl["x1_pct"] / 100 * w)
        rx2_full = int(hl["x2_pct"] / 100 * w)
        region_w = rx2_full - rx1_full
        inset = max(int(region_w * 0.05), 2)
        rx1 = rx1_full + inset
        rx2 = rx2_full - inset
        region = inv[ry1:ry2, rx1:rx2]

        if region.size == 0:
            continue

        # Row-variance and graphics density for illustration detection
        row_means = region.mean(axis=1)
        row_var = float(np.std(row_means)) if len(row_means) > 1 else 0
        very_dark = float(np.mean(region > 150))

        # ── Body text vs headline classification ──────────────────
        # Sample from the middle zone of each column independently,
        # not across the full width. Adjacent columns often have text
        # lines at different vertical positions — averaging across
        # both muddies the signal. Sampling each column's centre gives
        # a clean single-column rhythm.
        #
        # Estimate column centres from the region width and pitch.
        # Sample a strip ~30% of pitch wide from each column's centre.
        region_w_pct = hl["x2_pct"] - hl["x1_pct"]
        approx_pitch_px = int(pitch_estimate / 100 * w) if pitch_estimate else int(region_w_pct / hl["cols_spanned"] / 100 * w)
        sample_hw = max(int(approx_pitch_px * 0.15), 5)  # half-width of sample strip

        # Build column centre positions within the region
        n_cols = hl["cols_spanned"]
        col_width_px = (rx2 - rx1) // max(n_cols, 1)
        col_centres = [rx1 + int((i + 0.5) * col_width_px) for i in range(n_cols)]

        window = 20
        body_rows = 0
        headline_rows = 0
        total_rows = ry2 - ry1

        if total_rows > window:
            for col_cx in col_centres:
                # Extract a narrow vertical strip from this column's centre
                sx1 = max(0, col_cx - sample_hw)
                sx2 = min(w, col_cx + sample_hw)
                strip = inv[ry1:ry2, sx1:sx2].mean(axis=1)

                for start in range(0, len(strip) - window, window // 2):
                    chunk = strip[start:start + window]
                    peaks_v, troughs_v, peak_pos = [], [], []
                    for j in range(1, len(chunk) - 1):
                        if chunk[j] > chunk[j-1] and chunk[j] > chunk[j+1]:
                            peaks_v.append(chunk[j])
                            peak_pos.append(j)
                        if chunk[j] < chunk[j-1] and chunk[j] < chunk[j+1]:
                            troughs_v.append(chunk[j])

                    if len(peaks_v) >= 2 and troughs_v:
                        mean_spacing = float(np.mean(np.diff(peak_pos)))
                        # Body text has MULTIPLE troughs near zero at
                        # regular intervals. A single zero in a headline
                        # (word gap) should not trigger body text.
                        near_zero_troughs = sum(1 for t in troughs_v if t < 10)
                        most_troughs_zero = near_zero_troughs >= len(troughs_v) * 0.6

                        if most_troughs_zero and mean_spacing < 7:
                            body_rows += window
                        elif mean_spacing >= 7 and not most_troughs_zero:
                            headline_rows += window

        total_samples = total_rows * max(len(col_centres), 1)
        body_frac = body_rows / max(total_samples, 1)
        headline_frac = headline_rows / max(total_samples, 1)

        # Region-level decision
        if body_frac > 0.7:
            is_headline = False  # reject — mostly body text
        elif height_pct >= max_headline_height:
            is_headline = False  # too tall
        elif very_dark >= graphics_thresh:
            is_headline = False  # graphics/illustration
        elif row_var <= 10:
            is_headline = False  # low variance (illustration)
        else:
            is_headline = True

        entry = {
            "x1_pct": hl["x1_pct"],
            "x2_pct": hl["x2_pct"],
            "y1_pct": hl["y1_pct"],
            "y2_pct": hl["y2_pct"],
            "cols_spanned": hl["cols_spanned"],
            "confidence": hl["confidence"],
        }

        if is_headline:
            classified_headlines.append(entry)
        # else: discard. Ad promotion was removed — large_type is the
        # authority on "is this a headline"; gutter-fill is just a signal
        # the caller can use, FPs are tolerated and filtered downstream.

    classified_headlines.sort(key=lambda hl: (hl["y1_pct"], hl["x1_pct"]))

    # Build per-region analysis charts: horizontal-blur row profile
    # within each detected region. Each row is averaged horizontally
    # across the region's width (motion blur effect), sampled at
    # every pixel row for full resolution to show body text rhythm.
    # Body text: regular fine stripes. Headlines: thick sparse peaks.
    for region_entry in classified_headlines:
        ry1 = int(region_entry["y1_pct"] / 100 * h)
        ry2 = int(region_entry["y2_pct"] / 100 * h)
        rx1_full = int(region_entry["x1_pct"] / 100 * w)
        rx2_full = int(region_entry["x2_pct"] / 100 * w)
        rw = rx2_full - rx1_full
        inset = max(int(rw * 0.05), 2)
        rx1 = rx1_full + inset
        rx2 = rx2_full - inset
        if ry2 <= ry1 or rx2 <= rx1:
            continue
        # Sample from the middle zone of each column independently,
        # then average the per-column profiles. This avoids merging
        # misaligned text lines from adjacent columns.
        n_cols = region_entry.get("cols_spanned", 2)
        rw = rx2 - rx1
        col_w = rw // max(n_cols, 1)
        sample_hw = max(int(col_w * 0.15), 5)
        col_profiles = []
        for ci in range(n_cols):
            ccx = rx1 + int((ci + 0.5) * col_w)
            sx1 = max(rx1, ccx - sample_hw)
            sx2 = min(rx2, ccx + sample_hw)
            if sx2 > sx1:
                col_profiles.append(inv[ry1:ry2, sx1:sx2].mean(axis=1))
        if not col_profiles:
            col_profiles = [inv[ry1:ry2, rx1:rx2].mean(axis=1)]
        # Build a chart per column — each column sampled independently
        col_charts = []
        for cp in col_profiles:
            chart = []
            for i, val in enumerate(cp):
                y_pct = round((ry1 + i) / h * 100, 2)
                chart.append({"y_pct": y_pct, "val": round(float(val), 1)})

            # Mark body text segments using near-zero trough detection
            is_body = [False] * len(chart)
            vals_arr = np.array([p["val"] for p in chart])
            win = 20
            if len(vals_arr) > win:
                for start in range(0, len(vals_arr) - win, win // 2):
                    chunk = vals_arr[start:start + win]
                    peaks_v, peak_pos, troughs_v = [], [], []
                    for j in range(1, len(chunk) - 1):
                        if chunk[j] > chunk[j-1] and chunk[j] > chunk[j+1]:
                            peaks_v.append(chunk[j])
                            peak_pos.append(j)
                        if chunk[j] < chunk[j-1] and chunk[j] < chunk[j+1]:
                            troughs_v.append(chunk[j])
                    if len(peaks_v) >= 2 and troughs_v:
                        spacing = float(np.mean(np.diff(peak_pos)))
                        near_zero = sum(1 for t in troughs_v if t < 10)
                        if near_zero >= len(troughs_v) * 0.6 and spacing < 7:
                            for k in range(start, min(start + win, len(chart))):
                                is_body[k] = True

            for idx, b in enumerate(is_body):
                chart[idx]["body"] = b
            col_charts.append(chart)

        # Store first column chart as row_chart (backwards compat)
        # and all charts as col_charts
        region_entry["row_chart"] = col_charts[0] if col_charts else []
        if len(col_charts) > 1:
            region_entry["col_charts"] = col_charts

    # Also include per-gutter fill state for the overlay
    gutter_fills = []
    for gi in range(n_gutters):
        gutter_pct = round(gutter_xs[gi] / w * 100, 1)
        filled_ranges = []
        in_fill = False
        fill_start = 0
        for bi in range(n_blocks):
            if gutter_filled[bi, gi]:
                if not in_fill:
                    fill_start = bi * step
                    in_fill = True
            else:
                if in_fill:
                    filled_ranges.append({
                        "y1_pct": round(fill_start / h * 100, 1),
                        "y2_pct": round(bi * step / h * 100, 1),
                    })
                    in_fill = False
        if in_fill:
            filled_ranges.append({
                "y1_pct": round(fill_start / h * 100, 1),
                "y2_pct": round(min(n_blocks * step, h) / h * 100, 1),
            })
        if filled_ranges:
            gutter_fills.append({"x_pct": gutter_pct, "ranges": filled_ranges})

    analysis_data = {
        "gutter_fills": gutter_fills,
    }

    return classified_headlines, analysis_data


def assemble_headlines_from_charts(body_text_charts, columns_meta,
                                    ad_zones=None,
                                    gutter_fills=None,
                                    bar_subbars=None,
                                    h_rules=None,
                                    val_threshold=80,
                                    min_run_rows=11,
                                    run_mean_min=130,
                                    run_max_min=200,
                                    line_height_multiplier=1.6,
                                    y_overlap_min=0.5,
                                    rescue_overlap_min=0.3,
                                    promote_subbar_mean_min=40,
                                    promote_overlap_min=0.5,
                                    h_rule_strength_min=80):
    """
    Build headline regions directly from per-column body_text_charts.

    Replaces the gutter-fill primitive with an evidence-based approach:
      Step A — for each column's chart, find contiguous rows where the
        horizontal-blur value exceeds val_threshold. Body text rhythms
        produce bursts ≤ 2-3 rows; large-type lines produce sustained
        runs ≥ ~7 rows. min_run_rows separates them.
      Strength filter — tiered by run length:
        • short runs (<30 rows) overlap body-text territory, so they
          must clear the strict absolute thresholds (run_mean_min /
          run_max_min). No adaptation here — admitting body-text
          bursts is the failure mode.
        • medium / long runs (≥30 rows) cannot be body text. Their
          threshold relaxes against the column's own brightness
          distribution (per-column adaptive). A multi-line headline
          fused into one 90-row run with mean ~125 still passes.
      Step B — within a column, merge runs separated by gaps that are
        smaller than ~1.6× the run height. This collapses a 2-line
        headline (line / inter-line gap / line) into one block.
      Step C — across adjacent columns, merge blocks whose y-extents
        overlap by ≥ 50% of the shorter block. This produces multi-
        column headlines. Single-column blocks survive as 1-col
        headlines (newspapers contain many of these).
      Step D (rescue) — for each assembled block, look at adjacent
        columns for any *raw* run (regardless of strength) whose
        y-range overlaps the block by ≥ rescue_overlap_min. If found,
        extend the block into that column. Catches faint partners
        like an edge column whose ink is dim but rhythmically aligned
        with a stronger neighbour. When `gutter_fills` is supplied,
        a fill at the rescue boundary covering the block y-extent
        relaxes the per-run-coverage requirement: gutter ink crossing
        the boundary is independent evidence the structure spans
        both columns, so a weaker raw run is accepted.

    Args:
        body_text_charts: list of {col_idx, x_pct, chart: [{y_pct, val,
            body}, ...]} as produced by detect_body_text.
        columns_meta: list of {index, left_vw, right_vw}.
        ad_zones: list of (x1, x2, y1, y2) to skip.
        gutter_fills: optional list of {x_pct, ranges:[{y1_pct,y2_pct}]}
            from detect_headlines, used as a confirmation signal in
            Step D rescue (FPs are tolerated — chart evidence still
            required, gutter just relaxes thresholds).
        val_threshold: brightness above which a row is "bright".
        min_run_rows: minimum contiguous bright rows for a candidate.
            Body-text peaks are 7-9 rows; large-type lines are 14+.
        run_mean_min: strict absolute mean threshold for short runs.
            Long runs use a per-column adaptive baseline scaled down.
        run_max_min: strict absolute peak threshold for short runs.
        line_height_multiplier: maximum gap between two bright runs in
            the same column (relative to the larger run's height) for
            them to be treated as one headline block.
        y_overlap_min: minimum y-overlap (as fraction of shorter block)
            required for two adjacent-column blocks to merge.
        rescue_overlap_min: weaker overlap threshold used in step D.

    Returns:
        list of {x1_pct, x2_pct, y1_pct, y2_pct, cols_spanned,
                 source: 'chart'} sorted top-to-bottom, left-to-right.
    """
    col_by_idx = {c['index']: c for c in columns_meta}
    ad_z = ad_zones or []

    # Map each gutter_fill to the placed-column boundary it sits at.
    # Returns dict {(low_col_idx, high_col_idx): [(y1_pct, y2_pct), ...]}.
    # Tolerance is needed because detect_headlines runs on raw boundaries
    # while columns_meta reflects post-validation placed columns — the
    # x positions are close but rarely identical.
    gutter_map = {}
    # `gutter_check_active` controls whether the vertical-rule rule is
    # enforced. True if the caller supplied gutter_fills (even an empty
    # list — that means "detect_headlines ran and found no fills",
    # so cross-column merges should be blocked). None means the caller
    # didn't have gutter data; fall back to legacy y-overlap-only merge.
    gutter_check_active = gutter_fills is not None
    if gutter_fills:
        cols_sorted = sorted(columns_meta, key=lambda c: c['left_vw'])
        boundaries = [(
            (cols_sorted[i]['right_vw'] + cols_sorted[i + 1]['left_vw']) / 2,
            cols_sorted[i]['index'],
            cols_sorted[i + 1]['index'],
        ) for i in range(len(cols_sorted) - 1)]
        for gf in gutter_fills:
            gx = gf.get('x_pct')
            if gx is None or not boundaries:
                continue
            best = min(boundaries, key=lambda b: abs(b[0] - gx))
            if abs(best[0] - gx) > 2.0:  # tolerance: 2% of page width
                continue
            key = (min(best[1], best[2]), max(best[1], best[2]))
            ranges = [(r['y1_pct'], r['y2_pct'])
                      for r in gf.get('ranges', [])]
            gutter_map.setdefault(key, []).extend(ranges)

    def _gutter_supports(col_a, col_b, y1, y2):
        """True if a gutter_fill at the boundary between col_a and col_b
        covers more than half of the block y-range [y1, y2].

        Encodes the rule: a vertical column rule between two columns
        is a sure-stop signal — content cannot span across an intact
        rule. gutter_fills mark where the rule is broken (content
        crossing the gutter); only there can a cross-column merge
        happen."""
        key = (min(col_a, col_b), max(col_a, col_b))
        fills = gutter_map.get(key)
        if not fills:
            return False
        block_h = y2 - y1
        if block_h <= 0:
            return False
        for fy1, fy2 in fills:
            ov = min(y2, fy2) - max(y1, fy1)
            if ov > 0 and ov / block_h > 0.5:
                return True
        return False

    # ── First pass: collect raw runs and per-column distribution ──
    # Per-column adaptive baseline. The midpoint between p50 (the
    # paper / inter-line level) and p90 (the column's typical peak)
    # gives a column-aware "this is bright for this column" bar that
    # accommodates dim edge columns and dense interior columns alike.
    raw_runs_by_col = {}     # col_idx -> [{s,e,n,mean,max}, ...]
    chart_by_col = {}
    x_sample_by_col = {}
    adaptive_by_col = {}     # col_idx -> (adaptive_mean, adaptive_max)

    for chart_obj in body_text_charts:
        col_idx = chart_obj['col_idx']
        chart = chart_obj['chart']
        if not chart:
            continue
        chart_by_col[col_idx] = chart
        x_sample_by_col[col_idx] = chart_obj.get('x_pct', 0)

        vals_sorted = sorted(pt['val'] for pt in chart)
        nv = len(vals_sorted)
        col_p50 = vals_sorted[nv // 2]
        col_p90 = vals_sorted[min(nv - 1, int(nv * 0.9))]
        # Floors prevent quiet/blank columns from dropping thresholds
        # too far. Ceilings prevent very bright columns from raising
        # them above what real headlines can clear.
        adaptive_mean = max(80, min(140, (col_p50 + col_p90) / 2))
        adaptive_max = max(150, min(220, col_p90 * 0.95))
        adaptive_by_col[col_idx] = (adaptive_mean, adaptive_max)

        raw_runs = []
        in_run = False
        run_start = 0

        def _push(s, e):
            vals = [chart[i]['val'] for i in range(s, e + 1)]
            raw_runs.append({
                's': s, 'e': e,
                'n': e - s + 1,
                'mean': sum(vals) / len(vals),
                'max': max(vals),
            })

        for i, pt in enumerate(chart):
            bright = pt['val'] > val_threshold
            if bright and not in_run:
                run_start = i
                in_run = True
            elif not bright and in_run:
                if i - run_start >= min_run_rows:
                    _push(run_start, i - 1)
                in_run = False
        if in_run and len(chart) - run_start >= min_run_rows:
            _push(run_start, len(chart) - 1)

        raw_runs_by_col[col_idx] = raw_runs

    def passes_strength(run, adaptive_mean, adaptive_max):
        n = run['n']
        if n < 30:
            # Short runs overlap body-text territory: strict absolutes.
            return run['mean'] >= run_mean_min or run['max'] >= run_max_min
        if n < 60:
            # Medium: per-column-aware, slightly relaxed.
            return (run['mean'] >= adaptive_mean * 0.95 or
                    run['max'] >= adaptive_max * 0.95)
        # Long (≥60 rows): cannot be body text. Multi-line headlines
        # fuse here when inter-line gaps don't drop below val_threshold.
        return (run['mean'] >= adaptive_mean * 0.7 or
                run['max'] >= adaptive_max * 0.85)

    # ── Promotion step (between A and B) ──────────────────────────
    # A chart raw run that fails the strength filter may still represent
    # a real headline line if (a) the bar_width strip has ink at this y
    # in this column (mean>40 in any width) AND (b) an adjacent column
    # has independent evidence of a headline at the same y (a chart raw
    # run, or a bar_width sub-bar). The strength filter is fragile to
    # asymmetric letter layout that fragments the narrow central strip
    # mean below the threshold even though chart's val>80,n≥11 caught
    # the line cleanly.
    #
    # False-positive guards (each independently necessary):
    #   1. h_rule barrier — reject if any h_rule (strength≥threshold)
    #      sits inside the candidate run's y-range. Also applied to the
    #      partner. Rules are the recurring failure mode for promotions
    #      like this.
    #   2. ad-zone skip — candidate's centre must not fall inside an ad.
    #   3. bar_width sub-bar in THIS col with mean>40 covering ≥50% of
    #      the candidate run — confirms ink density (rules out chart
    #      noise, blurred edges, overscan artefacts).
    #   4. cross-col aligned partner — adjacent col has a chart raw run
    #      OR bar sub-bar (mean>40) overlapping ≥50% of shorter run, and
    #      the partner is not itself an h_rule.
    # Per-column index: an h_rule in col 2 says nothing about whether a
    # candidate in col 5 is on a rule. h_rules are detected per-column
    # strip, so the barrier must be applied per-column too.
    h_rule_y_by_col = {}
    if h_rules:
        for hr in h_rules:
            if hr.get('strength', 0) >= h_rule_strength_min:
                h_rule_y_by_col.setdefault(hr.get('col_idx'), []).append(
                    hr['y_pct'])

    def _hits_h_rule(col_idx, y1, y2):
        ypcs = h_rule_y_by_col.get(col_idx, ())
        return any(y1 <= ypc <= y2 for ypc in ypcs)

    bar_subbars_safe = bar_subbars or {}

    def _has_subbar_in_col(col_idx, y1, y2):
        subs = bar_subbars_safe.get(col_idx, [])
        rh = y2 - y1
        if rh <= 0:
            return False
        for sb in subs:
            if sb.get('mean', 0) < promote_subbar_mean_min:
                continue
            ov = min(y2, sb['y2_pct']) - max(y1, sb['y1_pct'])
            if ov > 0 and ov / rh >= promote_overlap_min:
                return True
        return False

    def _has_aligned_partner(col_idx, y1, y2):
        rh = y2 - y1
        if rh <= 0:
            return False
        for adj in (col_idx - 1, col_idx + 1):
            if adj not in raw_runs_by_col:
                continue
            adj_chart = chart_by_col[adj]
            # Partner type 1: chart raw run in adjacent col
            for r in raw_runs_by_col[adj]:
                ay1 = adj_chart[r['s']]['y_pct']
                ay2 = adj_chart[r['e']]['y_pct']
                ah = ay2 - ay1
                if ah <= 0:
                    continue
                ov = min(y2, ay2) - max(y1, ay1)
                if ov <= 0:
                    continue
                if ov / min(rh, ah) < promote_overlap_min:
                    continue
                if _hits_h_rule(adj, ay1, ay2):
                    continue
                return True
            # Partner type 2: bar_width sub-bar in adjacent col
            for sb in bar_subbars_safe.get(adj, []):
                if sb.get('mean', 0) < promote_subbar_mean_min:
                    continue
                ay1, ay2 = sb['y1_pct'], sb['y2_pct']
                ah = ay2 - ay1
                if ah <= 0:
                    continue
                ov = min(y2, ay2) - max(y1, ay1)
                if ov <= 0:
                    continue
                if ov / min(rh, ah) < promote_overlap_min:
                    continue
                if _hits_h_rule(adj, ay1, ay2):
                    continue
                return True
        return False

    promoted_by_col = {ci: set() for ci in raw_runs_by_col}
    if bar_subbars is not None:
        for col_idx, raw_runs in raw_runs_by_col.items():
            adaptive = adaptive_by_col[col_idx]
            chart = chart_by_col[col_idx]
            x_sample = x_sample_by_col[col_idx]
            for ri, r in enumerate(raw_runs):
                if passes_strength(r, *adaptive):
                    continue
                ry1 = chart[r['s']]['y_pct']
                ry2 = chart[r['e']]['y_pct']
                if _hits_h_rule(col_idx, ry1, ry2):
                    continue
                cy = (ry1 + ry2) / 2
                if any(az[0] <= x_sample <= az[1] and
                       az[2] <= cy <= az[3] for az in ad_z):
                    continue
                if not _has_subbar_in_col(col_idx, ry1, ry2):
                    continue
                if not _has_aligned_partner(col_idx, ry1, ry2):
                    continue
                promoted_by_col[col_idx].add(ri)

        # ── Sandwich pass — second promotion path ──────────────────
        # A weak chart run sandwiched between two same-column accepted
        # runs (passes_strength OR already promoted via subbar+partner)
        # is strong structural evidence of a multi-line headline. The
        # cross-column partner gate fails when the headline is single-
        # column or when the partner column is blank/ads, but the
        # sandwich pattern itself confirms the layout: three lines of
        # tightly-spaced large type in one column.
        #
        # Same false-positive guards as primary promotion (per-col
        # h_rule, ad-zone, subbar strength). Spacing gates use the
        # same line_height_multiplier as Step B's gap-merge — if the
        # sandwich gaps are too wide for gap-merge, they're too wide
        # for sandwich admission.
        for col_idx, raw_runs in raw_runs_by_col.items():
            if not raw_runs:
                continue
            adaptive = adaptive_by_col[col_idx]
            chart = chart_by_col[col_idx]
            x_sample = x_sample_by_col[col_idx]
            promoted = promoted_by_col[col_idx]
            accepted = {ri for ri, r in enumerate(raw_runs)
                        if passes_strength(r, *adaptive) or ri in promoted}

            for ri, r in enumerate(raw_runs):
                if ri in accepted:
                    continue
                # Find nearest accepted before and after.
                before = max((j for j in accepted if j < ri), default=None)
                after = min((j for j in accepted if j > ri), default=None)
                if before is None or after is None:
                    continue

                # Gap-tightness: same gate as Step B gap-merge.
                rh = r['e'] - r['s'] + 1
                rb = raw_runs[before]
                ra = raw_runs[after]
                gap_b = r['s'] - rb['e']
                gap_a = ra['s'] - r['e']
                allowed_b = max(int(min(rb['e'] - rb['s'] + 1, rh) *
                                    line_height_multiplier), 12)
                allowed_a = max(int(min(ra['e'] - ra['s'] + 1, rh) *
                                    line_height_multiplier), 12)
                if gap_b > allowed_b or gap_a > allowed_a:
                    continue

                # Standard guards (h_rule, ad-zone, subbar strength).
                ry1 = chart[r['s']]['y_pct']
                ry2 = chart[r['e']]['y_pct']
                if _hits_h_rule(col_idx, ry1, ry2):
                    continue
                cy = (ry1 + ry2) / 2
                if any(az[0] <= x_sample <= az[1] and
                       az[2] <= cy <= az[3] for az in ad_z):
                    continue
                if not _has_subbar_in_col(col_idx, ry1, ry2):
                    continue

                promoted_by_col[col_idx].add(ri)

    # ── Steps A (filter) + B (gap-merge) per column ───────────────
    per_col_blocks = []
    for col_idx, raw_runs in raw_runs_by_col.items():
        adaptive = adaptive_by_col[col_idx]
        chart = chart_by_col[col_idx]
        x_sample = x_sample_by_col[col_idx]

        promoted = promoted_by_col.get(col_idx, set())
        runs = [(r['s'], r['e']) for ri, r in enumerate(raw_runs)
                if passes_strength(r, *adaptive) or ri in promoted]

        # Merge vertically-adjacent runs whose gap is small relative
        # to the line height — this is the multi-line headline case.
        merged = []
        for r in runs:
            r_h = r[1] - r[0] + 1
            if merged:
                prev = merged[-1]
                prev_h = prev[1] - prev[0] + 1
                gap = r[0] - prev[1]
                # Use MIN rather than MAX of the two run heights so that
                # a tiny isolated run can't be glommed onto a far-away
                # giant fused block. Floor at ~one body-text line height
                # (12 rows ≈ 0.36% page) so genuine multi-line headlines
                # with a kicker line still merge across normal inter-line
                # gaps.
                allowed = max(int(min(prev_h, r_h) * line_height_multiplier),
                              12)
                if gap <= allowed:
                    merged[-1] = (prev[0], r[1])
                    continue
            merged.append(r)

        for ms, me in merged:
            y1 = chart[ms]['y_pct']
            y2 = chart[me]['y_pct']
            # Skip blocks whose centre falls inside an ad zone
            cy = (y1 + y2) / 2
            in_ad = any(az[0] <= x_sample <= az[1] and
                        az[2] <= cy <= az[3] for az in ad_z)
            if in_ad:
                continue
            per_col_blocks.append({
                'col_idx': col_idx,
                'y1_pct': y1,
                'y2_pct': y2,
            })

    # ── Step C: cross-column alignment merge ──────────────────────
    # When gutter_fills are supplied, a cross-column merge is only
    # allowed if the gutter between the two columns has a fill
    # covering the candidate merge y-range. An intact vertical column
    # rule is a sure-stop signal that the content does NOT span. When
    # gutter_fills aren't available (caller didn't supply), fall back
    # to legacy y-overlap-only behaviour.
    per_col_blocks.sort(key=lambda b: (b['col_idx'], b['y1_pct']))

    assembled = []  # {col_min, col_max, y1, y2}
    for b in per_col_blocks:
        merged_flag = False
        for a in assembled:
            adjacent = (b['col_idx'] == a['col_max'] + 1 or
                        b['col_idx'] == a['col_min'] - 1)
            if not adjacent:
                continue
            y_lo = max(a['y1'], b['y1_pct'])
            y_hi = min(a['y2'], b['y2_pct'])
            overlap = y_hi - y_lo
            min_h = min(a['y2'] - a['y1'], b['y2_pct'] - b['y1_pct'])
            if min_h <= 0 or overlap / min_h < y_overlap_min:
                continue
            # Vertical-rule check: the gutter between the two columns
            # must have a fill at the proposed merge y-range, otherwise
            # the rule is intact and the merge is forbidden.
            if gutter_check_active:
                # Anchor on the assembled block's near-side col, partner
                # on the candidate col.
                anchor = a['col_max'] if b['col_idx'] > a['col_max'] else a['col_min']
                merge_y1 = min(a['y1'], b['y1_pct'])
                merge_y2 = max(a['y2'], b['y2_pct'])
                if not _gutter_supports(anchor, b['col_idx'],
                                        merge_y1, merge_y2):
                    continue
            a['col_min'] = min(a['col_min'], b['col_idx'])
            a['col_max'] = max(a['col_max'], b['col_idx'])
            a['y1'] = min(a['y1'], b['y1_pct'])
            a['y2'] = max(a['y2'], b['y2_pct'])
            merged_flag = True
            break
        if not merged_flag:
            assembled.append({
                'col_min': b['col_idx'],
                'col_max': b['col_idx'],
                'y1': b['y1_pct'],
                'y2': b['y2_pct'],
            })

    # ── Step D: cross-column rescue ───────────────────────────────
    # The strength filter is intentionally strict to keep body text
    # out. That sometimes drops a faint but rhythmically-correct run
    # in an edge column that should belong to a neighbour's headline.
    # For each assembled block, look one column outward on each side
    # for a raw run that meaningfully overlaps the block's y-extent.
    #
    # When gutter_fills are supplied, the rescue REQUIRES gutter-fill
    # support across the boundary — same vertical-rule rule as Step C.
    # When gutter_fills aren't available, fall back to the historical
    # dual-constraint check (run mostly inside block AND block mostly
    # inside run).
    for a in assembled:
        block_h = a['y2'] - a['y1']
        for adj in (a['col_min'] - 1, a['col_max'] + 1):
            if adj not in raw_runs_by_col:
                continue
            if a['col_min'] <= adj <= a['col_max']:
                continue
            anchor = adj if adj < a['col_min'] else a['col_max']
            partner = a['col_min'] if adj < a['col_min'] else adj
            if gutter_check_active and not _gutter_supports(
                    anchor, partner, a['y1'], a['y2']):
                continue
            chart = chart_by_col[adj]
            best = None
            for r in raw_runs_by_col[adj]:
                ry1 = chart[r['s']]['y_pct']
                ry2 = chart[r['e']]['y_pct']
                y_lo = max(a['y1'], ry1)
                y_hi = min(a['y2'], ry2)
                overlap = y_hi - y_lo
                rh = ry2 - ry1
                if rh <= 0 or block_h <= 0:
                    continue
                # Block-coverage is the always-required floor.
                if overlap / block_h < rescue_overlap_min:
                    continue
                # If gutter check isn't active, the legacy dual-constraint
                # check still requires the run to mostly cover the block.
                if not gutter_check_active and overlap / rh < 0.5:
                    continue
                if best is None or overlap > best['overlap']:
                    best = {'overlap': overlap, 'y1': ry1, 'y2': ry2}
            if best is None:
                continue
            if adj < a['col_min']:
                a['col_min'] = adj
            else:
                a['col_max'] = adj
            a['y1'] = min(a['y1'], best['y1'])
            a['y2'] = max(a['y2'], best['y2'])

    # Build output using column metadata for x extents
    headlines = []
    for a in assembled:
        col_min_meta = col_by_idx.get(a['col_min'])
        col_max_meta = col_by_idx.get(a['col_max'])
        if not col_min_meta or not col_max_meta:
            continue
        headlines.append({
            'x1_pct': round(col_min_meta['left_vw'], 1),
            'x2_pct': round(col_max_meta['right_vw'], 1),
            'y1_pct': round(a['y1'], 2),
            'y2_pct': round(a['y2'], 2),
            'cols_spanned': a['col_max'] - a['col_min'] + 1,
            'source': 'chart',
        })

    headlines.sort(key=lambda h: (h['y1_pct'], h['x1_pct']))
    return headlines


if __name__ == "__main__":
    import json, sys

    # Quick test on 3 issues
    tests = [
        ("/tmp/issue_1920-01-02/1920-01-02-01.pdf", "columns/1920-01-02/p1"),
        ("/tmp/issue_1937-01-14/1937-01-14-01.pdf", "columns/1937-01-14/p1"),
        ("/tmp/issue_1947-11-06/1947-11-06-01.pdf", "columns/1947-11-06/p1"),
    ]

    for pdf_path, col_dir in tests:
        # Load detected boundaries
        with open(f"{col_dir}/page_analysis.json") as f:
            analysis = json.load(f)
        det = analysis.get("detected_boundaries", [])
        boundaries = [b["pct"] for b in det]

        # Load ad zones
        ad_zones = []
        ads = analysis.get("ad_exclusion_zones", [])
        for az in ads:
            ad_zones.append((az["x1"], az["x2"], az["y1"], az["y2"]))

        headlines, _ = detect_headlines(pdf_path, boundaries, ad_zones=ad_zones)

        issue = pdf_path.split("/")[-1].rsplit("-", 1)[0]
        print(f"\n{issue}:")
        if not headlines:
            print("  No detections")
        for hl in headlines:
            print(f"  HEADLINE: x={hl['x1_pct']:.0f}-{hl['x2_pct']:.0f}% "
                  f"y={hl['y1_pct']:.0f}-{hl['y2_pct']:.0f}% "
                  f"cols={hl['cols_spanned']} conf={hl['confidence']}")
