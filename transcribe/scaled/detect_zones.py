"""Stage 2b — BOXED ZONES, from the grid.

The pipeline, in the order it actually runs:

    Tesseract separators   RAW -- see the note below
      -> separator_grid     quantised onto SQUARE cells; corners resolved
      -> ad_rectangles      rectangles, from corners alone
      -> content            what each one contains, and what that says

The separators are used RAW. `rules.py` cleans them -- dropping conjoined
regions, rejoining fragments -- and that cleaning is NOT applied here, on
measurement: across 90 pages it gives 251 zones against 266, worse on 14
pages and better on 11, and on p13 it loses the Sidewalk Sale. It was
built for the rule-pairing detector, where a fragment broke the pair; the
corner derivation wants rule ENDS, and merging fragments removes them.
An earlier version of this docstring claimed the cleaning ran. It did not.

This replaces the rule-PAIRING detector (archived as
`archive/detect_boxes_pairing.py`). That one asked "is this a valid
rectangle?", to which the union of two stacked ads answers yes -- it has
four ruled edges, because the side rules run continuously past both. Six
tuned thresholds existed to clean up after that. The corner predicate --
a rectangle is an item when no other corner interrupts its sides --
rejects unions, bridges and gutter slivers by construction and needs none
of them.

WHAT WAS CARRIED FORWARD, and what was left behind
---------------------------------------------------
Kept, because it is about the RULES rather than the boxes:
  * conjoined-region removal and fragment rejoining (`rules.py`)
  * the page-edge margin that excludes scan artefacts
  * the content-area filter, so shadows outside the type are ignored

Left behind with the pairing detector:
  * the six geometric thresholds (aspect ratio, thin dimension, gap
    tolerance, twin collapse, double-rule merge, gutter drop)
  * `n_sides` and the three-sided closure, which inferred a foot where
    none was printed. The corner map already carries that evidence, and
    an inferred edge is not needed to make an atomic rectangle.

Newly added here, and NOT previously wired in anywhere: the CONTENT
check. Geometry decides what the rectangles are; content says what they
contain and flags the ones worth a second look. Nothing is dropped on a
content test alone -- 28.8% of boxes in this corpus hold no text block at
all, and many of those are pictorial ads, so an emptiness rule that only
counted text would delete them.

Flags are advisory:
    empty        no block and no photo
    pictorial    no block but a photo -- an image ad
    duplicate    identical block set to another zone
    encloses     its blocks are exactly the union of the zones inside it

Usage::

    python3 -m transcribe.scaled.detect_zones show 1980-04-06 --page 13
    python3 -m transcribe.scaled.detect_zones run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse

from . import _support as _sup
from . import detect_grid as _grid
from . import detect_hlines as _hl
from .experiments import ad_rectangles as _ads
from .experiments import separator_grid as _sepgrid


def _content(conn, page_id: str, zones: list[dict],
             cw: float, chh: float) -> None:
    """Fill in what each zone contains, and flag it. Drops nothing."""
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

    for z in zones:
        def inside(it):
            return (z["left_pct"] <= (it["L"] + it["R"]) / 2 <= z["right_pct"]
                    and z["top_pct"] <= (it["T"] + it["B"]) / 2 <= z["bottom_pct"])
        z["blocks"] = sorted(b["block_idx"] for b in blocks if inside(b))
        z["n_lines"] = sum(1 for l in lines if inside(l))
        z["n_photos"] = sum(1 for p in photos if inside(p))

    for i, a in enumerate(zones):
        flags = []
        if not a["blocks"]:
            flags.append("pictorial" if a["n_photos"] else "empty")
        if a["blocks"] and any(a["blocks"] == b["blocks"]
                               for j, b in enumerate(zones) if i != j):
            flags.append("duplicate")
        # One CELL of slack, which is one physical distance on both axes.
        # This used to be a flat 0.5 page-percent either way -- 1.00 cell
        # across but 1.41 down, so a zone had to be tucked half again as
        # far inside vertically to count as contained. See §5z.7.
        inner = [b for j, b in enumerate(zones) if i != j
                 and b["left_pct"] >= a["left_pct"] - cw
                 and b["right_pct"] <= a["right_pct"] + cw
                 and b["top_pct"] >= a["top_pct"] - chh
                 and b["bottom_pct"] <= a["bottom_pct"] + chh]
        if inner and a["blocks"]:
            covered = set()
            for b in inner:
                covered |= set(b["blocks"])
            if covered == set(a["blocks"]):
                flags.append("encloses")
        a["flags"] = ",".join(sorted(set(flags)))


def detect(conn, page_id: str) -> dict:
    """Boxed zones for one page, with their content."""
    g = _sepgrid.build(conn, page_id)
    junction, crossing = g[1], g[3]
    n_cols, n_rows = g[6], g[7]

    cw, chh = _sepgrid.cell_size(conn, page_id)

    # Corners, then rectangles -- both entirely in CELLS. Percent appears
    # only on the way out, because that is what the rest of the schema
    # speaks.
    pts = [(p[1], p[0]) for p in
           _sepgrid.corner_points(junction, crossing, n_cols, n_rows)]
    gut, edges = _sepgrid._gutter_centres(conn, page_id, cw, chh)
    rects = _ads.ad_rectangles(pts, gut + edges,
                               _sepgrid._photo_units(conn, page_id, cw, chh))

    cols = _grid.detect(conn, page_id).get("columns") or []
    zones = []
    for i, r in enumerate(rects):
        L, T = r["L"] * cw, r["T"] * chh
        R, B = r["R"] * cw, r["B"] * chh
        span = _hl.column_span(L, R, cols) if cols else None
        zones.append({
            "idx": i,
            "left_pct": round(L, 2), "top_pct": round(T, 2),
            "right_pct": round(R, 2), "bottom_pct": round(B, 2),
            "width_pct": round(R - L, 2), "height_pct": round(B - T, 2),
            "score": r["score"], "reasons": "; ".join(r["reasons"]),
            "col_lo": span[0] if span else None,
            "col_hi": span[1] if span else None,
        })
    _content(conn, page_id, zones, cw, chh)
    return {"zones": zones, "n_corners": len(pts)}


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_zones WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for z in res["zones"]:
        conn.execute(
            """INSERT INTO page_zones
               (id, page_id, idx, left_pct, top_pct, right_pct, bottom_pct,
                col_lo, col_hi, score, reasons, n_blocks, n_lines, n_photos,
                block_ids, flags, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, z["idx"], z["left_pct"], z["top_pct"],
             z["right_pct"], z["bottom_pct"], z["col_lo"], z["col_hi"],
             z["score"], z["reasons"], len(z["blocks"]), z["n_lines"],
             z["n_photos"], ",".join(str(b) for b in z["blocks"]),
             z["flags"], now))
    conn.commit()


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? "
                           "AND day=? AND page=?",
                           (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        res = detect(conn, row["id"])
        print(f"  {res['n_corners']} corners -> {len(res['zones'])} zones\n")
        print(f"  {'x':>15} {'y':>15} {'cols':>6}{'blk':>5}{'ln':>5}{'ph':>4}"
              f"{'sc':>4}  flags")
        for z in res["zones"]:
            cols = (f"{z['col_lo']}-{z['col_hi']}"
                    if z["col_lo"] is not None else "-")
            print(f"  {z['left_pct']:6.2f}-{z['right_pct']:6.2f}% "
                  f"{z['top_pct']:6.2f}-{z['bottom_pct']:6.2f}% {cols:>6}"
                  f"{len(z['blocks']):5d}{z['n_lines']:5d}{z['n_photos']:4d}"
                  f"{z['score']:4.0f}  {z['flags']}")
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
            tot += len(res["zones"])
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(res['zones'])} zones")
        print(f"\n{n} page(s), {tot} zones"
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
