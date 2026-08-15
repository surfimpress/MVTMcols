"""Stage 2b — BOXED ZONES: ruled rectangles on the page.

A boxed-off area is a deliberate landmark. Most are display ads, but
notices, standing panels, indexes and feature boxes use the same device.
Knowing where they are matters for its own sake, and because a box's
interior is set to its OWN grid, not the page's (see
`instructions/scaled_pipeline.md` §5f on display-ad grid contamination).

THE SIGNATURE, AND WHY IT IS NOT CORNERS
-----------------------------------------
The obvious method is to look for `ocr_separator` rules meeting at their
corners. **Measured across the corpus, that does not work:** of 4,452
horizontal-rule endpoints, only **22%** sit within 0.5% of a vertical
rule's end (26% within 1%, 33% within 3%; median distance **9.0%**).
Tesseract simply does not report all four sides of a box reliably --
often the verticals are missing, merged into adjacent text, or reported
as one long rule spanning several stacked boxes.

What does work is a **top and bottom rule sharing the same x-extent**.
That pair is the box's real signature, and it survives the verticals
being absent. Measured: 921 such pairs, 10.2 per page, median box
13.9% wide x 6.3% tall.

Vertical sides are then recorded as CORROBORATION, not required: 59% of
pairs have at least one matching vertical. `n_sides` carries this, so a
consumer can demand 4-sided boxes if it wants them without this stage
having thrown the 2-sided ones away.

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

# A vertical rule counts as a side if it runs the box's height (allowing
# this much shortfall at each end) and sits near a left/right edge.
SIDE_SLACK_PCT = 2.0


def _extent_match(a: dict, b: dict) -> tuple[float, float] | None:
    """Do two horizontal rules bound the same box? If so, its x-extent.

    STRICT: both ends must agree. Nothing clever.

    A containment variant was tried (allowing a wide rule to pair with a
    narrow one, on the theory that Tesseract merges collinear rules from
    adjacent boxes) and it was a clear failure -- boxes went from 6.8 to
    20.8 per page and the render showed overlapping rectangles cutting
    across body text on 1980-04-06 p6. It let almost any rule pair with
    almost any other. REVERTED; do not reintroduce it.

    The lesson, which the page render made obvious: Tesseract's separator
    rules ALREADY trace these boxes. On p6 the rules alone outline the
    Pakenham Seniors panel, the Beach Party ad, the Sidewalk Sale, HI
    MOM/RELAX and the I.D.A. ad correctly. The job is to read them, not
    to infer boxes they do not support.
    """
    if (abs(a["L"] - b["L"]) <= EDGE_TOL_PCT
            and abs(a["R"] - b["R"]) <= EDGE_TOL_PCT):
        return min(a["L"], b["L"]), max(a["R"], b["R"])
    return None


def _rules(conn, page_id: str, orientation: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_separator' "
        "AND orientation=?", (page_id, orientation))]


def find_boxes(conn, page_id: str, cols: list[dict]) -> list[dict]:
    """Ruled rectangles, from top/bottom rule pairs sharing an x-extent."""
    H = sorted(({"L": r["L"], "R": r["R"], "y": (r["T"] + r["B"]) / 2}
                for r in _rules(conn, page_id, "horizontal")),
               key=lambda d: d["y"])
    V = [{"x": (r["L"] + r["R"]) / 2, "T": r["T"], "B": r["B"]}
         for r in _rules(conn, page_id, "vertical")]

    boxes = []
    for i, a in enumerate(H):
        if a["R"] - a["L"] < MIN_WIDTH_PCT:
            continue
        # Nearest rule BELOW that shares the extent. Nearest, not widest:
        # stacked boxes share a left/right edge, and taking the furthest
        # match would swallow every box in the stack into one.
        for j in range(i + 1, len(H)):
            b = H[j]
            if b["y"] - a["y"] < MIN_HEIGHT_PCT:
                continue
            m = _extent_match(a, b)
            if m:
                left, right = m
                sides = 2
                for v in V:
                    if v["T"] > a["y"] + SIDE_SLACK_PCT:
                        continue
                    if v["B"] < b["y"] - SIDE_SLACK_PCT:
                        continue
                    if (abs(v["x"] - left) <= EDGE_TOL_PCT
                            or abs(v["x"] - right) <= EDGE_TOL_PCT):
                        sides += 1
                if sides < 3:
                    # No vertical side found between these two rules, so
                    # there is no evidence of a box -- just two rules that
                    # happen to share an x-extent (a story's top and
                    # bottom cutoff rules, for instance).
                    break
                span = _hl.column_span(left, right, cols) if cols else None
                boxes.append({
                    "left_pct": round(left, 2), "right_pct": round(right, 2),
                    "top_pct": round(a["y"], 2), "bottom_pct": round(b["y"], 2),
                    "width_pct": round(right - left, 2),
                    "height_pct": round(b["y"] - a["y"], 2),
                    "n_sides": min(4, sides),
                    "col_lo": span[0] if span else None,
                    "col_hi": span[1] if span else None,
                })
                break
    return boxes


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
                n_sides, col_lo, col_hi, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, b["left_pct"], b["top_pct"],
             b["right_pct"], b["bottom_pct"], b["n_sides"],
             b["col_lo"], b["col_hi"], now))
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
