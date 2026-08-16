"""EXPERIMENT — boxes from FACES, with corner precision and content check.

A third derivation, alongside `detect_boxes` (rule pairing) and
`separator_grid`'s corner-quadruple search. Nothing existing is disturbed;
all three can be compared on the same page.

THE THREE SIGNALS, EACH DOING WHAT IT IS GOOD AT
-------------------------------------------------
1. **Topology — face extraction.** Label the background of the ruling
   grid and discard components touching the border; each survivor is one
   enclosed region. Enumerating corner quadruples asks "is this a valid
   rectangle?", and the union of two stacked ads genuinely is one -- which
   is why that route needed a twin-collapse, a gutter-drop and a
   double-rule merge to clean up after itself. Face extraction asks "what
   does the ruling enclose?", and a union is not a face because the
   divider splits it. Measured on 1980-04-06 p13: 8 faces, neither the
   JOHNSON/INSULATE bridge nor the ELECTRICAL/NOTICE union present, with
   no filtering at all.

2. **Precision — the corner map.** A face's bounding box is the HOLE, so
   it sits inside the ruling by about a cell: JOHNSON reads as
   x 40.0-60.0 where the corners say 39.0-61.0. Each face bound is
   therefore snapped to the nearest resolved corner.

3. **Keep or drop — contained content.** Geometry finds the regions;
   content decides which are items. This is where the geometric thresholds
   went: no aspect ratio, no thin-dimension bar, no gap tolerance.

       empty       no block AND no photo          -> DROP
       duplicate   identical block set to another -> DROP one (double rule)
       enclosure   its blocks are exactly the union of the regions inside
                   it, so it contributes nothing  -> DROP
       pictorial   no block but a photo           -> KEEP

   The pictorial case is why the test is not simply "has text": 28.8% of
   boxes in this corpus contain no text block, and 40 of those contain a
   photo. Content answers that correctly by looking at photos as well as
   blocks -- an emptiness test that only counted text would delete every
   pictorial ad.

Usage::

    python3 -m transcribe.scaled.experiments.faces 1980-04-06 --page 13
"""

from __future__ import annotations

import argparse

import numpy as np
from scipy import ndimage

from .. import _support as _sup
from . import separator_grid as _grid

# Closes the gap at a rounded corner so a face does not leak out into the
# page background. One cell each way. NOTE: corner insets were measured at
# 0.5% on a plain box but up to 3.9% on an ornate one (~8 cells), so a
# wide-radius border may still leak -- `--dilate` exists to test that.
DILATE_CELLS = 1
MIN_FACE_CELLS = 20          # below this a "region" is a gap, not an area
SNAP_CELLS = 3.0             # how far a face bound may reach for a corner


def faces(counts, n_cols, n_rows, dilate=DILATE_CELLS):
    """Bounding boxes of the regions the ruling encloses, in cell coords."""
    ink = np.array([[1 if counts[y][x] else 0 for x in range(n_cols)]
                    for y in range(n_rows)], bool)
    k = 2 * dilate + 1
    ink = ndimage.binary_dilation(ink, np.ones((k, k), bool))
    lab, n = ndimage.label(~ink)
    border = (set(lab[0, :]) | set(lab[-1, :])
              | set(lab[:, 0]) | set(lab[:, -1]))
    out = []
    for i in range(1, n + 1):
        if i in border:
            continue
        ys, xs = np.where(lab == i)
        if len(ys) < MIN_FACE_CELLS:
            continue
        out.append([int(ys.min()), int(xs.min()), int(ys.max()), int(xs.max())])
    return out


def snap_to_corners(box, points):
    """Pull a face's bounds out to the corners that actually bound it.

    A face is the hole, so its bbox is inset by the ruling. The corner map
    carries the true edge positions, so each side reaches for the nearest
    corner within SNAP_CELLS and takes it.
    """
    y0, x0, y1, x1 = box
    ys = [p[0] for p in points]
    xs = [p[1] for p in points]

    def nearest(v, cands, lo, hi):
        c = [q for q in cands if lo <= q <= hi and abs(q - v) <= SNAP_CELLS]
        return min(c, key=lambda q: abs(q - v)) if c else v

    return [nearest(y0, ys, y0 - SNAP_CELLS, y0 + SNAP_CELLS),
            nearest(x0, xs, x0 - SNAP_CELLS, x0 + SNAP_CELLS),
            nearest(y1, ys, y1 - SNAP_CELLS, y1 + SNAP_CELLS),
            nearest(x1, xs, x1 - SNAP_CELLS, x1 + SNAP_CELLS)]


def content_of(conn, page_id, box_pct):
    """What a region contains. Membership is by an item's CENTRE."""
    L, T, R, B = box_pct

    def inside(it):
        return (L <= (it["L"] + it["R"]) / 2 <= R
                and T <= (it["T"] + it["B"]) / 2 <= B)

    blocks = [dict(x) for x in conn.execute(
        "SELECT block_idx, bbox_left_pct L, bbox_top_pct T, bbox_right_pct R, "
        "bbox_bottom_pct B FROM page_ocr_blocks WHERE page_id=?", (page_id,))]
    lines = [dict(x) for x in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_lines WHERE page_id=? AND n_words > 0", (page_id,))]
    photos = [dict(x) for x in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_photo'",
        (page_id,))]
    return {
        "blocks": sorted(b["block_idx"] for b in blocks if inside(b)),
        "n_lines": sum(1 for l in lines if inside(l)),
        "n_photos": sum(1 for p in photos if inside(p)),
    }


def confirm(regions):
    """Flag each region from its content, and decide keep or drop."""
    for i, a in enumerate(regions):
        flags = []
        if not a["blocks"]:
            flags.append("pictorial" if a["n_photos"] else "empty")
        for j, b in enumerate(regions):
            if i != j and a["blocks"] and a["blocks"] == b["blocks"]:
                flags.append("duplicate")
                break
        # Enclosure: the regions strictly inside it account for every one
        # of its blocks, so it contributes nothing of its own.
        inner = [b for j, b in enumerate(regions) if i != j
                 and b["L"] >= a["L"] - 0.5 and b["R"] <= a["R"] + 0.5
                 and b["T"] >= a["T"] - 0.5 and b["B"] <= a["B"] + 0.5]
        if inner and a["blocks"]:
            covered = set()
            for b in inner:
                covered |= set(b["blocks"])
            if covered == set(a["blocks"]):
                flags.append("enclosure")
        a["flags"] = sorted(set(flags))
        a["keep"] = not ({"empty", "duplicate", "enclosure"} & set(flags))
    # A duplicate pair must not lose BOTH members: reinstate the larger,
    # which for a double-ruled border is the outer rule and so the item's
    # true extent.
    for a in regions:
        if "duplicate" not in a["flags"] or a["keep"]:
            continue
        twins = [b for b in regions
                 if b["blocks"] == a["blocks"] and "duplicate" in b["flags"]]
        if not any(b["keep"] for b in twins):
            biggest = max(twins, key=lambda b: (b["R"] - b["L"]) * (b["B"] - b["T"]))
            biggest["keep"] = True
    return regions


def detect(conn, page_id, dilate=DILATE_CELLS):
    g = _grid.build(conn, page_id)
    counts, junction, near, crossing = g[0], g[1], g[2], g[3]
    n_cols, n_rows = g[6], g[7]
    row = conn.execute("SELECT display_width_px w, display_height_px h "
                       "FROM pages WHERE id=?", (page_id,)).fetchone()
    cw = _grid.CELL_PCT
    chh = cw / (row["h"] / row["w"])

    pts = _grid.corner_points(junction, crossing, n_cols, n_rows)
    out = []
    for f in faces(counts, n_cols, n_rows, dilate):
        y0, x0, y1, x1 = snap_to_corners(f, pts)
        box = (x0 * cw, y0 * chh, x1 * cw, y1 * chh)
        rec = {"L": box[0], "T": box[1], "R": box[2], "B": box[3],
               "cells": (y0, x0, y1, x1)}
        rec.update(content_of(conn, page_id, box))
        out.append(rec)
    return confirm(sorted(out, key=lambda r: (r["T"], r["L"])))


def _cmd(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(v) for v in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? "
                           "AND day=? AND page=?", (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        regs = detect(conn, row["id"], args.dilate)
        print(f"  {len(regs)} regions\n")
        print(f"  {'x':>15} {'y':>15} {'lines':>6}{'blk':>5}{'pho':>5}  flags")
        for r in regs:
            print(f"  {r['L']:6.2f}-{r['R']:6.2f}% {r['T']:6.2f}-{r['B']:6.2f}% "
                  f"{r['n_lines']:6d}{len(r['blocks']):5d}{r['n_photos']:5d}  "
                  f"{','.join(r['flags'])}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date")
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--dilate", type=int, default=DILATE_CELLS)
    p.set_defaults(func=_cmd)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
