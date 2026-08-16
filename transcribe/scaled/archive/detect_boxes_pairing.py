"""Stage 2b — BOXED ZONES: ruled rectangles on the page.

ARCHIVED — superseded by `detect_zones`. Nothing imports this.

It asked "is this a valid rectangle?", and the union of two stacked ads
answers YES -- four ruled edges, because the side rules run continuously
past both. So it produced bridges, gutter slivers and double-rule pairs,
and each needed its own filter: aspect ratio, thin dimension, gap
tolerance, twin collapse, double-rule merge, gutter drop. Six tuned
thresholds, all cleaning up after the generator rather than fixing it.
`detect_zones` asks instead whether any other CORNER interrupts a
rectangle's sides, which rejects all three by construction.

CARRIED FORWARD: the rule preprocessing, which was the real discovery
here and is now `transcribe/scaled/rules.py` -- Tesseract both MERGES
collinear rules from adjacent boxes and SPLITS single rules into
fragments, and both must be undone before the ruling can be read.

NOT CARRIED FORWARD: `n_sides` and the three-sided closure, which
inferred a foot where none was printed. The corner map already carries
that evidence, and an atomic rectangle needs no inferred edge.


A boxed-off area is a deliberate landmark. Most are display ads, but
notices, standing panels, indexes and feature boxes use the same device.
Knowing where they are matters for its own sake, and because a box's
interior is set to its OWN grid, not the page's (see
`instructions/scaled_pipeline.md` §5f on display-ad grid contamination).

THE SIGNATURE: FOUR SIDES, WITH THE PRINT'S OWN QUIRKS ALLOWED FOR
------------------------------------------------------------------
A box is two verticals and two horizontals. Reading them naively fails,
because printed boxes are not clean rectangles. Three properties of the
actual print, each confirmed in the data:

1. **Rounded corners mean the sides never meet.** On 1980-04-06 p8 the
   Fastball standings box has horizontals spanning x 50.82-72.00 while
   its verticals sit at x 50.35 and 72.40 -- the rules stop ~0.5% SHORT
   of the join. CENTENNIAL DOLLARS, with an ornate border, is inset by
   2.5-3.9%. This is why corner-matching scored only 22% when it was
   tried: the corners genuinely do not touch. A side therefore has to
   BRIDGE the box within `INSET_PCT`, not land on its corner.

2. **Drop shadows make opposite sides uneven.** A box with a shadow has a
   markedly heavier rule on the shadowed sides. p8's Sidewalk Sale is
   28px on top against 48px at the bottom; another box measures
   [32, 26, 19, 23]. An earlier version REQUIRED the four sides to match
   in weight and found only 2 boxes on the whole page as a result.
   Thickness is RECORDED (`side_px`) and never used as a filter.

3. **Stacked boxes share their verticals.** POLICE CONSTABLE and
   Congratulations sit in one column inside a single pair of verticals
   running y 39-73. So every horizontal that bridges a vertical pair is
   collected, and a box is emitted between each CONSECUTIVE pair of them
   -- plus one for the whole enclosure, which is the container the strips
   sit inside. That yields Fraser's Meat Market as one box AND its price
   rows, the Sidewalk Sale grid AND its cells.

KNOWN LIMIT, not a bug in this code: some boxes are simply incomplete in
Tesseract's output. Smithson Motor Sales and CENTENNIAL DOLLARS on p8
have no bottom border reported at all, so no geometry over the separators
can recover them. See instructions/scaled_pipeline.md 5k -- pixel-level
rule detection is the route, and Tesseract config tuning is ruled out.

Usage::

    python3 -m transcribe.scaled.detect_boxes show 1980-04-06 --page 11
    python3 -m transcribe.scaled.detect_boxes run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup
from . import detect_grid as _grid
from . import detect_hlines as _hl

# Two rules belong to the same box if their left and right ends agree
# within this. Absorbs scan skew and the way Tesseract clips a rule's
# extent at a hairline.
EDGE_TOL_PCT = 1.2

# A rule narrower than this is furniture inside an item -- a table rule, a
# coupon's dashes -- not a box side.
MIN_WIDTH_PCT = 3.0

# A box must be at least this tall. Below it, a "pair" is two rules of a
# single rule set (a double rule, or a rule and its scan echo).
MIN_HEIGHT_PCT = 1.0

# How far a side may stop SHORT of the corner and still count as bridging
# the box. Rounded corners mean the sides never actually meet: measured at
# ~0.5% on a plain box and 2.5-3.9% on an ornate one.
INSET_PCT = 2.5

# A vertical this close to a page edge is a scan artefact, not a box side.
EDGE_MARGIN_PCT = 2.0

# Slack when deciding whether one rule's run sits inside another's.
CONJOIN_TOL_PCT = 0.3

# Two collinear pieces are the same printed rule when they sit this close
# across the rule, and the gap along it is no wider than the second value.
# The gap allowance is generous because what interrupts a rule (a page
# number, scan damage) can be several percent wide.
FRAGMENT_POS_PCT = 0.7
FRAGMENT_GAP_PCT = 8.0

# Two verticals are "the same pair of sides" -- and so can have their feet
# joined to close an open box -- when both ends agree within this.
PAIR_MATCH_PCT = 1.5

# An open box is only closed if a barrier (another rule, or an already
# established box) sits below it within this distance. Without a barrier
# there is nothing saying where the box ends.
BARRIER_GAP_PCT = 6.0

# Boxes may nest or be disjoint, never straddle. Overlap beyond this
# fraction of the smaller box, without containment, is a crossing.
CROSS_TOL = 0.08

# Boxes agreeing within this on all four edges are the same box seen from
# two different vertical pairs.
DEDUPE_PCT = 1.5


def _drop_conjoined(rows: list[dict], orientation: str) -> list[dict]:
    """Remove separator regions that are several rules merged into one.

    Tesseract sometimes reports BOTH the individual rules AND a single
    region covering them. On 1980-04-06 p13 the left edge appears three
    times:

        V  x 4.29-4.69  y 25.82-47.79  (17px)   the real upper rule
        V  x 4.57-5.27  y 49.51-95.80  (29px)   the real lower rule
        V  x 3.76-4.96  y 25.82-95.88  (50px)   both, conjoined

    The merged region is thicker (roughly the sum) and spans the gap
    between the real rules, so it manufactures boxes across a boundary
    that is not there and hides the true ones.

    A region is conjoined when at least TWO others of the same
    orientation lie within its RUN and overlap it on the thickness axis.
    Containment of the full bbox is NOT the test -- the merged region is
    typically slightly WIDER than its own parts (3.76-4.96 against a part
    at 4.57-5.27), so a bbox test misses it.
    """
    keep = []
    for i, a in enumerate(rows):
        inner = 0
        for j, b in enumerate(rows):
            if i == j:
                continue
            if orientation == "vertical":
                within = (b["T"] >= a["T"] - CONJOIN_TOL_PCT
                          and b["B"] <= a["B"] + CONJOIN_TOL_PCT
                          and (b["B"] - b["T"]) < (a["B"] - a["T"]) * 0.9)
                overlaps = min(a["R"], b["R"]) - max(a["L"], b["L"]) > 0
            else:
                within = (b["L"] >= a["L"] - CONJOIN_TOL_PCT
                          and b["R"] <= a["R"] + CONJOIN_TOL_PCT
                          and (b["R"] - b["L"]) < (a["R"] - a["L"]) * 0.9)
                overlaps = min(a["B"], b["B"]) - max(a["T"], b["T"]) > 0
            if within and overlaps:
                inner += 1
        if inner < 2:
            keep.append(a)
    return keep


def _merge_fragments(rows: list[dict], orientation: str) -> list[dict]:
    """Join collinear pieces of one printed rule back together.

    The mirror of `_drop_conjoined`: Tesseract also SPLITS a single rule
    into segments, typically where something interrupts it. On
    1980-04-06 p13 the Sidewalk Sale box -- which occupies the whole
    lower half of the page -- has left, right and top rules but its foot
    arrives in pieces:

        H  x  4.43-75.05  y 95.12-96.02
        H  x 80.97-95.24  y 95.75-96.27

    Neither piece bridges both verticals, so the largest box on the page
    was missed entirely.

    Pieces are merged when they sit at the same position across the rule
    (within FRAGMENT_POS_PCT) and the gap along it is no wider than
    FRAGMENT_GAP_PCT. The merged rule spans the full extent and takes the
    heaviest thickness of its parts.
    """
    pos = (lambda r: (r["L"] + r["R"]) / 2) if orientation == "vertical" \
        else (lambda r: (r["T"] + r["B"]) / 2)
    lo = (lambda r: r["T"]) if orientation == "vertical" else (lambda r: r["L"])
    hi = (lambda r: r["B"]) if orientation == "vertical" else (lambda r: r["R"])

    out: list[dict] = []
    for r in sorted(rows, key=lambda r: (pos(r), lo(r))):
        merged = False
        for o in out:
            if abs(pos(o) - pos(r)) > FRAGMENT_POS_PCT:
                continue
            gap = max(lo(r) - hi(o), lo(o) - hi(r))
            if gap > FRAGMENT_GAP_PCT:
                continue
            o["L"], o["R"] = min(o["L"], r["L"]), max(o["R"], r["R"])
            o["T"], o["B"] = min(o["T"], r["T"]), max(o["B"], r["B"])
            for k in ("wd", "ht"):
                if r.get(k) and (o.get(k) or 0) < r[k]:
                    o[k] = r[k]
            merged = True
            break
        if not merged:
            out.append(dict(r))
    return out


def _rules(conn, page_id: str, orientation: str) -> list[dict]:
    return _merge_fragments(_drop_conjoined([dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
        "width_px wd, height_px ht "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_separator' "
        "AND orientation=?", (page_id, orientation))], orientation), orientation)


def _crosses(a: tuple, b: tuple) -> bool:
    """Do two boxes straddle each other's edges (rather than nest)?"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ow = min(ax1, bx1) - max(ax0, bx0)
    oh = min(ay1, by1) - max(ay0, by0)
    if ow <= 0 or oh <= 0:
        return False
    inter = ow * oh
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    if smaller <= 0:
        return False
    contained = ((ax0 >= bx0 - DEDUPE_PCT and ax1 <= bx1 + DEDUPE_PCT
                  and ay0 >= by0 - DEDUPE_PCT and ay1 <= by1 + DEDUPE_PCT)
                 or (bx0 >= ax0 - DEDUPE_PCT and bx1 <= ax1 + DEDUPE_PCT
                     and by0 >= ay0 - DEDUPE_PCT and by1 <= ay1 + DEDUPE_PCT))
    return not contained and inter / smaller > CROSS_TOL


def _close_open_boxes(H: list[dict], V: list[dict], done: list) -> list:
    """Close a box that has three printed sides but no printed foot.

    The PLEXIGLASS ad on 1980-04-06 p8 is the case: a top rule and two
    verticals whose ends match each other exactly, with the top of the
    next box immediately below. Nothing crosses that gap, so the feet of
    the two verticals can be joined and the box completed.

    Two conditions, both required:
      * the verticals are a genuine PAIR -- both ends agree within
        PAIR_MATCH_PCT, i.e. they were drawn as the sides of one box
      * a BARRIER sits below within BARRIER_GAP_PCT -- another rule, or a
        box already established. Without one there is nothing to say
        where the box ends, and the foot would be invention.

    The result is marked `n_sides = 3` and `needs_review = 1`: it is an
    inference, and it is labelled as one so an LLM pass can confirm it
    rather than inheriting a guess dressed as a measurement.
    """
    out = []
    for i, vl in enumerate(V):
        for vr in V[i + 1:]:
            if vr["x"] - vl["x"] < MIN_WIDTH_PCT:
                continue
            # Sides of the same box: both ends match.
            if (abs(vl["y0"] - vr["y0"]) > PAIR_MATCH_PCT
                    or abs(vl["y1"] - vr["y1"]) > PAIR_MATCH_PCT):
                continue
            top = max(vl["y0"], vr["y0"])
            foot = min(vl["y1"], vr["y1"])
            if foot - top < MIN_HEIGHT_PCT:
                continue
            # A printed head, bridging both verticals.
            heads = [h for h in H
                     if abs(h["y"] - top) <= INSET_PCT
                     and h["x0"] <= vl["x"] + INSET_PCT
                     and h["x1"] >= vr["x"] - INSET_PCT]
            if not heads:
                continue
            # If a printed foot already exists the main pass handled it.
            if any(abs(h["y"] - foot) <= INSET_PCT
                   and h["x0"] <= vl["x"] + INSET_PCT
                   and h["x1"] >= vr["x"] - INSET_PCT for h in H):
                continue
            # Something must stop it below.
            barrier = any(foot < h["y"] <= foot + BARRIER_GAP_PCT
                          and h["x1"] > vl["x"] and h["x0"] < vr["x"] for h in H)
            if not barrier:
                barrier = any(foot < d[1] <= foot + BARRIER_GAP_PCT
                              and d[2] > vl["x"] and d[0] < vr["x"] for d in done)
            if not barrier:
                continue
            out.append((vl["x"], heads[0]["y"], vr["x"], foot,
                        [vl["t"], vr["t"], heads[0]["t"], 0], 3,
                        abs(vl["x"] - heads[0]["x0"])
                        + abs(vr["x"] - heads[0]["x1"])))
    return out


def find_boxes(conn, page_id: str, cols: list[dict]) -> list[dict]:
    """Ruled rectangles from four sides. See the module docstring."""
    H = [{"y": (r["T"] + r["B"]) / 2, "x0": r["L"], "x1": r["R"],
          "t": r["ht"] or 0} for r in _rules(conn, page_id, "horizontal")]
    # Page-edge verticals are the sheet edge and binding shadow, not box
    # sides -- the same artefacts detect_grid filters out, and letting one
    # act as a side stretched boxes across the whole page width.
    # SORTED BY X, and that is load-bearing. The pair loop below takes
    # V[i], V[j] with j > i and requires vr.x - vl.x >= MIN_WIDTH_PCT. The
    # rows arrive from SQLite in no particular order, so whenever the
    # left-hand rule happened to be listed later the difference came out
    # negative and the pair was skipped in silence. That is why the
    # CENTENNIAL DOLLARS box on 1980-04-06 p8 was missed despite having
    # all four sides present -- its left rule (x 15.68) is listed after
    # its right one (x 49.13).
    V = sorted(
        ({"x": (r["L"] + r["R"]) / 2, "y0": r["T"], "y1": r["B"],
          "t": r["wd"] or 0} for r in _rules(conn, page_id, "vertical")
         if EDGE_MARGIN_PCT <= (r["L"] + r["R"]) / 2 <= 100 - EDGE_MARGIN_PCT),
        key=lambda v: v["x"])

    raw = []
    for i, vl in enumerate(V):
        for vr in V[i + 1:]:
            if vr["x"] - vl["x"] < MIN_WIDTH_PCT:
                continue
            y0 = max(vl["y0"], vr["y0"])
            y1 = min(vl["y1"], vr["y1"])
            if y1 - y0 < MIN_HEIGHT_PCT:
                continue
            # Horizontals that BRIDGE both verticals. The inset allowance
            # is what makes rounded corners work.
            span = sorted(
                (h for h in H
                 if y0 - INSET_PCT <= h["y"] <= y1 + INSET_PCT
                 and h["x0"] <= vl["x"] + INSET_PCT
                 and h["x1"] >= vr["x"] - INSET_PCT),
                key=lambda h: h["y"])
            if len(span) < 2:
                continue
            # The VERTICALS define the sides. Extending to the
            # horizontals' ends was wrong: a bridging rule often belongs
            # to a neighbouring box too and overshoots, which stretched
            # POLICE CONSTABLE and Congratulations a whole column left
            # into the body text on 1980-04-06 p8.
            L, R = vl["x"], vr["x"]
            # Each consecutive pair is a box (stacked boxes share
            # verticals) ...
            # How well these two verticals fit the rules they bound. A
            # gutter holds TWO verticals -- this box's own side and the
            # neighbouring box's -- and both bridge the same horizontals.
            # The right one is the tighter fit: a box's own side sits at
            # the end of its own rules. On 1980-04-06 p13 the JOHNSON
            # CROSS YANOSIK ad is bounded by x 39.22 and 61.11 against
            # horizontals spanning 38.98-61.22, while its neighbours' rules
            # at 37.97 and 62.11 bridge equally well and were being
            # preferred because they make a bigger box.
            # Slack is measured against the TWO rules that bound each box,
            # never across the whole bridging set: a page-wide rule in the
            # set otherwise swamps the comparison and both pairs score the
            # same.
            def _slack(a, b):
                # Score against whichever bounding rule fits BEST, not
                # against both together. One of the two is often a
                # page-wide rule shared with the neighbours, and combining
                # them lets it swamp the comparison so both candidate
                # pairs score the same. The rule specific to this box is
                # the one that identifies its true sides.
                return min(abs(vl["x"] - h["x0"]) + abs(vr["x"] - h["x1"])
                           for h in (a, b))

            for a, b in zip(span, span[1:]):
                if b["y"] - a["y"] >= MIN_HEIGHT_PCT:
                    raw.append((L, a["y"], R, b["y"],
                                [vl["t"], vr["t"], a["t"], b["t"]], 4,
                                _slack(a, b)))
            # ... and the whole enclosure is the container they sit in.
            if span[-1]["y"] - span[0]["y"] >= MIN_HEIGHT_PCT:
                raw.append((L, span[0]["y"], R, span[-1]["y"],
                            [vl["t"], vr["t"], span[0]["t"], span[-1]["t"]],
                            4, _slack(span[0], span[-1])))

    raw.extend(_close_open_boxes(H, V, raw))

    # Dedupe FIRST, keeping the tightest fit -- not the largest. Sorting by
    # area here is what let a neighbour's rule stand in for a box's own
    # side. Then the crossing filter runs largest-first, which it needs.
    tight = []
    for cand in sorted(raw, key=lambda b: b[6]):
        L, T, R, B = cand[0], cand[1], cand[2], cand[3]
        if any(abs(o[0] - L) < DEDUPE_PCT and abs(o[2] - R) < DEDUPE_PCT
               and abs(o[1] - T) < DEDUPE_PCT and abs(o[3] - B) < DEDUPE_PCT
               for o in tight):
            continue
        tight.append(cand)

    out = []
    for L, T, R, B, side_px, sides, _slack in sorted(
            tight, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1])):
        # A box may sit INSIDE another or beside it, never straddle its
        # edge. Fraser's price rows were being drawn from the column
        # gutter at x 49.13 while Fraser's own box starts at 61.89, so the
        # rows crossed both their container and the gutter. Larger boxes
        # are accepted first, so anything crossing one is the bad one.
        if any(_crosses((L, T, R, B),
                        (o["left_pct"], o["top_pct"],
                         o["right_pct"], o["bottom_pct"])) for o in out):
            continue
        span = _hl.column_span(L, R, cols) if cols else None
        out.append({
            "left_pct": round(L, 2), "right_pct": round(R, 2),
            "top_pct": round(T, 2), "bottom_pct": round(B, 2),
            "width_pct": round(R - L, 2), "height_pct": round(B - T, 2),
            "n_sides": sides,
            # 3 means the foot was inferred, not printed -- surfaced so a
            # later LLM pass can confirm or reject it rather than having
            # the guess silently presented as a measurement.
            "needs_review": 1 if sides < 4 else 0,
            "side_px": ",".join(str(x) for x in side_px),
            "col_lo": span[0] if span else None,
            "col_hi": span[1] if span else None,
        })
    return sorted(out, key=lambda b: (b["top_pct"], b["left_pct"]))


def detect(conn, page_id: str) -> dict:
    cols = _grid.detect(conn, page_id).get("columns") or []
    boxes = find_boxes(conn, page_id, cols)
    return {"boxes": boxes, "n_columns": len(cols)}


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_boxes WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for b in res["boxes"]:
        conn.execute(
            """INSERT INTO page_boxes
               (id, page_id, left_pct, top_pct, right_pct, bottom_pct,
                n_sides, col_lo, col_hi, side_px, needs_review, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, b["left_pct"], b["top_pct"],
             b["right_pct"], b["bottom_pct"], b["n_sides"],
             b["col_lo"], b["col_hi"], b.get("side_px"),
             b.get("needs_review", 0), now))
    conn.commit()


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
        print(f"  {len(res['boxes'])} boxed zone(s)\n")
        print(f"  {'x':>15}  {'y':>15}  {'w':>6} {'h':>6}  cols  sides")
        for b in res["boxes"]:
            cols = (f"{b['col_lo']}-{b['col_hi']}"
                    if b["col_lo"] is not None else "-")
            print(f"  {b['left_pct']:6.2f}-{b['right_pct']:6.2f}%  "
                  f"{b['top_pct']:6.2f}-{b['bottom_pct']:6.2f}%  "
                  f"{b['width_pct']:5.2f}% {b['height_pct']:5.2f}%  "
                  f"{cols:>5}  {b['n_sides']}")
    finally:
        conn.close()


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = _grid.pages_to_run(conn, args.date)
        n = tot = 0
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            n += 1
            tot += len(res["boxes"])
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(res['boxes'])} boxes")
        print(f"\n{n} page(s), {tot} boxes"
              + (f", {tot / n:.1f}/page" if n else ""))
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("date")
    s.add_argument("--page", type=int, required=True)
    s.set_defaults(func=_cmd_show)
    r = sub.add_parser("run")
    r.add_argument("--date")
    r.set_defaults(func=_cmd_run)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
