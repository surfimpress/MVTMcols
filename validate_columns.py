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

# ─── "Out-of-volume page edge" rule (v2b) ──────────────────────────
#
# Bound-volume scans sometimes show the edge of a lower page in the
# stack peeking past the photographed page (the volume is not
# perfectly stacked; pages slip out of register). The visible strip
# is real, well-formed body text — just from a different page.
# v2's body/ad/headline rule won't catch it because the body signal
# is genuine. The discriminating signals are:
#
#   1. The strip's body coverage is markedly lower than this page's
#      interior columns (a partial column edge, not a full column).
#   2. The column extends past the page_profile-detected text_area
#      edge (the underlying page sits beyond this page's body band).
#
# Both must fire — neither alone is reliable. Body-ratio alone would
# misfire on real but sparse edge cols (e.g. an editorial column
# light on body text). text_area-extension alone would misfire on
# pages where text_area was estimated narrowly. Together they
# specifically describe "real text where this page's content
# shouldn't reach."
#
# Thresholds derived from 1947-01-09 (4 phantom edges + 12 real
# edges in the data):
#   body_ratio < 0.85  — every kept real edge had ratio ≥ 0.85
#                        OR no text_area extension. Phantoms sat at
#                        0.13-0.78.
#   text_area_ext > 1.0% — every phantom extended 1.5-5.7% past
#                          text_area; every real edge stayed within
#                          0.4%.
BODY_RATIO_THRESHOLD = 0.85
TEXT_AREA_EXT_THRESHOLD = 1.0  # % of page width

# ─── Conservatism rules (v2c) ──────────────────────────────────────
#
# After the 1947 batch rerun the user identified four failure modes:
#   - iterative peel ate real columns (1947-12-24 P8: 7c → 3c)
#   - symmetric 8→6 drops were dropping at least one real column
#   - real columns covered by display ads were being dropped
#   - on ad-heavy pages with very little body text the body-ratio
#     denominator is noisy, so Rule B fires on real columns
#
# The four rules below all bias toward keeping columns. The user's
# stated preference: "we may be better off tolerating some misplaced
# columns" — false positives (lost real cols) are worse than false
# negatives (kept phantoms).

# Ad anchor: an ad covering this much of a candidate column says the
# column is real, regardless of body/headline signals. (Body text alone
# can be sparse on ad-heavy pages; an ad whose footprint reaches into
# the column anchors it.)
AD_ANCHOR_OVERLAP = 0.50

# Sparse-body gate: when the interior median body height is below this
# floor (% page height), the page is ad-dominated and body_ratio is
# noisy. Raise Rule B's text_area-extension threshold for this page.
SPARSE_BODY_FLOOR = 25.0
SPARSE_BODY_EXT_THRESHOLD = 1.5  # % of page width when body is sparse


def _h_overlap_frac(col_x1, col_x2, rect_x1, rect_x2):
    """Fraction of [col_x1, col_x2] covered by [rect_x1, rect_x2]."""
    col_w = col_x2 - col_x1
    if col_w <= 0:
        return 0.0
    inter = max(0.0, min(col_x2, rect_x2) - max(col_x1, rect_x1))
    return inter / col_w


def _ad_anchors_column(col_x1, col_x2, ads):
    """
    True if any detected ad covers >= AD_ANCHOR_OVERLAP of the column.

    A multi-column ad whose footprint clearly extends into a candidate
    edge column is strong evidence the column is real — the column
    can't be phantom margin if a typeset ad block sits on it.
    """
    for a in ads or []:
        ax1 = a.get("x_pct")
        ax2 = a.get("x_end_pct")
        if ax1 is None or ax2 is None:
            continue
        if _h_overlap_frac(col_x1, col_x2, ax1, ax2) >= AD_ANCHOR_OVERLAP:
            return True
    return False


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


def _body_height_for_col(col_idx, body_regions):
    h = 0.0
    for b in body_regions or []:
        if b.get("col_idx") != col_idx:
            continue
        y1 = b.get("y1_pct")
        y2 = b.get("y2_pct")
        if y1 is None or y2 is None:
            continue
        h += max(0.0, y2 - y1)
    return h


def _interior_median_body(active, body_regions):
    """
    Median body coverage across the interior columns of the current
    active boundary set. Returns 0.0 if there are no interior columns
    (i.e. fewer than 3 columns active).
    """
    if len(active) < 3:
        return 0.0
    interior_orig_idxs = active[1:-1]
    heights = [_body_height_for_col(i, body_regions) for i in interior_orig_idxs]
    if not heights:
        return 0.0
    heights = sorted(heights)
    mid = len(heights) // 2
    if len(heights) % 2:
        return float(heights[mid])
    return float((heights[mid - 1] + heights[mid]) / 2.0)


def validate_columns_v2(boundaries, body_regions, ads, headlines,
                        text_area=None):
    """
    Drop empty edge columns using post-detection signals.

    Two phantom rules, OR'd:

    Rule A (empty edge): all of
        body_height_pct < BODY_HEIGHT_THRESHOLD
        ad_overlap      < AD_OVERLAP_THRESHOLD
        hl_overlap      < HL_OVERLAP_THRESHOLD
      catches scan-bleed/edge-rule "columns" that have ink but no
      real content.

    Rule B (out-of-volume page edge): both of
        body_height_pct / interior_median_body < BODY_RATIO_THRESHOLD
        column extends past text_area edge by > TEXT_AREA_EXT_THRESHOLD %
      catches strips of a different page (lower in the bound volume)
      that are physically visible past this page's body band. Body
      coverage is real text but only partial; the column sits
      outside the detected text_area. Both signals must fire — the
      ratio alone misfires on real but sparse edge cols, and the
      extension alone misfires when text_area was estimated narrowly.

    Single-pass evaluation: only the leftmost and rightmost columns
    are candidates (an interior column is a real content column even
    if sparse). No iteration — peeling a newly-exposed edge against a
    shrinking interior median was found to runaway-eat real columns
    (1947-12-24 P8: 7c → 3c).

    Args:
        boundaries:    boundary list (list of dicts with "x_pct").
        body_regions:  list of body-text regions, each with
                       col_idx/y1_pct/y2_pct.
        ads:           list of ad rects, each with x_pct/x_end_pct.
        headlines:     list of headlines, each with x1_pct/x2_pct.
        text_area:     optional dict with "left"/"right" page-pct
                       bounds of the body text band. Without this,
                       Rule B is skipped.

    Returns (filtered_boundaries, dropped_orig_idx, drop_log) where
        filtered_boundaries — list with phantom edges removed
        dropped_orig_idx    — set of ORIGINAL col_idx values dropped
        drop_log            — list of ('left'/'right', signals)
                              tuples in drop order, for diagnostics

    Edge-only by design: an interior near-empty column is more
    likely a real content column with sparse content than a
    segmentation error.

    Conservatism rules (v2c, see header for context):
      - Ad anchor: a column with a multi-col ad covering it (>=50%)
        is never dropped, regardless of body/headline signals.
      - Symmetric-drop tiebreaker: if both edges qualify, only the
        weaker (more phantom-y) edge is dropped — a symmetric 8→6
        drop almost always loses at least one real column.
      - Sparse-body gate: when interior median body coverage is below
        SPARSE_BODY_FLOOR, Rule B's text_area threshold is raised
        because body_ratio is noisy on ad-dominated pages.
    """
    new_boundaries = list(boundaries)
    n_original = len(boundaries) - 1
    if n_original < 3:
        return new_boundaries, set(), []

    drop_log = []
    dropped_orig = set()

    ta_left = ta_right = None
    if text_area:
        ta_left = text_area.get("left")
        ta_right = text_area.get("right")

    interior_med = _interior_median_body(list(range(n_original)),
                                         body_regions)
    # Sparse-body gate: ad-heavy pages have low body coverage everywhere,
    # so body_ratio (Rule B) is noisy. Raise the text_area threshold.
    sparse_body = 0.0 < interior_med < SPARSE_BODY_FLOOR
    ext_thresh = (SPARSE_BODY_EXT_THRESHOLD if sparse_body
                  else TEXT_AREA_EXT_THRESHOLD)

    def _is_phantom_empty(signals):
        return (signals["body_height_pct"] < BODY_HEIGHT_THRESHOLD
                and signals["ad_overlap"] < AD_OVERLAP_THRESHOLD
                and signals["hl_overlap"] < HL_OVERLAP_THRESHOLD)

    def _is_phantom_outofvolume(signals, side, col_x1, col_x2):
        # Rule B: low body ratio AND col extends past text_area edge.
        if interior_med <= 0:
            return False
        body_ratio = signals["body_height_pct"] / interior_med
        if body_ratio >= BODY_RATIO_THRESHOLD:
            return False
        if side == "left":
            if ta_left is None:
                return False
            ext = max(0.0, ta_left - col_x1)
        else:
            if ta_right is None:
                return False
            ext = max(0.0, col_x2 - ta_right)
        return ext > ext_thresh

    # Evaluate both edges against the original boundary set (no peel).
    right_orig = n_original - 1
    right_x1 = new_boundaries[-2]["x_pct"]
    right_x2 = new_boundaries[-1]["x_pct"]
    right_sig = _column_signals(right_orig, right_x1, right_x2,
                                body_regions, ads, headlines)
    right_drop = (_is_phantom_empty(right_sig)
                  or _is_phantom_outofvolume(right_sig, "right",
                                             right_x1, right_x2))
    # Ad-anchor protection: a column with an ad covering it isn't phantom.
    if right_drop and _ad_anchors_column(right_x1, right_x2, ads):
        right_drop = False

    left_orig = 0
    left_x1 = new_boundaries[0]["x_pct"]
    left_x2 = new_boundaries[1]["x_pct"]
    left_sig = _column_signals(left_orig, left_x1, left_x2,
                               body_regions, ads, headlines)
    left_drop = (_is_phantom_empty(left_sig)
                 or _is_phantom_outofvolume(left_sig, "left",
                                            left_x1, left_x2))
    if left_drop and _ad_anchors_column(left_x1, left_x2, ads):
        left_drop = False

    # Symmetric-drop tiebreaker: dropping both edges from an 8c page
    # produces 6c, which almost always loses at least one real column.
    # When both qualify, drop only the side with the lower body
    # coverage (the more phantom-y of the two).
    if right_drop and left_drop:
        if right_sig["body_height_pct"] <= left_sig["body_height_pct"]:
            left_drop = False
        else:
            right_drop = False

    # Apply: right first so left index doesn't shift.
    if right_drop:
        new_boundaries.pop()
        dropped_orig.add(right_orig)
        drop_log.append(("right", right_sig))
    if left_drop:
        new_boundaries.pop(0)
        dropped_orig.add(left_orig)
        drop_log.append(("left", left_sig))

    return new_boundaries, dropped_orig, drop_log
