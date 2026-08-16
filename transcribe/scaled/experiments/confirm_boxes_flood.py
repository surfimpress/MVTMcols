"""EXPERIMENT — confirm boxed zones by an INDEPENDENT method.

Not wired into the pipeline. Kept runnable so the numbers below can be
reproduced.

THE QUESTION
------------
`detect_boxes` builds rectangles by pairing rules. Its own output cannot
tell us whether that pairing is right -- the recurring failure in this
project is a stage grading itself (see archive/README.md). So: is there
an established algorithm that derives bounded areas from the same rules
by a genuinely different route, usable as confirmation?

THE ESTABLISHED OPTIONS
-----------------------
* **Planar arrangement face extraction** (computational geometry; CGAL's
  2D Arrangements is the reference implementation). Build the arrangement
  of all segments and enumerate its bounded faces. Equivalently: minimal
  cycles in the graph whose nodes are rule intersections. This is the
  rigorous answer to "what do these segments bound".
* **Morphological line extraction + contour finding** -- the standard in
  table-structure recognition: isolate horizontal/vertical strokes with
  directional kernels, then connected components / findContours.
* **Classic page segmentation** -- recursive XY-cut (Nagy & Seth), RLSA
  (Wong/Casey/Wahl), Docstrum (O'Gorman), Voronoi (Kise). These segment
  regions rather than ruled boxes, so they are less apt here.

THE OBSTACLE, which we already measured
---------------------------------------
Rounded corners mean our segments DO NOT INTERSECT (0.5-3.9% inset,
measured on 1980-04-06 p8). A strict planar arrangement therefore finds
no faces at all. Every method above needs a snapping or dilation step
first -- which is precisely what `INSET_PCT` does by hand in the
detector.

WHAT THIS IMPLEMENTS
--------------------
The simplest independent variant: rasterise ONLY the rules, dilate to
close the corner gaps, flood-fill from the page border, and report every
background component the flood cannot reach. Those are areas the rules
enclose. It shares no logic with the pairing heuristic -- no pair loop,
no inset test, no thickness reasoning -- so agreement is real evidence.

RESULT, 1980-04-06
------------------
    page   heuristic boxes   flood regions   matched (IoU >= 0.5)
    p13          15               8                8/8
    p5            1               1                1/1
    p6           10               6                5/6
    p8           49              38               24/38

**The disagreement is structural, not error.** Flood-fill only ever finds
LEAF cells: a container's interior is subdivided by its own inner rules,
so the flood never sees the container as one region. That is why p8 --
Fraser's Meat Market and the Sidewalk Sale grid, both heavily subdivided
-- has the lowest match rate, while p13 (mostly un-subdivided ads) is
8/8.

HOW IT SHOULD BE USED
---------------------
Not as a replacement. As a confirmation channel:

  * a heuristic box that matches a flood region is CORROBORATED by an
    independent derivation
  * a flood region with no matching box is a MISS worth investigating
  * a heuristic box with no flood region is either a legitimate container
    (expected, and identifiable because it contains other boxes) or
    unsupported (worth flagging for the LLM review pass alongside the
    `needs_review` three-sided boxes)

Usage::

    python3 -m transcribe.scaled.experiments.confirm_boxes_flood
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

from .. import _support as _sup
from .. import detect_boxes as _boxes
from .. import detect_grid as _grid

GRID = 1000          # raster resolution; a rule is >=1px at this size
CLOSE_PCT = 1.2      # dilation to bridge rounded-corner gaps
MIN_AREA_FRAC = 0.0004   # ignore specks below 0.04% of the page


def enclosed_regions(conn, page_id: str, grid: int = GRID,
                     close_pct: float = CLOSE_PCT) -> list[tuple]:
    """Bounding boxes of every area the rules enclose."""
    g = np.zeros((grid, grid), bool)
    for orient in ("horizontal", "vertical"):
        for r in _boxes._rules(conn, page_id, orient):
            x0 = int(r["L"] / 100 * (grid - 1))
            x1 = int(r["R"] / 100 * (grid - 1))
            y0 = int(r["T"] / 100 * (grid - 1))
            y1 = int(r["B"] / 100 * (grid - 1))
            g[max(0, y0):y1 + 1, max(0, x0):x1 + 1] = True

    k = max(1, int(close_pct / 100 * grid))
    g = ndimage.binary_dilation(g, np.ones((k, k), bool))

    lab, n = ndimage.label(~g)
    border = (set(lab[0, :]) | set(lab[-1, :])
              | set(lab[:, 0]) | set(lab[:, -1]))
    out = []
    for i in range(1, n + 1):
        if i in border:
            continue
        ys, xs = np.where(lab == i)
        if len(ys) < grid * grid * MIN_AREA_FRAC:
            continue
        out.append((xs.min() / grid * 100, ys.min() / grid * 100,
                    xs.max() / grid * 100, ys.max() / grid * 100))
    return out


def iou(a: tuple, b: tuple) -> float:
    x0, x1 = max(a[0], b[0]), min(a[2], b[2])
    y0, y1 = max(a[1], b[1]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / ((a[2] - a[0]) * (a[3] - a[1])
                    + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def main():
    conn = _sup.open_connection()
    try:
        print(f"{'page':>14} {'boxes':>6} {'flood':>6} {'matched':>9}")
        for r in conn.execute(
                "SELECT id, year, month, day, page FROM pages "
                "WHERE hocr_parsed_at IS NOT NULL "
                "ORDER BY year, month, day, page LIMIT 20"):
            cols = _grid.detect(conn, r["id"]).get("columns") or []
            ours = [(b["left_pct"], b["top_pct"], b["right_pct"],
                     b["bottom_pct"]) for b in _boxes.find_boxes(
                        conn, r["id"], cols)]
            theirs = enclosed_regions(conn, r["id"])
            hit = sum(1 for t in theirs
                      if ours and max(iou(t, o) for o in ours) >= 0.5)
            label = f"{r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}"
            print(f"{label:>14} {len(ours):6d} {len(theirs):6d} "
                  f"{hit:4d}/{len(theirs):<4d}")
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
