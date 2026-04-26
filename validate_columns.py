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
