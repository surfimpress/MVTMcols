"""ARCHIVED — deriving boxes by enumerating corner QUADRILATERALS.

Superseded by `experiments/ad_rectangles.py`. Nothing imports this.

WHAT IT DID
-----------
Clustered the corner map into x-lines and y-lines, then tested every pair
of x-lines against every pair of y-lines: a rectangle was accepted when
enough of its four corners were marked AND all four of its edges were
ruled.

WHY IT WAS REPLACED
-------------------
It asks the wrong question. "Is this a valid rectangle?" is answered YES
for the union of two stacked ads -- that union has four marked corners and
four ruled edges, because the side rules run continuously past both. So
the generator produced bridges, gutter slivers and double-rule pairs, and
each needed its own filter: a twin-rule collapse, a gutter-sliver drop, a
double-rule merge. Six tuned thresholds, all cleaning up after the
generator rather than fixing it.

`ad_rectangles` asks instead whether any OTHER corner interrupts a
rectangle's sides. A union's sides run straight through the divider's
corners, so it is rejected by construction -- and so are slivers, and
unions of any depth. One predicate, no thresholds.

THE FAILURE WORTH REMEMBERING
-----------------------------
`MIN_MARKED_CORNERS` was relaxed from 4 to 3, on the reasoning that three
corners fix an axis-aligned rectangle and that only 43% of known boxes
have all four marked. Every corpus number improved -- 547 to 812 boxes,
80% agreement with `detect_boxes`, under-finding pages 31 down to 18 of
90. The RENDER was far worse: a thicket of slightly-offset near-duplicate
rectangles around every ad, because a corner a cell or two away can stand
in for the missing one, and each substitution makes another valid
rectangle. It was shipped on the numbers and reverted on sight.

That is the clearest example in this experiment of a count agreeing with
another detector not being evidence. See `instructions/scaled_pipeline.md`
§5z.

The two percent-unit filters this generator needed are archived separately
in `percent_box_filters.py`.
"""

from __future__ import annotations

# --- deriving boxes from the corner map ------------------------------
# A candidate edge counts as ruled if at least this share of the cells
# along it hold a separator. Below 1.0 because a rule's ends stop short of
# the corners (the rounded-corner inset, measured at 0.5-3.9%) and because
# Tesseract fragments rules.
EDGE_SUPPORT = 0.80
BOX_MIN_CELLS = 4          # a box smaller than this is furniture

# Corners needed to accept a rectangle. FOUR -- see the module docstring
# for what happened when this was three.
MIN_MARKED_CORNERS = 4

# Two stacked boxes are divided by TWO rules -- one's foot and the next
# one's head, a pica or so apart. Each pairs validly with the shared side
# rules, so a stack of two yields five rectangles. Where two boxes agree
# on three edges and differ on the fourth by no more than this, the
# narrower is kept.
TWIN_EDGE_CELLS = 4


def boxes_from_corners(points, counts, n_cols, n_rows, tol=1.6):
    """Rectangles whose corners are marked and whose four edges are ruled.

    Corners alone would give a combinatorial explosion, so every candidate
    is checked against the ruling: an edge must be a real line on the
    page, not merely the shortest path between two corners.
    """
    xs = sorted({round(p[1]) for p in points})
    ys = sorted({round(p[0]) for p in points})

    def has_corner(y, x):
        return any(abs(p[0] - y) <= tol and abs(p[1] - x) <= tol
                   for p in points)

    def ruled_h(y, x0, x1):
        hit = sum(1 for x in range(x0, x1 + 1)
                  if any(0 <= y2 < n_rows and counts[y2][x]
                         for y2 in (y - 1, y, y + 1)))
        return hit / max(1, x1 - x0 + 1) >= EDGE_SUPPORT

    def ruled_v(x, y0, y1):
        hit = sum(1 for y in range(y0, y1 + 1)
                  if any(0 <= x2 < n_cols and counts[y][x2]
                         for x2 in (x - 1, x, x + 1)))
        return hit / max(1, y1 - y0 + 1) >= EDGE_SUPPORT

    out = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            if x1 - x0 < BOX_MIN_CELLS:
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    if y1 - y0 < BOX_MIN_CELLS:
                        continue
                    marked = sum((has_corner(y0, x0), has_corner(y0, x1),
                                  has_corner(y1, x0), has_corner(y1, x1)))
                    if marked < MIN_MARKED_CORNERS:
                        continue
                    if not (ruled_h(y0, x0, x1) and ruled_h(y1, x0, x1)
                            and ruled_v(x0, y0, y1) and ruled_v(x1, y0, y1)):
                        continue
                    out.append((y0, x0, y1, x1))

    keep = []
    for b in sorted(out, key=lambda b: -((b[2] - b[0]) * (b[3] - b[1]))):
        if not any(abs(b[0] - k[0]) <= tol and abs(b[1] - k[1]) <= tol
                   and abs(b[2] - k[2]) <= tol and abs(b[3] - k[3]) <= tol
                   for k in keep):
            keep.append(b)

    # Collapse the twin-rule rectangles. Smallest first, so the narrower
    # survivor is already in `final` when its wider twin is tested.
    final = []
    for b in sorted(keep, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])):
        twin = False
        for k in final:
            diffs = [abs(b[0] - k[0]), abs(b[1] - k[1]),
                     abs(b[2] - k[2]), abs(b[3] - k[3])]
            if sum(1 for d in diffs if d <= tol) == 3 \
                    and max(diffs) <= TWIN_EDGE_CELLS:
                twin = True
                break
        if not twin:
            final.append(b)
    return sorted(final, key=lambda b: (b[0], b[1]))
