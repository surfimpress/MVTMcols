"""
Detect body text regions in newspaper pages.

Scans down the centre of each placed column looking for the body text
rhythm: regular, tightly-spaced peaks with near-zero troughs (the
alternating pattern of text lines and inter-line white space).

Runs AFTER column placement so it has accurate column boundaries.
Results are stored per-column as vertical runs of body text.
"""

import numpy as np
from PIL import Image
from coordinates import pct_to_px, px_to_pct
from pdf_utils import render_grey


def detect_body_text(pdf_path, columns, page_number=0, dpi=300,
                     r2_top_pct=None, r2_bottom_pct=None,
                     gutter_fills=None, ad_zones=None):
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

    grey = render_grey(pdf_path, page_number, dpi)
    h, w = grey.shape
    inv = 255.0 - grey

    # Constrain to R2 vertical extent
    y_min_px = pct_to_px(r2_top_pct or 0, h)
    y_max_px = min(pct_to_px(r2_bottom_pct or 100, h), h)

    # Detection parameters
    win = int(40 * dpi / 300)  # ~40px at 300 DPI, scales with resolution
    min_region_pct = 2.5  # minimum region height as % of page
    min_region_px = int(h * min_region_pct / 100)

    n_cols = len(columns)
    min_col_w_px = min(pct_to_px(c['right_vw'] - c['left_vw'], w)
                       for c in columns)

    def sample_strip_bounds(col):
        """
        Per-column horizontal sample strip used by the chart, blur,
        large-type, and h-rule detectors. Edge columns (index 0 and
        n_cols-1) widen the strip by 1.5× so that text shifted toward
        the page interior — common in justified columns near the
        edge — still falls inside the sample. The widened strip is
        capped so it never crosses the column's own boundaries.
        """
        left_px = pct_to_px(col['left_vw'], w)
        right_px = pct_to_px(col['right_vw'], w)
        cx = (left_px + right_px) // 2
        is_edge = (col['index'] == 0 or col['index'] == n_cols - 1)
        edge_factor = 1.5 if is_edge else 1.0
        shw = max(int(min_col_w_px * 0.24 * edge_factor), 8)
        # Stay inside the column itself (with a small margin).
        col_hw = max(8, (right_px - left_px) // 2 - 2)
        shw = min(shw, col_hw)
        s1 = max(0, cx - shw)
        s2 = min(w, cx + shw)
        return s1, s2, cx, left_px, right_px

    results = []
    charts = []

    for col in columns:
        sx1, sx2, cx, left_px, right_px = sample_strip_bounds(col)
        col_w = right_px - left_px
        if col_w < 10:
            continue

        # Sample a fixed-width strip from the column centre.
        # Width is normally ~24% of the narrowest column width; edge
        # columns widen the strip (see sample_strip_bounds).
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

        # Second pass: faint text recovery. Where the first pass found
        # nothing, check with lower thresholds. Only accept faint text
        # if it's adjacent to already-detected body text (fills gaps,
        # doesn't create new detections in blank areas).
        is_body_faint = np.zeros(h, dtype=bool)
        for start in range(y_min_px, min(y_max_px, h) - win, win // 2):
            if is_body[start:start + win].any():
                continue  # already detected, skip
            chunk = strip[start:start + win]
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
                # Relaxed thresholds for faint text
                if (spacing < max_spacing and contrast > 8 and peak_mean > 5):
                    is_body_faint[start:min(start + win, h)] = True

        # Only keep faint detections that are adjacent to existing body text
        # (within gap bridge distance)
        for y in range(h):
            if is_body_faint[y] and not is_body[y]:
                # Check if there's confirmed body text nearby
                search_range = int(h * 0.03)
                nearby = is_body[max(0, y - search_range):min(h, y + search_range)]
                if nearby.any():
                    is_body[y] = True

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

    # Generate blur visualisation from the SAME 300 DPI render used
    # for detection. This ensures the blur shows exactly what the
    # chart measures — same pixels, same strip positions, same values.
    # Downscale to 150 DPI afterwards to match page_raw.png for display.
    blur_img_hires = np.zeros((h, w), dtype=np.uint8)
    for col in columns:
        s1, s2, cx, left_px, right_px = sample_strip_bounds(col)
        if right_px - left_px < 10:
            continue
        col_strip = inv[:, s1:s2].mean(axis=1)
        enhanced = np.minimum(col_strip * 2, 255).astype(np.uint8)
        for x in range(s1, s2):
            blur_img_hires[:, x] = enhanced

    # Downscale to match page_raw.png dimensions exactly.
    # page_raw.png is rendered at 150 DPI by process_issue.py.
    # Rather than computing target size from DPI ratio (which can
    # be off by 1px due to rounding), we scale by integer factor.
    scale = dpi // 150 if dpi >= 150 else 1
    if scale > 1:
        blur_pil = Image.fromarray(blur_img_hires)
        target_w = (w + scale - 1) // scale  # ceiling division
        target_h = (h + scale - 1) // scale
        blur_img = np.array(blur_pil.resize((target_w, target_h), Image.LANCZOS))
    else:
        blur_img = blur_img_hires

    # ── Horizontal rule detection ────────────────────────────────
    # Scan each column strip for thin isolated spikes: high peak value,
    # very narrow (1-4 rows at 300 DPI), with low values on both sides.
    # These are column-width horizontal separators between articles.
    h_rules = []
    for col in columns:
        s1, s2, cx, left_px, right_px = sample_strip_bounds(col)
        if right_px - left_px < 10:
            continue
        strip_data = inv[:, s1:s2].mean(axis=1)

        for i in range(5, len(strip_data) - 5):
            v = strip_data[i]
            if v < 40:
                continue
            # Context: mean of 5 rows either side
            left_ctx = float(strip_data[max(0, i - 5):i].mean())
            right_ctx = float(strip_data[i + 1:min(len(strip_data), i + 6)].mean())
            # Must be a sharp isolated spike
            if left_ctx > v * 0.3 or right_ctx > v * 0.3:
                continue
            # Measure width at half-peak
            half_peak = v * 0.5
            width = 1
            for j in range(i - 1, max(0, i - 6), -1):
                if strip_data[j] > half_peak:
                    width += 1
                else:
                    break
            for j in range(i + 1, min(len(strip_data), i + 6)):
                if strip_data[j] > half_peak:
                    width += 1
                else:
                    break
            # Horizontal rules are very thin: 1-4 rows at 300 DPI
            max_rule_width = max(4, int(6 * dpi / 300))
            if width <= max_rule_width:
                y_pct = px_to_pct(i, h)
                # Avoid duplicates from adjacent peaks
                if not h_rules or h_rules[-1]['col_idx'] != col['index'] or \
                   abs(h_rules[-1]['y_pct'] - y_pct) > 0.3:
                    h_rules.append({
                        'col_idx': col['index'],
                        'x1_pct': round(col['left_vw'], 1),
                        'x2_pct': round(col['right_vw'], 1),
                        'y_pct': y_pct,
                        'strength': round(float(v), 1),
                    })

    # ── Large type detection ─────────────────────────────────────
    # Scan each column for regions with thick dark peaks that don't
    # drop to zero (trough_min > 10) and have complex wriggle (std > 40).
    # These are headlines, mastheads, and sub-headlines.
    large_type = []
    for col in columns:
        s1, s2, cx, left_px, right_px = sample_strip_bounds(col)
        if right_px - left_px < 10:
            continue
        strip_data = inv[:, s1:s2].mean(axis=1)

        is_large = np.zeros(h, dtype=bool)
        lt_win = int(40 * dpi / 300)
        for start in range(y_min_px, min(y_max_px, h) - lt_win, lt_win // 2):
            chunk = strip_data[start:start + lt_win]
            peaks_v, peak_pos, troughs_v = [], [], []
            for j in range(1, len(chunk) - 1):
                if chunk[j] > chunk[j - 1] and chunk[j] > chunk[j + 1]:
                    peaks_v.append(chunk[j])
                    peak_pos.append(j)
                if chunk[j] < chunk[j - 1] and chunk[j] < chunk[j + 1]:
                    troughs_v.append(chunk[j])

            if len(peaks_v) >= 1 and troughs_v:
                peak_mean = float(np.mean(peaks_v))
                trough_min = float(np.min(troughs_v))
                wriggle = float(np.std(chunk))
                # Large type: troughs don't reach zero, has wriggle,
                # and peaks are significant ink
                spacing = float(np.mean(np.diff(peak_pos))) if len(peak_pos) >= 2 else 0
                # Large type: troughs don't reach zero, has wriggle,
                # peaks are significant, AND spacing is wider than
                # body text (which has spacing < max_spacing).
                # Bold body text has same spacing as regular — it's
                # just darker. Large type has physically bigger letters
                # so the peaks are further apart.
                min_large_spacing = max_spacing * 0.8  # must be near or above body text spacing
                if (trough_min > 25 and wriggle > 45 and
                        peak_mean > 50 and spacing >= min_large_spacing):
                    is_large[start:min(start + lt_win, h)] = True

        # Extract contiguous large type runs
        in_run = False
        run_start = 0
        min_lt_px = int(h * 0.01)  # at least 1% of page
        for y_row in range(h):
            if is_large[y_row]:
                if not in_run:
                    run_start = y_row
                    in_run = True
            else:
                if in_run:
                    if y_row - run_start >= min_lt_px:
                        large_type.append({
                            'col_idx': col['index'],
                            'x1_pct': round(col['left_vw'], 1),
                            'x2_pct': round(col['right_vw'], 1),
                            'y1_pct': px_to_pct(run_start, h),
                            'y2_pct': px_to_pct(y_row, h),
                            'method': 'window',
                        })
                    in_run = False
        if in_run and h - run_start >= min_lt_px:
            large_type.append({
                'col_idx': col['index'],
                'x1_pct': round(col['left_vw'], 1),
                'x2_pct': round(col['right_vw'], 1),
                'y1_pct': px_to_pct(run_start, h),
                'y2_pct': px_to_pct(h, h),
                'method': 'window',
            })

    # ── Large type detection (bar width method) ──────────────────
    # Scan the blur image directly for bright bars that are wider
    # than body text lines. Body text bars are ~3-5px at 150 DPI
    # (~6-10px at 300 DPI). Large type bars are 2x+ wider.
    # This is more direct than the sliding window — it measures
    # the actual bar width rather than inferring from peak spacing.
    large_type_bars = []
    body_line_height = int(5 * dpi / 150)  # ~5px at 150 DPI
    min_large_width = body_line_height * 2  # must be 2x body text height
    # Sub-bars: relaxed width threshold (= body_line_height, half of
    # min_large_width). Used by the chart-method assembly as cross-col-
    # confirmed strength evidence when chart runs align across columns
    # but the strip is fragmented by asymmetric letter layout.
    min_subbar_width = body_line_height
    bar_subbars_by_col = {}

    for col in columns:
        s1, s2, cx, left_px, right_px = sample_strip_bounds(col)
        if right_px - left_px < 10:
            continue
        strip_data = inv[:, s1:s2].mean(axis=1)

        col_subbars = []

        def _record_bar(start_px, end_px, *,
                        _col=col, _strip=strip_data, _subbars=col_subbars):
            """Common emission for both strict bars and sub-bars.

            Loop-vars bound as default args so the closure is safe to
            store/return without late-binding surprises.
            """
            w_px = end_px - start_px
            if w_px <= 0:
                return
            mean_v = float(_strip[start_px:end_px].mean())
            if mean_v <= 40:
                return
            if w_px >= min_large_width:
                large_type_bars.append({
                    'col_idx': _col['index'],
                    'x1_pct': round(_col['left_vw'], 1),
                    'x2_pct': round(_col['right_vw'], 1),
                    'y1_pct': px_to_pct(start_px, h),
                    'y2_pct': px_to_pct(end_px, h),
                    'method': 'bar_width',
                })
            if w_px >= min_subbar_width:
                _subbars.append({
                    'y1_pct': px_to_pct(start_px, h),
                    'y2_pct': px_to_pct(end_px, h),
                    'mean': mean_v,
                    'width_px': w_px,
                })

        # Find bright bars: contiguous runs above a brightness threshold
        bar_thresh = 30  # minimum brightness to be "in a bar"
        in_bar = False
        bar_start = 0
        for y_row in range(y_min_px, y_max_px):
            if strip_data[y_row] > bar_thresh:
                if not in_bar:
                    bar_start = y_row
                    in_bar = True
            else:
                if in_bar:
                    _record_bar(bar_start, y_row)
                    in_bar = False
        if in_bar:
            _record_bar(bar_start, y_max_px)

        bar_subbars_by_col[col['index']] = col_subbars

    # ── Large type detection (chart-assembly method) ─────────────
    # Drive assembly directly off the per-column charts: find bright
    # runs (val>80, ≥11 rows), filter by signal strength, merge
    # multi-line groups within a column, then merge cross-column
    # blocks whose y-extents align. Catches multi-line and single-
    # column headlines that the bar_width method misses, and merges
    # the lines of a 2-line headline into one block.
    large_type_chart = []
    try:
        from detect_headlines import assemble_headlines_from_charts
        large_type_chart = assemble_headlines_from_charts(
            charts, columns, gutter_fills=gutter_fills,
            ad_zones=ad_zones,
            bar_subbars=bar_subbars_by_col,
            h_rules=h_rules)
        for lt in large_type_chart:
            lt['method'] = 'chart'
    except Exception:
        large_type_chart = []

    # Merge all three methods — sliding window, bar_width, and chart
    all_large_type = large_type + large_type_bars + large_type_chart

    return results, charts, blur_img, h_rules, all_large_type
