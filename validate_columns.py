"""
Validate placed column boundaries by content plausibility.

The column-boundary detector occasionally produces an empty edge
"column" — a sliver of margin or right-side ad strip that contains
almost no body text. This module drops the first or last column when
its ink content is implausibly low compared to the median interior
column.

Edge-only by design: a middle near-empty column is more likely a real
content column with sparse content than a segmentation error.

Threshold: edge column is dropped if its ink mean (over the body band
y_pct 20–90 %) is below 35 % of the median ink mean of the interior
columns.

The threshold was empirically derived (2026-04-24) from a sample of 8
pages spanning 1878–1965; in that sample real edge content columns
sat at 78–141 % of median, and clear empty edges at 8–43 %.
"""

import numpy as np
from pdf_utils import render_grey


# Edge column ink must be at least this fraction of the median
# interior column's ink to be kept.
EDGE_INK_RATIO_THRESHOLD = 0.35

# Vertical band over which to measure column ink, skipping masthead and
# footer regions where edge cols often light up for unrelated reasons.
INK_BAND_TOP_PCT = 20.0
INK_BAND_BOTTOM_PCT = 90.0


def _column_ink_means(grey, boundaries):
    """
    Compute mean ink (255 - grey) per column over the body band.

    boundaries: list of dicts each with an "x_pct" key. With N
        boundaries there are N-1 columns; column i sits between
        boundaries[i] and boundaries[i+1].
    """
    H, W = grey.shape
    ink = 255.0 - grey
    y0 = int(H * INK_BAND_TOP_PCT / 100.0)
    y1 = int(H * INK_BAND_BOTTOM_PCT / 100.0)

    means = []
    for i in range(len(boundaries) - 1):
        x0 = int(W * boundaries[i]["x_pct"] / 100.0)
        x1 = int(W * boundaries[i + 1]["x_pct"] / 100.0)
        if x1 <= x0:
            means.append(0.0)
            continue
        means.append(float(ink[y0:y1, x0:x1].mean()))
    return means


def validate_edge_columns(boundaries, pdf_path, page_number=0,
                          dpi=75, ratio_threshold=EDGE_INK_RATIO_THRESHOLD):
    """
    Return a filtered copy of `boundaries` with empty edge columns removed.

    Only the leftmost and rightmost columns are tested. If either is
    below `ratio_threshold` × the median ink of the interior columns,
    the boundary that defines that edge column is dropped (collapsing
    the empty column into the page margin).

    Returns:
        (filtered_boundaries, dropped) where `dropped` is a list of
        ("left"/"right", ink_mean, median_interior, ratio) tuples for
        diagnostic logging. Empty list when nothing was dropped.
    """
    if len(boundaries) < 4:
        # Need at least 3 columns to have an interior median to compare
        # against; below that, trust the detector.
        return list(boundaries), []

    grey = render_grey(pdf_path, page_number, dpi)
    means = _column_ink_means(grey, boundaries)
    if len(means) < 3:
        return list(boundaries), []

    interior = means[1:-1]
    median_interior = float(np.median(interior))
    if median_interior <= 0:
        return list(boundaries), []

    dropped = []
    new_boundaries = list(boundaries)

    # Rightmost first so left-side index doesn't shift
    right_ink = means[-1]
    right_ratio = right_ink / median_interior
    if right_ratio < ratio_threshold:
        new_boundaries = new_boundaries[:-1]
        dropped.append(("right", right_ink, median_interior, right_ratio))

    left_ink = means[0]
    left_ratio = left_ink / median_interior
    if left_ratio < ratio_threshold:
        new_boundaries = new_boundaries[1:]
        dropped.append(("left", left_ink, median_interior, left_ratio))

    return new_boundaries, dropped


# ─── Post-detection edge-column validator (v2) ──────────────────────
#
# v2 runs AFTER detect_body_text and detect_headlines, so it has
# access to the strongest signals available. v1 (above) is still
# useful as a cheap pre-extraction prune; v2 catches cases v1 misses
# because the edge "column" has some ink (scan bleed, edge ruling)
# but no real content.
#
# An edge column is dropped if it has ALL of:
#   - body_height_pct  < BODY_HEIGHT_THRESHOLD  (very little body text)
#   - max ad overlap   < AD_OVERLAP_THRESHOLD   (no display ad span)
#   - max headline ov. < HL_OVERLAP_THRESHOLD   (no headline span)
#
# Body, ad, and headline are independent positive signals — a real
# column will hit at least one. Empty-margin "columns" hit none.
#
# Threshold rationale (from 1947-01-09 p1, the canonical phantom-edge
# case): body_height_pct of phantom edges sat at 4.5–11.3% of page
# height; real columns sat at ~51%. A 20% threshold cleanly separates
# them. The has-body-region binary check (used in earlier prototypes)
# was too lenient — the body region merger at min_region_pct=2.5% lets
# isolated text fragments survive on phantom margins.
#
# A refined content-area check (column centre vs the union of body
# regions in interior columns) was prototyped but did not change any
# decision in the validation set: when the three signals all fail,
# the column centre is also already outside the body span. Kept as a
# tie-breaker comment only.

BODY_HEIGHT_THRESHOLD = 20.0  # body coverage as % of page height
AD_OVERLAP_THRESHOLD = 0.30
HL_OVERLAP_THRESHOLD = 0.30


def _h_overlap_frac(col_x1, col_x2, rect_x1, rect_x2):
    """Fraction of [col_x1, col_x2] covered by [rect_x1, rect_x2]."""
    col_w = col_x2 - col_x1
    if col_w <= 0:
        return 0.0
    inter = max(0.0, min(col_x2, rect_x2) - max(col_x1, rect_x1))
    return inter / col_w


def _column_signals(col_idx, col_x1, col_x2,
                    body_regions, ads, headlines):
    body_height = 0.0
    for b in body_regions or []:
        if b.get("col_idx") != col_idx:
            continue
        y1 = b.get("y1_pct")
        y2 = b.get("y2_pct")
        if y1 is None or y2 is None:
            continue
        body_height += max(0.0, y2 - y1)
    max_ad = 0.0
    for a in ads or []:
        ax1 = a.get("x_pct")
        ax2 = a.get("x_end_pct")
        if ax1 is None or ax2 is None:
            continue
        max_ad = max(max_ad, _h_overlap_frac(col_x1, col_x2, ax1, ax2))
    max_hl = 0.0
    for hl in headlines or []:
        hx1 = hl.get("x1_pct")
        hx2 = hl.get("x2_pct")
        if hx1 is None or hx2 is None:
            continue
        max_hl = max(max_hl, _h_overlap_frac(col_x1, col_x2, hx1, hx2))
    return {
        "body_height_pct": body_height,
        "ad_overlap": max_ad,
        "hl_overlap": max_hl,
    }


def validate_columns_v2(boundaries, body_regions, ads, headlines):
    """
    Drop empty edge columns using post-detection signals.

    Iteratively peels failing edges from the boundary list until both
    edges pass or fewer than 3 columns remain. This handles pages
    where multiple adjacent phantom columns sit on a wide scan
    margin: dropping just the outermost would leave the next-outermost
    (now the new edge) still phantom. Capped at 4 iterations as a
    safety bound; no observed page in the corpus needs more than 2.

    Returns (filtered_boundaries, dropped_orig_idx, drop_log) where:
        filtered_boundaries — boundary list with phantom edges removed
        dropped_orig_idx    — set of ORIGINAL col_idx values that were
                              dropped (caller uses to filter and
                              renumber per-column data in the analysis
                              dict)
        drop_log            — list of ('left'/'right', signals_dict)
                              tuples in drop order, for diagnostics

    Edge-only by design (same as v1): an interior near-empty column
    is more likely a real content column with sparse content than a
    segmentation error. By iterating, we still only ever inspect
    columns that are at the outer edge of the *current* boundary
    list, which preserves the conservative posture.
    """
    new_boundaries = list(boundaries)
    n_original = len(boundaries) - 1
    if n_original < 3:
        return new_boundaries, set(), []

    # active[i] = ORIGINAL col_idx of the i-th column in new_boundaries
    active = list(range(n_original))
    drop_log = []
    dropped_orig = set()

    def _is_phantom(signals):
        return (signals["body_height_pct"] < BODY_HEIGHT_THRESHOLD
                and signals["ad_overlap"] < AD_OVERLAP_THRESHOLD
                and signals["hl_overlap"] < HL_OVERLAP_THRESHOLD)

    for _ in range(4):
        if len(active) < 3:
            break
        any_drop = False

        # Right edge first so the left position doesn't shift this iter.
        right_orig = active[-1]
        right_x1 = new_boundaries[-2]["x_pct"]
        right_x2 = new_boundaries[-1]["x_pct"]
        right_sig = _column_signals(right_orig, right_x1, right_x2,
                                    body_regions, ads, headlines)
        if _is_phantom(right_sig):
            new_boundaries.pop()
            active.pop()
            dropped_orig.add(right_orig)
            drop_log.append(("right", right_sig))
            any_drop = True

        if len(active) < 3:
            break

        left_orig = active[0]
        left_x1 = new_boundaries[0]["x_pct"]
        left_x2 = new_boundaries[1]["x_pct"]
        left_sig = _column_signals(left_orig, left_x1, left_x2,
                                   body_regions, ads, headlines)
        if _is_phantom(left_sig):
            new_boundaries.pop(0)
            active.pop(0)
            dropped_orig.add(left_orig)
            drop_log.append(("left", left_sig))
            any_drop = True

        if not any_drop:
            break

    return new_boundaries, dropped_orig, drop_log
