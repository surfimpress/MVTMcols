"""Stage 2: recover the page's underlying column grid.

Premise (see instructions/typesetting_practice.md): a newspaper page is
assembled on a fixed grid that the compositor aligned to -- non-repro
blue guides on a pasteboard, later master-page guides in PageMaker or
QuarkXPress. The grid is an INPUT to the page. It is four numbers:

    left margin | column width | gutter | column count

and every photo, ad and story block occupies an INTEGER number of
columns, never a fraction:

    width = n * col + (n - 1) * gutter

So this does not hunt for boundaries. It **fits four parameters** to the
edges the page already gives us, and treats anything that misses the
lattice as noise rather than as evidence of a new column width.

Per PAGE, not per issue -- by direction, because the photography of
these pages is highly variable (skew, scale, crop differ page to page),
and a single page carries plenty of blocks to fit four numbers.

Method
------
1. Pool the left and right edges of every block and text line.
2. Left edges pile up at column starts, right edges at column ends
   (body text is flush-left, and justified text is flush-right too).
   Histogram both.
3. Grid-search pitch (col + gutter) and offset; score by how many pooled
   edges land within tolerance of the lattice. The winning pitch is the
   one the page was actually set on.
4. Derive column width from the offset->edge distances, and the column
   count from the span of the text area.

Usage::

    python3 -m transcribe.scaled.detect_grid run [--date YYYY-MM-DD]
    python3 -m transcribe.scaled.detect_grid show 1980-04-06 --page 11
    python3 -m transcribe.scaled.detect_grid report
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup

# An edge counts as "on the grid" within this distance (% of page width).
# Generous enough to absorb scan skew and OCR bbox jitter, tight enough
# that a wrong pitch cannot score well.
SNAP_TOL_PCT = 0.9

# Plausible column pitch as % of page width. A 1-column page would be
# ~100%; 12 narrow classified columns ~8%. Outside this, it isn't a
# newspaper column grid.
# A column has to be wide enough to SET BODY TEXT IN. This is the
# typographic floor, and it is what stops the fitter halving a real grid.
#
# Why it was needed: on 1980-04-06 p2 the lattice locked onto the internal
# item/price sub-columns of a full-page grocery ad and returned 14 columns
# at 6.45% pitch, when the obituary and news columns on the same page are
# plainly ~10.5% wide. Rendered and confirmed by eye.
#
# 8.0% of a ~15in broadsheet is ~1.2in, about 7 picas. `typesetting_practice.md`
# records real body columns at 11-13 picas, so nothing legitimate is
# excluded, and the corpus agrees: across 90 fitted pages the halved fits
# sit at 6.25-7.20% pitch and every sound fit at 11.30% or above. The
# threshold sits in an empty gap, not against a cluster edge.
MIN_PITCH_PCT = 8.0
MAX_PITCH_PCT = 55.0

PITCH_STEP = 0.05     # % of page width -- fine enough to land on real pitches
OFFSET_STEP = 0.05

# Edges closer together than this are the same edge seen twice.
EDGE_MERGE_PCT = 0.6

MIN_EDGES = 12        # below this a page cannot support a fit

# A vertical rule this close to either page edge is a scan artefact (the
# sheet edge, the binding shadow), not a column rule. Measured: ~27% of
# tall vertical separators sit there.
EDGE_MARGIN_PCT = 2.0
EDGE_MARGIN_RIGHT_PCT = 97.0

# --- what an edge is WORTH -------------------------------------------
# Edges are weighted by the HEIGHT of the item that produced them, not by
# how many items share an x. A tall block running down a column is strong
# evidence of that column's edge; a pile of small fragments at the same x
# is not, and counting them let noise outvote structure. (Weighting by
# count is still available -- see `peak_counts` -- for comparison.)
#
# Two truncations, both to stop non-layout objects dominating a
# height-weighted measure:
MIN_ITEM_HEIGHT_LINES = 1.5    # taller than one text line, or it says nothing
MAX_ITEM_HEIGHT_FRAC = 0.90    # full-page-height boxes are scan artefacts
                               # (photo shadows, page-edge blobs), not columns

# Edge sources and weighting, both measured against an INDEPENDENT ground
# truth (the printed vertical rules Tesseract reports as ocr_separator,
# which take no part in the fit). 36 pages carrying >=3 such rules:
#
#   variant                          median  mean   p90   within 1%
#   blocks+lines, COUNT (original)    0.93   2.56   4.56     52%
#   blocks+lines, HEIGHT              0.94   1.46   2.97     52%
#   blocks only,  HEIGHT              1.00   1.82   3.11     51%
#   blocks only,  COUNT               0.89   1.28   3.02     56%
#
# Two things that measurement says, neither obvious beforehand:
#  - Height weighting barely moves the MEDIAN but roughly halves the mean
#    and cuts the p90 tail. It buys robustness, not typical accuracy.
#  - Excluding hOCR LINES is the larger effect. A line is one line tall by
#    definition, so under a height-weighted measure the 538 lines on a
#    page contribute a flat, low-weight haze that blurs the block peaks.
# BLOCKS ARE WHAT IS MEASURED. hOCR lines are referred to for exactly one
# purpose -- deriving the minimum item height (a block must be taller than
# MIN_ITEM_HEIGHT_LINES text lines to count) -- and contribute no edges of
# their own.
WEIGHT_BY_HEIGHT = True        # item height is the Y measure

# `fit` below is a DIAGNOSTIC, not a gate. Confidence scoring with an
# escalation threshold was tried and abandoned -- see
# transcribe/scaled/archive/README.md. Report the number, look at the
# page, don't let a self-authored score decide anything.


# --- refinement: subsume stray blocks -------------------------------
# A block wholly inside another, much narrower than it, and only a few
# lines tall, is not an independent layout element -- it is a fragment
# Tesseract split out of its parent (a drop cap, a price, a stray line
# of an ad). Left in place, these fragments contribute edges at
# arbitrary x positions and blur the grid fit.

MAX_SUBSUME_WIDTH_FRAC = 0.5   # narrower than half the parent
MAX_SUBSUME_LINES = 3          # and no more than this many hOCR lines


def median_line_height(conn, page_id: str) -> float:
    """Median hOCR line height on the page, as the unit for truncation."""
    hs = [r["h"] for r in conn.execute(
        "SELECT (bottom_pct - top_pct) AS h FROM page_hocr_lines WHERE page_id=?",
        (page_id,)) if r["h"] > 0]
    return statistics.median(hs) if hs else 1.0


def usable(item: dict, line_h: float) -> bool:
    """Is this item's height meaningful for locating a column edge?"""
    h = item["B"] - item["T"]
    return (h >= line_h * MIN_ITEM_HEIGHT_LINES
            and h <= 100.0 * MAX_ITEM_HEIGHT_FRAC)


def vertical_rules(conn, page_id: str, line_h: float) -> list[dict]:
    """Printed vertical rules (ocr_separator) as layout items.

    A rule sits INSIDE the gutter, so its edges map to the columns either
    side of it, crossed over:
        rule.L  ->  the preceding column's RIGHT edge
        rule.R  ->  the following column's LEFT edge
    Mapping left-to-left would place every column an entire rule-width
    off. Same height minimums as blocks.

    NOTE: separators were previously used as an INDEPENDENT ground truth
    for grading the fit. Feeding them in makes that comparison circular --
    any future validation must use something these do not contribute to.
    """
    out = []
    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_separator' "
        "AND orientation='vertical'", (page_id,),
    ):
        centre = (r["L"] + r["R"]) / 2
        # Page-edge rules are scan artefacts (the sheet edge, binding
        # shadow), not column rules. ~27% of tall vertical separators sit
        # there -- measured. Including them dragged column 0's left edge
        # to 0.60% and squeezed the last column.
        if centre < EDGE_MARGIN_PCT or centre > EDGE_MARGIN_RIGHT_PCT:
            continue
        item = {"block_idx": None, "L": r["L"], "T": r["T"],
                "R": r["R"], "B": r["B"], "n_lines": 0,
                "is_rule": True, "is_weak": True}
        if usable(item, line_h):
            out.append(item)
    return out


def photo_regions(conn, page_id: str, line_h: float) -> list[dict]:
    """Tesseract's own ocr_photo regions as weak layout items.

    A photo is dummied onto the grid like anything else, so its edges are
    column edges -- free evidence that costs nothing to read.

    Two differences from `vertical_rules`:
      * Edges map STRAIGHT THROUGH (L->left, R->right). A rule sits in the
        gutter and has to be crossed over; a photo sits ON the columns.
      * Same reduced weight as a rule. Tesseract's photo boxes are only
        approximately placed and it reports halftone noise as photo too,
        so they inform the fit without being allowed to drive it.

    Same height minimums and full-height truncation as every other item.
    """
    out = []
    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_photo'",
        (page_id,),
    ):
        item = {"block_idx": None, "L": r["L"], "T": r["T"], "R": r["R"],
                "B": r["B"], "n_lines": 0, "is_weak": True}
        if usable(item, line_h):
            out.append(item)
    return out


def page_blocks(conn, page_id: str) -> list[dict]:
    """Blocks with their hOCR line counts, ready for refinement."""
    counts = {r["block_idx"]: r["n"] for r in conn.execute(
        "SELECT block_idx, count(*) AS n FROM page_hocr_lines "
        "WHERE page_id=? GROUP BY block_idx", (page_id,))}
    out = []
    for r in conn.execute(
        "SELECT block_idx, bbox_left_pct L, bbox_top_pct T, bbox_right_pct R, "
        "bbox_bottom_pct B FROM page_ocr_blocks WHERE page_id=? ORDER BY block_idx",
        (page_id,),
    ):
        if r["R"] - r["L"] < 0.5:
            continue
        out.append({"block_idx": r["block_idx"], "L": r["L"], "T": r["T"],
                    "R": r["R"], "B": r["B"], "n_lines": counts.get(r["block_idx"], 0)})
    return out


def _contains(outer: dict, inner: dict, tol: float = 0.3) -> bool:
    return (inner["L"] >= outer["L"] - tol and inner["R"] <= outer["R"] + tol
            and inner["T"] >= outer["T"] - tol and inner["B"] <= outer["B"] + tol)


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


def edges_from_blocks(blocks: list[dict]
                      ) -> tuple[list[float], list[float], list[float]]:
    """(lefts, rights, heights) -- heights are the edge weights."""
    return ([round(b["L"], 2) for b in blocks],
            [round(b["R"], 2) for b in blocks],
            [round(b["B"] - b["T"], 3) for b in blocks])


def line_edges(conn, page_id: str, exclude_block_idx: set | None = None
               ) -> tuple[list[float], list[float], list[float]]:
    """Line edges (+ heights). NOT used by the fit -- blocks are what is
    measured. Retained for inspection and for `pooled_edges`, which the
    plots use to show what the line-level distribution looks like."""
    lefts, rights, heights = [], [], []
    for r in conn.execute(
        "SELECT block_idx, left_pct L, right_pct R, top_pct T, bottom_pct B "
        "FROM page_hocr_lines WHERE page_id=?", (page_id,),
    ):
        if r["R"] - r["L"] < 0.5:
            continue
        if exclude_block_idx and r["block_idx"] in exclude_block_idx:
            continue
        lefts.append(round(r["L"], 2))
        rights.append(round(r["R"], 2))
        heights.append(round(r["B"] - r["T"], 3))
    return lefts, rights, heights


def pooled_edges(conn, page_id: str) -> tuple[list[float], list[float]]:
    """Left and right edges of every block and line on the page.

    Blocks and lines are pooled deliberately: they are set to the same
    grid, so pooling gives the fit more evidence without adding any new
    assumption.
    """
    bl, br, _ = edges_from_blocks(page_blocks(conn, page_id))
    ll, lr, _ = line_edges(conn, page_id)
    return bl + ll, br + lr


def peak_counts(vals: list[float], bin_pct: float = 0.25) -> dict[int, int]:
    """How many edges fall in each bin. Retained for comparison against
    the height-weighted measure now used for fitting."""
    out: dict[int, int] = {}
    for v in vals:
        b = int(v / bin_pct)
        out[b] = out.get(b, 0) + 1
    return out


def _peaks(vals: list[float], weights: list[float] | None = None,
           bin_pct: float = 0.25, min_share: float = 0.015
           ) -> list[tuple[float, float]]:
    """Cluster centres of a 1-D edge distribution, with their weights.

    `weights` is the height of the item that produced each edge; without
    it every edge counts 1 (the old count-based behaviour).
    """
    if not vals:
        return []
    if weights is None:
        weights = [1.0] * len(vals)
    hist: dict[int, list[float]] = {}
    wsum: dict[int, float] = {}
    for v, wt in zip(vals, weights):
        b = int(v / bin_pct)
        hist.setdefault(b, []).append(v)
        wsum[b] = wsum.get(b, 0.0) + wt
    total = sum(wsum.values())
    floor = total * min_share
    keep = sorted(b for b in hist if wsum[b] >= floor)
    if not keep:
        return []
    out, run = [], [keep[0]]
    for b in keep[1:]:
        if (b - run[-1]) * bin_pct <= EDGE_MERGE_PCT:
            run.append(b)
        else:
            out.append(run)
            run = [b]
    out.append(run)
    peaks = []
    for grp in out:
        xs = [x for b in grp for x in hist[b]]
        peaks.append((round(statistics.median(xs), 2),
                      round(sum(wsum[b] for b in grp), 2)))
    return peaks


def _score_lattice(peaks: list[tuple[float, int]], offset: float, pitch: float,
                   lo: float, hi: float) -> float:
    """Chance-corrected share of edges landing on offset + k*pitch.

    The raw hit rate CANNOT be used directly: a finer lattice catches
    more edges by luck, so raw score rises monotonically as pitch falls
    and the fit collapses to the smallest allowed pitch. (Observed: a
    known 7-column page fitted as 15 columns at 6% pitch.)

    Each lattice line accepts a band of +/-SNAP_TOL_PCT, so a uniformly
    random edge hits with probability min(1, 2*tol/pitch). Subtracting
    that and renormalising gives Cohen's-kappa-style agreement: 0 means
    "no better than a lattice of that density would do by chance", 1
    means every alignment position is explained.

    Scored over PEAKS (weighted by how many edges formed each), not over
    raw edges. Most edges on a newspaper page are not grid-aligned at all
    -- ad interiors, centred headlines, captions -- so scoring every edge
    understates a correct grid badly: 1980-04-06 p11, visually verified
    as landing on the page's real printed rules, scored only 0.20 that
    way. A peak is an alignment position the page actually uses, which is
    what the grid is supposed to explain.
    """
    if pitch <= 0:
        return 0.0
    hit = tot = 0
    for e, wgt in peaks:
        if e < lo - SNAP_TOL_PCT or e > hi + SNAP_TOL_PCT:
            continue
        tot += wgt
        k = round((e - offset) / pitch)
        if abs((offset + k * pitch) - e) <= SNAP_TOL_PCT:
            hit += wgt
    if not tot:
        return 0.0
    observed = hit / tot
    chance = min(1.0, (2 * SNAP_TOL_PCT) / pitch)
    if chance >= 1.0:
        return 0.0
    return max(0.0, (observed - chance) / (1.0 - chance))


def fit_grid(lefts: list[float], rights: list[float],
             lw: list[float] | None = None,
             rw: list[float] | None = None) -> dict | None:
    """Fit margin / column width / gutter / column count.

    `lw`/`rw` are per-edge weights -- item heights. Passing them makes a
    tall block count for more than a small fragment at the same x.
    """
    if len(lefts) + len(rights) < MIN_EDGES:
        return None

    left_peaks = _peaks(lefts, lw)
    right_peaks = _peaks(rights, rw)
    if not left_peaks or not right_peaks:
        return None
    all_peaks = left_peaks + right_peaks

    text_left = min(p for p, _ in left_peaks)
    text_right = max(p for p, _ in right_peaks)
    span = text_right - text_left
    if span < MIN_PITCH_PCT:
        return None

    # Search pitch directly. An earlier version forced pitch = span/n,
    # which silently assumes the LAST column *starts* at the text right
    # edge -- it actually *ends* there, so that formula understates pitch
    # by roughly gutter/n and drags the whole lattice left. On
    # 1980-04-06 p11 it put lines at 49.5 and 72.1 while the page's real
    # alignment peaks sat at 52 and 75.
    #
    # Correct relation, from the typesetting model:
    #     span = (n - 1) * pitch + col_width
    # Rather than solve that with col_width unknown, scan pitch and let
    # the score decide; n then falls out of the span.
    best = None
    steps = int((MAX_PITCH_PCT - MIN_PITCH_PCT) / PITCH_STEP) + 1
    for i in range(steps):
        pitch = MIN_PITCH_PCT + i * PITCH_STEP
        ncols = int(round(span / pitch))
        if ncols < 1 or ncols > 20:
            continue
        # Offset may drift off text_left with skew or a hanging indent.
        for d in range(-8, 9):
            offset = text_left + d * OFFSET_STEP
            sc = _score_lattice(all_peaks, offset, pitch, text_left, text_right)
            if best is None or sc > best["score"] + 1e-9:
                best = {"score": round(sc, 3), "offset": round(offset, 2),
                        "pitch": round(pitch, 2), "n_columns": ncols}
    if best is None:
        return None

    # Column width = start -> the column's DOMINANT right-edge peak.
    # Use the heaviest peak, not the furthest: body text right-aligns at
    # the column end, but stray boxes (rules, overhanging headlines) sit
    # further right and would swallow the gutter. Taking max() gave a
    # 0.3% gutter on a page whose real gutter is ~1 pica.
    widths = []
    for k in range(best["n_columns"]):
        start = best["offset"] + k * best["pitch"]
        end = start + best["pitch"]
        # Only peaks in the slot's END REGION can be a column end. An
        # earlier version accepted any peak inside the slot, which let a
        # wide-spanning item (a photo, a multi-column ad) that happens to
        # stop early set col_width -- collapsing the column and inflating
        # the gutter to 8-13% on pages whose real gutter is ~1 pica.
        floor_x = start + (end - start) * COL_END_ZONE_FRAC
        inside = [(cnt, p) for p, cnt in right_peaks
                  if floor_x < p <= end + SNAP_TOL_PCT]
        if inside:
            widths.append(max(inside)[1] - start)
    col_w = round(statistics.median(widths), 2) if widths else round(best["pitch"], 2)
    gutter = round(max(0.0, best["pitch"] - col_w), 2)

    edges_out = [round(best["offset"] + k * best["pitch"], 2)
                 for k in range(best["n_columns"] + 1)]
    return {**best, "text_left": round(text_left, 2), "text_right": round(text_right, 2),
            "col_width": col_w, "gutter": gutter, "edges": edges_out,
            "n_edges": len(lefts) + len(rights), "n_peaks": len(all_peaks)}


# How far a column edge may be pulled to meet the majority alignment in
# pass 2. Wide enough to absorb scan scale drift across the page, narrow
# enough that a column cannot migrate into its neighbour's slot.
SNAP_SEARCH_PCT = 2.0

# Blocks in the bottom decile by height are dropped before an edge is
# chosen. Extreme-lean selection is by definition sensitive to whatever
# sits furthest out, so the shortest items -- the ones least likely to be
# real column structure -- must not be allowed to set an edge.
HEIGHT_PCTL_FLOOR = 10

# A printed rule or a photo box is real evidence, but weaker than a text
# block: rules get confused with photo borders, box edges and scan
# artefacts, photo boxes are only loosely placed, and misaligned pages
# traced to exactly that confusion. Text blocks count full value;
# separators and photo regions count half.
WEAK_WEIGHT_FRAC = 0.5

# For the LAST column only. The right margin is ragged: most lines stop
# short of the column edge, so the heaviest right-edge cluster sits LEFT
# of the truth and snapping to it makes the last column too narrow.
#
# Only items at least this fraction of the column width contribute a
# right edge for that decision.
#
# MEASURED, and the result is worth stating: sweeping this from 0.0 to
# 0.9 changes the outcome on ONE page in 90. The reason is that short
# items end further LEFT, so they cannot bias a rightmost-selection at
# all -- taking the max already immunises against them. The filter's real
# and much narrower job is to stop a thin OVERHANGING fragment from
# setting the edge, which is rare here. Kept as a cheap guard, not as a
# tuning knob: do not expect gains from adjusting it.
LAST_COL_MIN_ITEM_FRAC = 0.60

# A column's right edge must lie in the last part of its slot. Anything
# earlier is an item that stopped short, not the column measure.
COL_END_ZONE_FRAC = 0.70

# Below this many text lines a page cannot evidence a column grid, and a
# fit is a guess dressed as a measurement. 1980-04-06 p7 is the case:
# a full-page picture spread with 25 lines, all captions and no body text.
# It fitted 5 columns where the issue's grid is 8 -- but 8 was never
# verified either, because there is nothing on the page to verify against.
# Flagged rather than silently returned.
MIN_LINES_FOR_GRID = 60


def analyse(conn, page_id: str, blocks: list[dict] | None = None,
            exclude_block_idx: set | None = None) -> dict | None:
    """THE reusable analysis: edges -> fitted grid.

    Run once on the raw blocks and again after refinement, so both passes
    are guaranteed to be the same computation on different input.
    """
    blocks = page_blocks(conn, page_id) if blocks is None else blocks
    line_h = median_line_height(conn, page_id)

    # Truncation: drop items shorter than MIN_ITEM_HEIGHT_LINES text lines
    # and taller than MAX_ITEM_HEIGHT_FRAC of the page. The first removes
    # single-line fragments that carry no column information; the second
    # removes full-height scan artefacts (photo shadows, page-edge blobs)
    # that would otherwise dominate a height-weighted fit.
    ok = [b for b in blocks if usable(b, line_h)]
    rules = vertical_rules(conn, page_id, line_h)
    photos = photo_regions(conn, page_id, line_h)
    items = ok + rules + photos
    bl = tall_edges(items, "left")
    br = tall_edges(items, "right")
    bh = item_weights(items)
    w = bh if (WEIGHT_BY_HEIGHT and len(bh) == len(bl)) else None
    return fit_grid(bl, br, w, w)


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


def item_weights(items: list[dict]) -> list[float]:
    """Weight per surviving item: its height, halved for printed rules.

    Rules and photo regions are genuine evidence but less trustworthy than
    text -- Tesseract reports photo borders and box edges as separators
    too, and pages that came out misaligned traced to exactly that
    confusion.
    """
    floor = _decile_floor(items)
    out = []
    for i in items:
        h = i["B"] - i["T"]
        if h < floor:
            continue
        out.append(h * (WEAK_WEIGHT_FRAC if i.get("is_weak") else 1.0))
    return out


def line_right_extent(conn, page_id: str, left_bound: float,
                      exclude_block_idx: set | None = None) -> float | None:
    """Rightmost edge of any hOCR LINE in a block starting at or right of
    `left_bound`.

    This is the one place lines are consulted for geometry. A block's
    bbox can under-report its true extent, but its lines cannot: the
    longest line in the rightmost block marks where text actually reaches.
    The column must not end to the LEFT of that -- doing so would clip
    real text out of the column.
    """
    rows = conn.execute(
        """SELECT max(l.right_pct) AS mx
             FROM page_hocr_lines l
             JOIN page_ocr_blocks b
               ON b.page_id = l.page_id AND b.block_idx = l.block_idx
            WHERE l.page_id = ? AND b.bbox_left_pct >= ?""",
        (page_id, left_bound),
    ).fetchone()
    if not rows or rows["mx"] is None:
        return None
    return round(rows["mx"], 2)


def _decile_floor(items: list[dict]) -> float:
    if not items:
        return 0.0
    hs = sorted(i["B"] - i["T"] for i in items)
    return hs[min(len(hs) - 1, int(len(hs) * HEIGHT_PCTL_FLOOR / 100))]


def tall_edges(items: list[dict], side: str) -> list[float]:
    """Edges from all but the shortest decile of items, by height.

    For a printed rule the sides are swapped: the rule lies in the
    gutter, so its LEFT edge is a column's RIGHT boundary and vice
    versa.
    """
    if not items:
        return []
    heights = sorted(i["B"] - i["T"] for i in items)
    idx = min(len(heights) - 1, int(len(heights) * HEIGHT_PCTL_FLOOR / 100))
    floor = heights[idx]
    out = []
    for i in items:
        if (i["B"] - i["T"]) < floor:
            continue
        if i.get("is_rule"):
            key = "R" if side == "left" else "L"
        else:
            key = "L" if side == "left" else "R"
        out.append(round(i[key], 2))
    return out


def detect(conn, page_id: str) -> dict:
    """Two passes.

    PASS 1 establishes the likely columns -- pitch, offset, column width,
    column count -- from the raw blocks. That is a rigid lattice, and a
    rigid lattice cannot follow the scan's own scale drift across the
    page (measured: edges landing ~1.3% right of their slot at the
    right-hand end while fitting well on the left).

    Refinement then subsumes stray fragment blocks, which contribute
    edges at arbitrary x and blur the peaks.

    PASS 2 re-runs the same analysis on the cleaned blocks and then
    refines each column to the MAJORITY ALIGNMENT: every column edge is
    pulled to the heaviest nearby edge peak. The result follows the page
    as printed rather than holding a perfect lattice the page never had.
    """
    blocks = page_blocks(conn, page_id)
    first = analyse(conn, page_id, blocks)

    kept, subsumed = subsume_stray_blocks(blocks)
    dropped = {b["block_idx"] for b in subsumed}
    second = analyse(conn, page_id, kept, exclude_block_idx=dropped)

    # Pass 2 REFINES pass 1's reading; it does not get to overturn it.
    # Subsuming stray blocks changes the edge distribution slightly, so a
    # correction of one column either way is a legitimate sharpening -- but
    # a jump to twice as many columns means pass 2 has found a
    # sub-division (an ad's internal price columns, a table's cells), not
    # a better grid. Seen live: 1980-04-06 p12 went 4 -> 8 this way.
    grid = second or first
    if first and second and abs(second["n_columns"] - first["n_columns"]) > 1:
        grid = first
        count_guarded = (second["n_columns"], first["n_columns"])
    else:
        count_guarded = None

    if grid is None:
        return {"grid": None, "fit": 0.0, "note": "insufficient edges",
                "n_blocks": len(blocks), "n_kept": len(kept),
                "subsumed": len(subsumed), "subsumed_blocks": subsumed}

    # Majority-alignment refinement, using the cleaned edge distribution.
    line_h = median_line_height(conn, page_id)
    ok = [b for b in kept if usable(b, line_h)]
    weak = (vertical_rules(conn, page_id, line_h)
            + photo_regions(conn, page_id, line_h))
    left_edges = tall_edges(ok + weak, "left")
    right_edges = tall_edges(ok + weak, "right")

    # LEFT edges first, for every column. Each leans leftward to the
    # outermost block start near its predicted slot, bounded so it cannot
    # cross back past the previous slot.
    n = grid["n_columns"]
    lefts, hit_left = [], []
    for k in range(n):
        pl = grid["offset"] + k * grid["pitch"]
        prev_right = (grid["offset"] + (k - 1) * grid["pitch"]
                      + grid["col_width"]) if k else None
        x, hit = _lean(pl, left_edges, SNAP_SEARCH_PCT, rightward=False,
                       lo=prev_right)
        lefts.append(x)
        hit_left.append(hit)

    # RIGHT edges second, each bounded by the ACTUAL left edge of the next
    # column less a minimum gutter. Bounding against the *predicted* next
    # left was not enough: the next column then leans left to that same
    # prediction and the two meet, collapsing the gutter to zero.
    min_gutter = max(0.25, grid["gutter"] * 0.5)
    columns, moved = [], 0
    for k in range(n):
        hi = (lefts[k + 1] - min_gutter) if k + 1 < n else None
        right, hit_r = _lean(lefts[k] + grid["col_width"], right_edges,
                             SNAP_SEARCH_PCT, rightward=True, hi=hi)
        if right - lefts[k] < grid["col_width"] * 0.5:
            right = round(lefts[k] + grid["col_width"], 2)
            hit_r = False
        if k == n - 1:
            # LAST column: a block bbox can under-report its extent, but
            # its LINES cannot. The rightmost line in the rightmost block
            # marks where text actually reaches, and the column must not
            # end left of that or it would clip real text.
            ext = line_right_extent(conn, page_id, lefts[k] - SNAP_SEARCH_PCT, dropped)
            if ext is not None and ext > right:
                right = ext
                hit_r = True
        moved += int(hit_left[k]) + int(hit_r)
        columns.append({"col_idx": k, "left_pct": round(lefts[k], 2),
                        "right_pct": round(right, 2),
                        "snapped_left": hit_left[k], "snapped_right": hit_r})

    n_lines = conn.execute(
        "SELECT count(*) c FROM page_hocr_lines WHERE page_id=?",
        (page_id,)).fetchone()["c"]

    return {"grid": grid, "fit": grid["score"],
            "count_guarded": count_guarded,
            "low_evidence": n_lines < MIN_LINES_FOR_GRID,
            "n_lines": n_lines,
            "fit_before_refine": first["score"] if first else None,
            "columns": columns,
            "edges_snapped": moved, "edges_total": 2 * grid["n_columns"],
            "n_blocks": len(blocks), "n_kept": len(kept),
            "subsumed": len(subsumed), "subsumed_blocks": subsumed}


def store(conn, page_id: str, res: dict) -> None:
    """Persist the pass-2 (majority-aligned) columns -- that is the
    answer. The pass-1 lattice parameters go in `notes` so the rigid fit
    the refinement started from stays inspectable."""
    conn.execute("DELETE FROM page_columns WHERE page_id=? AND method='grid'",
                 (page_id,))
    g = res.get("grid")
    if not g or not res.get("columns"):
        return
    now = _sup.now_iso()
    for c in res["columns"]:
        conn.execute(
            """INSERT INTO page_columns
               (id, page_id, col_idx, left_pct, right_pct, method, confidence,
                created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, c["col_idx"], c["left_pct"], c["right_pct"],
             "grid", res["fit"], now,
             f"pass1 pitch={g['pitch']} col={g['col_width']} gutter={g['gutter']}; "
             f"snapped L={c['snapped_left']} R={c['snapped_right']}"))
    conn.commit()


def pages_to_run(conn, date: str | None) -> list[dict]:
    sql = "SELECT id, year, month, day, page FROM pages WHERE hocr_parsed_at IS NOT NULL"
    params: list = []
    if date:
        y, m, d = (int(x) for x in date.split("-"))
        sql += " AND year=? AND month=? AND day=?"
        params += [y, m, d]
    return [dict(r) for r in conn.execute(sql + " ORDER BY year,month,day,page", params)]


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = pages_to_run(conn, args.date)
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            g = res.get("grid")
            desc = (f"{g['n_columns']} col  pitch={g['pitch']:.2f}%  "
                    f"col={g['col_width']:.2f}%  gutter={g['gutter']:.2f}%"
                    if g else "no fit")
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{desc}  fit={res['fit']:.2f}  "
                  f"subsumed={res.get('subsumed', 0)}")
        print(f"\n{len(rows)} page(s) fitted.")
    finally:
        conn.close()


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? AND day=? "
                           "AND page=?", (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        res = detect(conn, row["id"])
        g = res.get("grid")
        if not g:
            print("  no fit:", res.get("note"))
            return
        print(f"  text area   : {g['text_left']}% .. {g['text_right']}%")
        print(f"  columns     : {g['n_columns']}")
        print(f"  pitch       : {g['pitch']}%  (column {g['col_width']}% + gutter {g['gutter']}%)")
        print(f"  edges       : {g['edges']}")
        print(f"  fit          : {g['score']:.2f}  "
              f"(peak weight explained, chance-corrected)")
        print(f"  refinement   : {res['n_blocks']} blocks -> {res['n_kept']} "
              f"({res['subsumed']} stray subsumed); "
              f"{res['edges_snapped']}/{res['edges_total']} edges snapped "
              f"to majority alignment")
        print("  pass 2 columns (majority-aligned):")
        for c in res["columns"]:
            gut = ""
            nxt = next((x for x in res["columns"] if x["col_idx"] == c["col_idx"] + 1), None)
            if nxt:
                gut = f"  gutter {nxt['left_pct'] - c['right_pct']:+.2f}%"
            print(f"    col {c['col_idx']}: {c['left_pct']:6.2f}% -> {c['right_pct']:6.2f}%"
                  f"  (w {c['right_pct'] - c['left_pct']:5.2f}%){gut}")
    finally:
        conn.close()


def _cmd_report(args):
    conn = _sup.open_connection()
    try:
        rows = conn.execute(
            "SELECT page_id, confidence, count(*) n FROM page_columns "
            "WHERE method='grid' GROUP BY page_id").fetchall()
        if not rows:
            print("no results; run first")
            return
        confs = [r["confidence"] for r in rows]
        print(f"pages fitted: {len(rows)}")
        print(f"fit  min={min(confs):.2f} median={statistics.median(confs):.2f} "
              f"max={max(confs):.2f}   (diagnostic only, not a gate)")
        import collections
        print("column counts:", dict(sorted(collections.Counter(
            r["n"] for r in rows).items())))
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("run"); pr.add_argument("--date"); pr.set_defaults(func=_cmd_run)
    ps = sub.add_parser("show"); ps.add_argument("date")
    ps.add_argument("--page", type=int, required=True); ps.set_defaults(func=_cmd_show)
    pp = sub.add_parser("report"); pp.set_defaults(func=_cmd_report)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
