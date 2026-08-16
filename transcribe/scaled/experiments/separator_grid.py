"""EXPERIMENT — quantise ocr_separator presence onto a 1% grid.

Not part of the pipeline. A way of LOOKING at the separator data rather
than deriving from it.

The page is divided into squares 1% of PAGE WIDTH on a side, so the grid
is 100 cells wide and 100 x (height/width) cells tall -- the cells are
square in real page terms, not stretched to the page's aspect.

Each cell is shaded by HOW MANY separator regions touch it: one adds 25%
grey, two 50%, and so on to black. So the shade reads directly as "how
much ruling is happening here", and cells where several rules meet --
corners, junctions, stacked box edges -- stand out from cells crossed by
a single rule.

Uses the RAW `ocr_separator` regions, deliberately: this is a view of
what Tesseract actually emitted, before `detect_boxes` drops conjoined
regions or merges fragments. Pass --clean to see the cleaned set instead
and compare the two.

Usage::

    python3 -m transcribe.scaled.experiments.separator_grid 1980-04-06 --page 13
    python3 -m transcribe.scaled.experiments.separator_grid 1980-04-06 --page 13 --clean
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from .. import _support as _sup
from .. import detect_boxes as _boxes

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "grids")

CELL_PCT = 1.0        # cell side, as a percentage of PAGE WIDTH
CELL_PX = 14          # on-screen size of one cell
STEP_DARK = 0.25      # each separator in a cell adds this much darkness
GRID_LINE = (215, 219, 226)
LABEL = (90, 96, 108)


def build(conn, page_id: str, clean: bool = False):
    """Per-cell counts of separator regions, and the grid's shape."""
    row = conn.execute(
        "SELECT display_width_px w, display_height_px h FROM pages WHERE id=?",
        (page_id,)).fetchone()
    aspect = (row["h"] / row["w"]) if row and row["w"] else 1.4

    n_cols = int(round(100 / CELL_PCT))
    # Square cells: one cell is CELL_PCT of the WIDTH, so its height in
    # page-height percent is CELL_PCT / aspect.
    cell_h_pct = CELL_PCT / aspect
    n_rows = int(round(100 / cell_h_pct))

    counts = [[0] * n_cols for _ in range(n_rows)]

    if clean:
        regions = [dict(r) for o in ("horizontal", "vertical")
                   for r in _boxes._rules(conn, page_id, o)]
    else:
        regions = [dict(r) for r in conn.execute(
            "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
            "FROM page_hocr_regions WHERE page_id=? "
            "AND region_class='ocr_separator'", (page_id,))]

    for r in regions:
        c0 = max(0, min(n_cols - 1, int(r["L"] / CELL_PCT)))
        c1 = max(0, min(n_cols - 1, int(r["R"] / CELL_PCT)))
        r0 = max(0, min(n_rows - 1, int(r["T"] / cell_h_pct)))
        r1 = max(0, min(n_rows - 1, int(r["B"] / cell_h_pct)))
        for y in range(r0, r1 + 1):
            for x in range(c0, c1 + 1):
                counts[y][x] += 1
    return counts, n_cols, n_rows, len(regions)


def render(counts, n_cols, n_rows, title: str, out_path: str) -> str:
    pad_l, pad_t, pad_b = 34, 26, 8
    W = pad_l + n_cols * CELL_PX
    H = pad_t + n_rows * CELL_PX + pad_b
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    for y in range(n_rows):
        for x in range(n_cols):
            n = counts[y][x]
            x0, y0 = pad_l + x * CELL_PX, pad_t + y * CELL_PX
            if n:
                v = int(255 * max(0.0, 1.0 - STEP_DARK * n))
                d.rectangle([x0, y0, x0 + CELL_PX, y0 + CELL_PX],
                            fill=(v, v, v))
            d.rectangle([x0, y0, x0 + CELL_PX, y0 + CELL_PX],
                        outline=GRID_LINE)

    for x in range(0, n_cols + 1, 10):
        d.text((pad_l + x * CELL_PX - 6, 8), str(x), fill=LABEL)
    for y in range(0, n_rows + 1, 10):
        d.text((4, pad_t + y * CELL_PX - 4), str(y), fill=LABEL)

    d.text((pad_l, H - pad_b - 2), title, fill=(0, 0, 0))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def _cmd(args):
    conn = _sup.open_connection()
    try:
        y, m, dd = (int(v) for v in args.date.split("-"))
        row = conn.execute(
            "SELECT id FROM pages WHERE year=? AND month=? AND day=? AND page=?",
            (y, m, dd, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        counts, nc, nr, n = build(conn, row["id"], args.clean)
        busiest = max(max(r) for r in counts)
        cells = sum(1 for r in counts for v in r if v)
        kind = "cleaned" if args.clean else "raw"
        title = (f"{args.date} p{args.page} — {n} {kind} ocr_separator regions "
                 f"· {cells} cells touched · busiest cell {busiest} "
                 f"· grid {nc}x{nr} at {CELL_PCT}% of page width")
        suffix = "_clean" if args.clean else ""
        out = os.path.join(OUT_DIR, args.date,
                           f"p{args.page}_sepgrid{suffix}.png")
        print(" ", render(counts, nc, nr, title, out))
        print(f"  {n} regions, {cells} cells touched, busiest cell {busiest}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date")
    p.add_argument("--page", type=int, required=True)
    p.add_argument("--clean", action="store_true",
                   help="use detect_boxes' cleaned rules instead of raw")
    p.set_defaults(func=_cmd)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
