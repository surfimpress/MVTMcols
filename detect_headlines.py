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

    # Convert boundary positions to pixel x coordinates
    gutter_xs = [int(b / 100 * w) for b in column_boundaries]
    # Only use interior gutters (not the outer edges)
    if len(gutter_xs) >= 2:
        gutter_xs = gutter_xs[1:-1]  # drop first and last
    if not gutter_xs:
        return []

    ad_z = ad_zones or []

    # Parameters
    block_h = max(int(h * 0.01), 10)  # ~1% of page height per block
    gutter_hw = max(int(w * 0.005), 4)  # half-width of gutter sample zone

    # Scan the page in vertical blocks
    n_blocks = h // block_h
    n_gutters = len(gutter_xs)

    # First pass: measure gutter darkness at every block
    gutter_dark = np.zeros((n_blocks, n_gutters))
    for bi in range(n_blocks):
        y1 = bi * block_h
        y2 = min((bi + 1) * block_h, h)
        band = inv[y1:y2, :]
        for gi, gx in enumerate(gutter_xs):
            x1 = max(0, gx - gutter_hw)
            x2 = min(w, gx + gutter_hw)
            gutter_dark[bi, gi] = float(band[:, x1:x2].mean())

    # Compute per-gutter baseline: the median darkness when NOT in a
    # headline. Use the 25th percentile — gutters are white most of
    # the time, so the lower values represent the normal state.
    gutter_baseline = np.percentile(gutter_dark, 25, axis=0)
    gutter_baseline = np.maximum(gutter_baseline, 5)  # floor at 5

    # A gutter is "filled" when its darkness is well above its own
    # baseline. Use 2× baseline or baseline + 40, whichever is higher.
    # This catches headlines on both clean and noisy pages.
    gutter_filled = np.zeros((n_blocks, n_gutters), dtype=bool)
    for gi in range(n_gutters):
        fill_thresh = max(gutter_baseline[gi] * 2, gutter_baseline[gi] + 40)
        gutter_filled[:, gi] = gutter_dark[:, gi] > fill_thresh

    # Find headline regions: contiguous vertical runs where 2+ adjacent
    # gutters are simultaneously filled.
    # Build a map of "spanning" blocks: blocks where consecutive gutters
    # are filled, indicating text crossing those column boundaries.
    headlines = []
    min_span_blocks = max(2, int(h * 0.01) // block_h)  # at least ~1% of page

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
        # Horizontal extent: from the column boundary before g_min
        # to the column boundary after g_max
        # g_min is index into gutter_xs (interior gutters)
        # The headline spans from column g_min to column g_max + 2
        # (because each gutter separates two columns)
        x1_idx = m["g_min"]  # gutter index
        x2_idx = m["g_max"]
        cols_spanned = x2_idx - x1_idx + 2  # +2 because spanning across gutters

        # Convert to page percentages
        # The headline extends from one boundary before the first filled gutter
        # to one boundary after the last filled gutter
        all_boundaries = [int(b / 100 * w) for b in column_boundaries]
        # gutter_xs are interior boundaries (indices 1..n-2 of all_boundaries)
        # So gutter_xs[gi] corresponds to all_boundaries[gi + 1]
        left_bound_idx = x1_idx + 1 - 1  # boundary before the first filled gutter
        right_bound_idx = x2_idx + 1 + 1  # boundary after the last filled gutter
        left_bound_idx = max(0, left_bound_idx)
        right_bound_idx = min(len(all_boundaries) - 1, right_bound_idx)

        x1_pct = column_boundaries[left_bound_idx]
        x2_pct = column_boundaries[right_bound_idx]

        y1_pct = round(m["b_start"] * block_h / h * 100, 1)
        y2_pct = round(min((m["b_end"] + 1) * block_h, h) / h * 100, 1)

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

    # Filter: require minimum 3 columns spanned or high confidence.
    # Must be within the scanned image area.
    final = []
    for hl in kept:
        if hl["y1_pct"] < y_min or hl["y2_pct"] > y_max:
            continue
        if hl["cols_spanned"] >= 3:
            final.append(hl)
        elif hl["confidence"] >= 0.8 and hl["cols_spanned"] >= 2:
            final.append(hl)

    final.sort(key=lambda hl: (hl["y1_pct"], hl["x1_pct"]))
    return final


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

        headlines = detect_headlines(pdf_path, boundaries, ad_zones=ad_zones)

        issue = pdf_path.split("/")[-1].rsplit("-", 1)[0]
        print(f"\n{issue}:")
        if not headlines:
            print("  No headlines detected")
        for hl in headlines:
            print(f"  x={hl['x1_pct']:.0f}-{hl['x2_pct']:.0f}% "
                  f"y={hl['y1_pct']:.0f}-{hl['y2_pct']:.0f}% "
                  f"cols={hl['cols_spanned']} conf={hl['confidence']}")
