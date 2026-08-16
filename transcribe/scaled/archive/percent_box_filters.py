"""ARCHIVED — the two box filters that conflated page-percent units.

Superseded by `experiments/ad_rectangles.py`, which needs neither.

Both took boxes in GRID CELLS and converted back to page percent to apply
percent thresholds. Page percent is two different units -- x is a
percentage of page WIDTH, y of page HEIGHT -- so a single threshold meant
different physical distances on each axis. On 1980-04-06 p13 the vertical
reading is 1.41x the horizontal.

The consequence in `drop_gutters` was not a skew but a broken quantity:
its "ratio" divided a percent-of-width by a percent-of-height, so a SQUARE
20x20-cell region scored 1.406 rather than 1.0. Every threshold in it was
tuned against that.

Both existed only to clean up after corner-quadruple enumeration, which
generates unions of stacked boxes, gutter slivers and double-rule pairs.
`ad_rectangles` replaces the lot with one predicate -- no corner may
interrupt a side -- and works in cells, which are square by construction.

Kept for the record. Nothing imports this.
"""

from __future__ import annotations

DOUBLE_RULE_GAP_PCT = 2.5
GUTTER_RATIO_EXTREME = 20.0
GUTTER_THIN_EXTREME = 2.5
GUTTER_RATIO_TOUCHING = 12.0
GUTTER_THIN_TOUCHING = 2.0
GUTTER_TOUCH_PCT = 0.8


def merge_double_rules(boxes, cell_w, cell_h):
    """Collapse the two rectangles of one double-ruled border into one."""
    def area(b):
        return (b[3] - b[1]) * (b[2] - b[0])

    drop = set()
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            inner, outer = (a, b) if area(a) < area(b) else (b, a)
            nested = (inner[1] >= outer[1] - 1 and inner[3] <= outer[3] + 1
                      and inner[0] >= outer[0] - 1 and inner[2] <= outer[2] + 1)
            if not nested:
                continue
            gap = max(abs(a[1] - b[1]) * cell_w, abs(a[3] - b[3]) * cell_w,
                      abs(a[0] - b[0]) * cell_h, abs(a[2] - b[2]) * cell_h)
            # A genuine box-inside-a-box (a panel within an ad) is nested
            # too, but sits far further in than a border's own gap.
            if 0.01 < gap <= DOUBLE_RULE_GAP_PCT:
                drop.add(id(inner))
    return [b for b in boxes if id(b) not in drop]


def drop_gutters(boxes, cell_w, cell_h):
    """Remove rectangles that can only be the space BETWEEN boxes.

    `boxes` are in cell coordinates; cell_w/cell_h convert to page percent
    so the thresholds mean the same thing on any page shape.
    """
    def dims(b):
        return (b[3] - b[1]) * cell_w, (b[2] - b[0]) * cell_h

    def touches(b, others):
        """Do BOTH long edges sit on a parallel edge of a different box?"""
        w, h = dims(b)
        if w >= h:                                   # a horizontal sliver
            top = any(abs((b[0] - o[2]) * cell_h) <= GUTTER_TOUCH_PCT
                      or abs((b[0] - o[0]) * cell_h) <= GUTTER_TOUCH_PCT
                      for o in others)
            bot = any(abs((b[2] - o[0]) * cell_h) <= GUTTER_TOUCH_PCT
                      or abs((b[2] - o[2]) * cell_h) <= GUTTER_TOUCH_PCT
                      for o in others)
            return top and bot
        left = any(abs((b[1] - o[3]) * cell_w) <= GUTTER_TOUCH_PCT
                   or abs((b[1] - o[1]) * cell_w) <= GUTTER_TOUCH_PCT
                   for o in others)
        right = any(abs((b[3] - o[1]) * cell_w) <= GUTTER_TOUCH_PCT
                    or abs((b[3] - o[3]) * cell_w) <= GUTTER_TOUCH_PCT
                    for o in others)
        return left and right

    keep = []
    for b in boxes:
        w, h = dims(b)
        thin, ratio = min(w, h), max(w, h) / max(0.01, min(w, h))
        others = [o for o in boxes if o is not b]
        if ratio >= GUTTER_RATIO_EXTREME and thin <= GUTTER_THIN_EXTREME:
            continue
        if (ratio >= GUTTER_RATIO_TOUCHING and thin <= GUTTER_THIN_TOUCHING
                and touches(b, others)):
            continue
        keep.append(b)
    return keep

