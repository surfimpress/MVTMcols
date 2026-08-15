"""ARCHIVED — pass 2: stray-block subsumption + majority-alignment leaning.

Set aside 2026-08-15 by the user's direction: "Reviewing the pass 1 and
pass 2 versions, in almost every case, pass one is the better version."

WHAT IT DID
-----------
1. `subsume_stray_blocks` merged a block wholly inside another, under 50%
   of its parent's width and no more than 3 lines tall, into that parent
   -- removing fragments that contribute edges at arbitrary x.
2. `analyse` was re-run on the cleaned blocks.
3. `_lean` then pulled each column edge to the outermost nearby edge
   within SNAP_SEARCH_PCT: left edges leftward, right edges rightward,
   left edges chosen for every column first so a right edge could be
   bounded by the ACTUAL next left minus a minimum gutter.

WHY IT WAS SET ASIDE
--------------------
Pass 1 fits TWO global parameters (pitch, offset) and derives one column
width -- exactly the shape `instructions/typesetting_practice.md`
prescribes, because the page was laid out on a grid that is four fixed
numbers. Pass 2 replaces that with 2n free parameters, one per edge, and
leans each independently toward whatever sits furthest out nearby --
which on these pages includes display-ad interiors set to their own grid.

MEASURED, over 89 fitted pages:
  * pass 1 gutter varies within a page by 0.00% -- constant by construction
  * pass 2 gutter within-page stdev: median 0.42%, mean 0.48%, max 1.24%
  * pass 2 gutter varies by more than 0.30% within the page on 54/89
    pages (61%)

A gutter is one physical measure, about 1 pica, set once on the
pasteboard or the master page. It CANNOT vary down a page. So pass 2's
variation is not the fit following the page -- it is the fit following
noise, and it was degrading a correct answer.

THE ORIGINAL MOTIVATION IS STILL REAL
-------------------------------------
Pass 2 existed because a rigid lattice cannot follow the scan's own scale
drift across the page (measured: edges landing ~1.3% right of their slot
at the right-hand end while fitting well on the left). That problem has
NOT been solved -- it has been left unaddressed in favour of a fit that
is wrong more predictably. If it is revisited, the lesson from the
measurement above is that the correction must stay PARAMETRIC (e.g. one
global scale/skew term fitted across the page, keeping the gutter
constant) rather than per-edge.

Nothing imports this module. It is kept runnable so the comparison can
be reproduced, not because anything depends on it.
"""

from __future__ import annotations

import statistics

# Copied from detect_grid at the time of archiving, so this module does
# not drift when those constants are tuned.
MAX_SUBSUME_WIDTH_FRAC = 0.5
MAX_SUBSUME_LINES = 3
SNAP_SEARCH_PCT = 2.0


def subsume_stray_blocks(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge stray fragments into the block that encloses them.

    A block is subsumed when ALL of:
      - it lies wholly within another block,
      - it is narrower than MAX_SUBSUME_WIDTH_FRAC of that parent,
      - it has at most MAX_SUBSUME_LINES hOCR lines.

    The parent's bbox is unchanged -- the child was already inside it, so
    there is nothing to grow. Returns (kept blocks, subsumed blocks).
    """
    kept, subsumed = [], []
    for i, b in enumerate(blocks):
        if b["n_lines"] > MAX_SUBSUME_LINES:
            kept.append(b)
            continue
        bw = b["R"] - b["L"]
        parent = None
        for j, other in enumerate(blocks):
            if i == j:
                continue
            ow = other["R"] - other["L"]
            if ow <= bw:
                continue
            if bw >= ow * MAX_SUBSUME_WIDTH_FRAC:
                continue
            if _contains(other, b):
                # Smallest qualifying enclosing block is the true parent.
                if parent is None or ow < (parent["R"] - parent["L"]):
                    parent = other
        if parent is None:
            kept.append(b)
        else:
            subsumed.append({**b, "parent_block_idx": parent["block_idx"]})
    return kept, subsumed



def _lean(predicted: float, edges: list[float], window: float,
          rightward: bool, lo: float | None = None,
          hi: float | None = None) -> tuple[float, bool]:
    """Take the most EXTREME edge within `window` of the prediction.

    Averaging a cluster was the wrong move: it pulls an edge towards
    wherever most items happen to stop, which is INSIDE the column, and
    it made pass 2 weaker than pass 1. A column's true left edge is the
    LEFTMOST place its blocks start (anything further right is an indent);
    its true right edge is the RIGHTMOST place they end (anything further
    left is just a short line). Leaning to the extreme recovers the real
    boundary, and a consistent gutter falls out of it.

    `edges` must already have the shortest decile removed -- see
    `tall_edges`. Extreme selection is by construction sensitive to
    whatever sits furthest out, so the least trustworthy items must not
    be able to set an edge.
    """
    near = [x for x in edges if abs(x - predicted) <= window]
    # Hard bounds keep a column inside its own slot. Without them the
    # lean grabs the FAR side of the printed rule that separates it from
    # its neighbour -- the rule is only ~0.5-1% wide, well inside the
    # search window -- and the gutter collapses to zero.
    if lo is not None:
        near = [x for x in near if x >= lo]
    if hi is not None:
        near = [x for x in near if x <= hi]
    if not near:
        return predicted, False
    return round(max(near) if rightward else min(near), 2), True


def refine(conn, page_id, grid, detect_grid):
    """Reproduce pass 2 against a pass-1 `grid`.

    `detect_grid` is the live module, passed in rather than imported so
    this archive never creates a dependency edge into the package.
    """
    blocks = detect_grid.page_blocks(conn, page_id)
    kept, subsumed = subsume_stray_blocks(blocks)
    dropped = {b["block_idx"] for b in subsumed}

    line_h = detect_grid.median_line_height(conn, page_id)
    ok = [b for b in kept if detect_grid.usable(b, line_h)]
    weak = (detect_grid.vertical_rules(conn, page_id, line_h)
            + detect_grid.photo_regions(conn, page_id, line_h))
    left_edges = detect_grid.tall_edges(ok + weak, "left")
    right_edges = detect_grid.tall_edges(ok + weak, "right")

    n = grid["n_columns"]
    lefts = []
    for k in range(n):
        pl = grid["offset"] + k * grid["pitch"]
        prev_right = (grid["offset"] + (k - 1) * grid["pitch"]
                      + grid["col_width"]) if k else None
        x, _ = _lean(pl, left_edges, SNAP_SEARCH_PCT, rightward=False,
                     lo=prev_right)
        lefts.append(x)

    min_gutter = max(0.25, grid["gutter"] * 0.5)
    columns = []
    for k in range(n):
        hi = (lefts[k + 1] - min_gutter) if k + 1 < n else None
        right, _ = _lean(lefts[k] + grid["col_width"], right_edges,
                         SNAP_SEARCH_PCT, rightward=True, hi=hi)
        if right - lefts[k] < grid["col_width"] * 0.5:
            right = round(lefts[k] + grid["col_width"], 2)
        columns.append({"col_idx": k, "left_pct": round(lefts[k], 2),
                        "right_pct": round(right, 2)})
    return {"columns": columns, "subsumed": len(subsumed)}
