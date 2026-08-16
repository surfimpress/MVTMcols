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

# Two corners are on the same line if they are within this, in page
# percent. Rules are ~0.4% thick and corner marks resolve to about a
# cell, so this has to absorb both.
LINE_TOL = 0.9

# A corner counts as interrupting a side if it sits within this of the
# side's line, and is not one of the rectangle's OWN corners. The
# exclusion is exactly LINE_TOL -- the same distance that decides whether
# two corners are the same corner -- and nothing wider. A separate,
# larger margin was tried and it swallowed real dividers: the two rules
# between ELECTRICAL INSTALLATION and NOTICE sit 1.06% apart, so a 1.2%
# end margin treated each as the other's own corner and both ads came out
# in four overlapping variants.
ON_LINE_TOL = 0.9

# A side of a saleable ad. Below this it is furniture or a rule artefact.
MIN_SIDE_PCT = 2.0


def _lines(values, tol=LINE_TOL):
    """1-D clustering by sorting and splitting on gaps. Order-independent."""
    if not values:
        return []
    vals = sorted(values)
    groups, run = [], [vals[0]]
    for v in vals[1:]:
        if v - run[-1] <= tol:
            run.append(v)
        else:
            groups.append(run)
            run = [v]
    groups.append(run)
    return [sum(g) / len(g) for g in groups]


def _interrupted(x0, y0, x1, y1, corners):
    """Does any corner sit in the OPEN interior of one of the four sides?"""
    for (cx, cy) in corners:
        # left / right sides: same x, y strictly between
        for sx in (x0, x1):
            if abs(cx - sx) <= ON_LINE_TOL and y0 + LINE_TOL < cy < y1 - LINE_TOL:
                return True
        # top / bottom sides: same y, x strictly between
        for sy in (y0, y1):
            if abs(cy - sy) <= ON_LINE_TOL and x0 + LINE_TOL < cx < x1 - LINE_TOL:
                return True
    return False


def ad_rectangles(corners, column_lines=(), photos=(),
                  min_side=MIN_SIDE_PCT):
    """Most likely ad rectangles, from corner points alone.

    corners       [(x, y)]        page percent
    column_lines  [x]             gutter centres and content edges
    photos        [(L, T, R, B)]  page percent

    Returns dicts sorted by score, each with the rectangle, the score and
    the reasons behind it, so a caller can see WHY a rectangle ranked
    where it did rather than take the number on trust.
    """
    if not corners:
        return []

    xs = _lines([c[0] for c in corners])
    ys = _lines([c[1] for c in corners])

    def has_corner(x, y):
        return any(abs(cx - x) <= LINE_TOL and abs(cy - y) <= LINE_TOL
                   for (cx, cy) in corners)

    out = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            if x1 - x0 < min_side:
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    if y1 - y0 < min_side:
                        continue
                    if not (has_corner(x0, y0) and has_corner(x1, y0)
                            and has_corner(x0, y1) and has_corner(x1, y1)):
                        continue
                    # THE test.
                    if _interrupted(x0, y0, x1, y1, corners):
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
                                "score": round(score, 2), "reasons": reasons})
    return sorted(out, key=lambda r: (-r["score"],
                                      -(r["R"] - r["L"]) * (r["B"] - r["T"])))
