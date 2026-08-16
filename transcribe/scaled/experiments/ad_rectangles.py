"""Rectangles from CORNERS ALONE.

A standalone, data-in/data-out function. It takes corner points, and
optionally the column lines and photo positions, and returns the
rectangles most likely to be ads. It does not read the database, does not
look at separators, and does not know Tesseract exists. Once the corners
are established, the ruling has done its job.

THE ONE IDEA
------------
A rectangle is an ITEM when no other corner interrupts its sides.

That single test disposes of everything the rule-pairing route needed six
thresholds for:

  *bridges* -- the union of two stacked ads spans from the top of the
   upper to the bottom of the lower, so its left and right sides run
   straight THROUGH the four corners of the divider between them. Those
   corners interrupt the sides, so the union is not atomic.

  *slivers* -- the gap between two boxes has its top edge running the
   width of the page, and the corners of everything above it sit on that
   edge. Interrupted, so not atomic.

  *unions of any depth* -- same argument, no special case.

No aspect ratio, no thin-dimension bar, no gap tolerance, no twin
collapse. One geometric predicate.

WHY IT IS ORDER-INDEPENDENT
---------------------------
Corners are clustered by sorting and splitting on gaps, candidates are
every pair of x-lines against every pair of y-lines, and atomicity is a
property of the whole corner set. Nothing depends on the order corners
arrive in, so there is no directional bias to correct for -- the earlier
detectors' worst bugs were all order artefacts, and this construction
cannot have them.

SCORING, not filtering
----------------------
Column alignment and photo containment RANK the survivors; they never
reject one. A newspaper lays out to its column grid and sells ads by the
column inch, so an ad's sides falling on column boundaries is real
evidence -- but a small notice set inside a column is still an ad.
"""

from __future__ import annotations

# EVERYTHING HERE IS IN GRID CELLS, not page percent.
#
# THE CELLS ARE SQUARE -- 7 x 7 px on 1980-04-06 p13. They are quoted as
# 0.5% wide and 0.3555% tall only because those percentages are taken
# against different dimensions, width and height. The cell itself is not
# stretched.
#
# That distinction cost a real box. An earlier version worked in page
# percent with a single 0.9% tolerance on both axes: 1.80 cells across but
# 2.53 cells down, because the vertical percent is measured against the
# taller dimension. The vertical figure swallowed genuine 2-cell
# separations and lost the Sidewalk Sale box, whose foot sits exactly 2
# cells from the panel inside it -- the corner rows at the page foot are
# 266, 268 and 270, each 2 cells apart.
#
# In cells the tolerance is one number meaning one physical distance in
# both directions, and it can be checked against the grid by eye. The
# caller converts to percent on the way out.
LINE_TOL = 1.0        # cells: two corners this close are the same line
# ON_LINE_TOL was removed here: `_interrupted` asked "is this corner close
# enough to the side to count as on it", and now asks whether it is on the
# same LINE, which is membership and needs no distance. Nothing else used
# it, so it survived the change as a dead constant.
# A FREE PARAMETER, not a derived fact. Swept corpus-wide:
#   2 -> 364    3 -> 300    4 -> 273    5 -> 266    6 -> 257    8 -> 246
# roughly 10% of the corpus count per cell, with NO cliff anywhere to
# justify 4 specifically. `LINE_TOL` by contrast does sit on a plateau.
# Treat this as a knob someone chose, and re-sweep it against rendered
# pages before relying on any count that depends on it.
MIN_SIDE_CELLS = 4    # a side shorter than this is treated as furniture


def _lines(values, tol=LINE_TOL, want_index=False):
    """1-D clustering by sorting and splitting on gaps. Order-independent.

    With `want_index`, also returns a value -> cluster-index map, so a
    caller can ask which line a corner BELONGS to rather than how far it
    sits from the line's centroid. Those differ: splitting on gaps lets a
    chain of corners form a cluster wider than `tol`.
    """
    if not values:
        return ([], {}) if want_index else []
    vals = sorted(values)
    groups, run = [], [vals[0]]
    for v in vals[1:]:
        if v - run[-1] <= tol:
            run.append(v)
        else:
            groups.append(run)
            run = [v]
    groups.append(run)
    centres = [sum(g) / len(g) for g in groups]
    if not want_index:
        return centres
    index = {v: i for i, g in enumerate(groups) for v in g}
    return centres, index


def _interrupted(xi, yi, xj, yj, indexed):
    """Does any corner sit in the OPEN interior of one of the four sides?

    Asked entirely in CLUSTER INDICES -- `indexed` is the corner list as
    (x-line, y-line) index pairs. "Same line" is then identity, and
    "strictly between" is an integer comparison, so no tolerance appears
    here at all.

    NO TOLERANCE HERE DOES NOT MEAN NO SLOP. Membership is decided by the
    gap-splitting in `_lines`, and a chain of corners can build a cluster
    wider than `LINE_TOL`, so a corner up to a cluster-width away from a
    side still counts as sitting ON it. Measured corpus-wide: 80 of 413
    vetoes are caused by a corner more than 1.0 cell from the side it is
    charged to, and none beyond 2.0 cells (worst 1.78, on 1986-01-08 p9);
    15 of 969 accepted rectangles have a defining corner beyond 1.0 cell,
    same worst case. So the slop is real, bounded at ~1.8 cells (~0.9% of
    page width) and confined to two pages -- and it is symmetric, which
    the centroid version was not.

    It used to compare distances to the line CENTROIDS, and that was the
    same mistake `has_corner` made: a corner BELONGING to the right-hand
    line sat 1.04 cells from that line's centroid, so it failed to count
    as part of the side -- and simultaneously fell inside `x1 - LINE_TOL`,
    so it counted as an interior point of the TOP edge and vetoed the
    rectangle. Measured on 1980-04-06 p10: the Carleton Refrigeration /
    TRUCKING / McKay stack survived or died on whether that line's
    centroid landed at 122.83 or 123.04 -- a fifth of a cell. A corner of
    a side is not an interruption of the edge that ends on it.
    """
    for (cx, cy) in indexed:
        if cx in (xi, xj) and yi < cy < yj:      # on a side, between ends
            return True
        if cy in (yi, yj) and xi < cx < xj:      # on an edge, between ends
            return True
    return False


def ad_rectangles(corners, column_lines=(), photos=(),
                  min_side=MIN_SIDE_CELLS):
    """Most likely ad rectangles, from corner points alone.

    ALL COORDINATES ARE GRID CELLS -- see the note at the top.

    corners       [(x, y)]        cell units
    column_lines  [x]             cell units
    photos        [(L, T, R, B)]  cell units

    Returns dicts sorted by score, each with the rectangle, the score and
    the reasons behind it, so a caller can see WHY a rectangle ranked
    where it did rather than take the number on trust.
    """
    if not corners:
        return []

    # Cluster once, and remember WHICH cluster each corner joined.
    #
    # `has_corner` used to measure distance to the cluster's CENTROID with
    # the same tolerance used to build it. Clustering splits on gaps, so a
    # run of corners each within LINE_TOL of the previous chains into a
    # cluster wider than LINE_TOL -- measured, 18 such clusters corpus-wide,
    # the worst 3.0 cells across 14 corners. 25 corners on 9 pages then sat
    # further than LINE_TOL from their own cluster's centroid and could not
    # certify the line they had themselves defined, silently costing real
    # boxes. Membership is the right test: a corner belongs to the line it
    # was clustered into.
    xs, x_of = _lines([c[0] for c in corners], want_index=True)
    ys, y_of = _lines([c[1] for c in corners], want_index=True)
    indexed = [(x_of[c[0]], y_of[c[1]]) for c in corners]
    have = set(indexed)

    def has_corner(xi, yi):
        return (xi, yi) in have

    out = []
    for i, x0 in enumerate(xs):
        for xj in range(i + 1, len(xs)):
            x1 = xs[xj]
            if x1 - x0 < min_side:
                continue
            for j, y0 in enumerate(ys):
                for yj in range(j + 1, len(ys)):
                    y1 = ys[yj]
                    if y1 - y0 < min_side:
                        continue
                    if not (has_corner(i, j) and has_corner(xj, j)
                            and has_corner(i, yj) and has_corner(xj, yj)):
                        continue
                    # THE test.
                    if _interrupted(i, j, xj, yj, indexed):
                        continue

                    reasons, score = [], 0.0
                    on_col = sum(1 for cl in column_lines
                                 if abs(cl - x0) <= LINE_TOL
                                 or abs(cl - x1) <= LINE_TOL)
                    if on_col:
                        score += on_col
                        reasons.append(f"{on_col} side(s) on a column line")
                    for (pl, pt, pr, pb) in photos:
                        if (x0 <= (pl + pr) / 2 <= x1
                                and y0 <= (pt + pb) / 2 <= y1):
                            score += 1
                            reasons.append("contains a photo")
                            break
                    out.append({"L": round(x0, 2), "T": round(y0, 2),
                                "R": round(x1, 2), "B": round(y1, 2),
                                "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
                                "score": round(score, 2), "reasons": reasons})
    out = _resolve_crossings(out)
    return sorted(out, key=lambda r: (-r["score"],
                                      -(r["R"] - r["L"]) * (r["B"] - r["T"])))


def _crosses(a: dict, b: dict) -> bool:
    """Do two rectangles straddle each other rather than nest?"""
    ox = min(a["R"], b["R"]) - max(a["L"], b["L"])
    oy = min(a["B"], b["B"]) - max(a["T"], b["T"])
    if ox <= 0 or oy <= 0:
        return False

    def contains(p, q):
        return (p["L"] >= q["L"] - LINE_TOL and p["R"] <= q["R"] + LINE_TOL
                and p["T"] >= q["T"] - LINE_TOL and p["B"] <= q["B"] + LINE_TOL)

    return not (contains(a, b) or contains(b, a))


def _resolve_crossings(rects: list[dict]) -> list[dict]:
    """Rectangles may NEST or be DISJOINT. They may not cross.

    Two rectangles that overlap without either containing the other cannot
    both be printed regions of one page, so at least one is spurious. The
    corner predicate is local -- it judges each rectangle against the
    corner set -- and cannot see this, which is why the check is separate.
    On 1980-04-06 p8 it left three crossing pairs, all in the Sidewalk
    Sale, where a vertical strip down one product column crossed the
    horizontal row-bands (four of them, as the current corner set reads
    it -- the strip is dropped at crossings=4).

    UNTESTED ORDERING: the tie-break puts SCORE above AREA. Where a
    genuine container and a spurious sliver cross, both at one crossing,
    the lower-scoring one goes -- and a sliver with two sides on column
    lines scores 2 while a container inset within a column scores 0, so
    the container would lose. That has never happened here: corpus-wide
    there is exactly ONE drop and it is unambiguous at crossings=4, and
    an instrumented run found ZERO ties on any criterion. So the ordering
    is unexercised rather than validated -- do not read the clean corpus
    result as evidence it is right.

    Resolution is by how much trouble a rectangle causes: repeatedly drop
    whichever one crosses the MOST others, breaking ties on the lower
    score and then the smaller area, until none cross. Deterministic, and
    independent of input order because every comparison is recomputed over
    the whole surviving set.
    """
    keep = list(rects)
    while True:
        counts = [sum(1 for b in keep if b is not a and _crosses(a, b))
                  for a in keep]
        worst = max(counts) if counts else 0
        if worst == 0:
            return keep
        victims = [(c, -k["score"], -(k["R"] - k["L"]) * (k["B"] - k["T"]), i)
                   for i, (k, c) in enumerate(zip(keep, counts)) if c == worst]
        # max on (crossings, -score, -area) -> most crossings, then lowest
        # score, then smallest.
        drop = max(victims)[3]
        keep.pop(drop)
