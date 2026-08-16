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

CELL_PCT = 0.5        # cell side, as a percentage of PAGE WIDTH
CELL_PX = 8           # on-screen size of one cell
STEP_DARK = 0.25      # each separator in a cell adds this much darkness
GRID_LINE = (215, 219, 226)
LABEL = (90, 96, 108)
JUNCTION = (200, 30, 40)   # a separator END meeting another separator
GUTTER = (250, 214, 60)    # column gutter CENTRE line, blended in
GUTTER_MIX = 0.30          # how strongly the gutter tint shows through

# Padding around the content area. Rules that RUN ALONG the content edge
# (a box's outer border, the rule under a masthead) must survive, so the
# box is grown before filtering; only separators entirely outside it are
# dropped. Those are the digitisation shadows -- the sheet edge and the
# binding gutter.
CONTENT_PAD_PCT = 2.0


def _within_content(conn, page_id: str, regions: list[dict]) -> list[dict]:
    """Drop separators lying entirely outside the padded content area.

    The content rectangle comes from stage 1c, which is the same
    measurement the column fit is anchored to. Padding it by
    CONTENT_PAD_PCT keeps rules that run ALONG the content edge -- a box's
    outer border, the rule under a masthead -- and removes only what sits
    beyond the type altogether: the sheet edge and the binding shadow that
    digitisation leaves down the margins.
    """
    r = conn.execute(
        "SELECT content_left_pct l, content_right_pct r, content_top_pct t, "
        "content_bottom_pct b FROM pages WHERE id=?", (page_id,)).fetchone()
    if not r or r["l"] is None:
        return regions
    L = r["l"] - CONTENT_PAD_PCT
    R = r["r"] + CONTENT_PAD_PCT
    T = r["t"] - CONTENT_PAD_PCT
    B = r["b"] + CONTENT_PAD_PCT
    return [x for x in regions
            if x["R"] >= L and x["L"] <= R and x["B"] >= T and x["T"] <= B]


def _fold_contained(regions: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fold a separator wholly inside another into its container.

    These are mostly the same printed rule reported twice, and left alone
    they double the shading of every cell they cross -- pure noise in a
    density view.

    But they are NOT worthless, and discarding them outright would throw
    away the useful part: the contained rule's ENDS are real evidence.
    A long column rule often runs past several stacked boxes, and the
    short rule inside it marks where one of those boxes actually stops.
    So the geometry of the container is kept, and the contained rule is
    returned separately so its endpoints still count as junctions.
    """
    keep, folded = [], []
    for i, a in enumerate(regions):
        inside = False
        for j, b in enumerate(regions):
            if i == j:
                continue
            if (a["L"] >= b["L"] - 0.05 and a["R"] <= b["R"] + 0.05
                    and a["T"] >= b["T"] - 0.05 and a["B"] <= b["B"] + 0.05):
                # Identical pair: keep exactly one of them.
                same = (abs(a["L"] - b["L"]) < 0.05 and abs(a["R"] - b["R"]) < 0.05
                        and abs(a["T"] - b["T"]) < 0.05 and abs(a["B"] - b["B"]) < 0.05)
                if same and j > i:
                    continue
                inside = True
                break
        (folded if inside else keep).append(a)
    return keep, folded


def _gutter_centres(conn, page_id: str) -> list[float]:
    """x of the CENTRE of each gutter between adjacent columns.

    The centre line, not the column edges: the question this view is meant
    to answer is whether the rules separate cleanly around the gutter, and
    a pair of edge lines would pre-empt that by drawing where the answer is
    supposed to be.
    """
    cols = [dict(r) for r in conn.execute(
        "SELECT left_pct, right_pct FROM page_columns WHERE page_id=? "
        "AND method='grid' ORDER BY col_idx", (page_id,))]
    return [(cols[i]["right_pct"] + cols[i + 1]["left_pct"]) / 2
            for i in range(len(cols) - 1)]


def _ends(r: dict) -> list[tuple]:
    """The two end points of a separator."""
    if (r["R"] - r["L"]) >= (r["B"] - r["T"]):      # horizontal
        y = (r["T"] + r["B"]) / 2
        return [(r["L"], y), (r["R"], y)]
    x = (r["L"] + r["R"]) / 2
    return [(x, r["T"]), (x, r["B"])]


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
            "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
            "orientation FROM page_hocr_regions WHERE page_id=? "
            "AND region_class='ocr_separator'", (page_id,))]

    regions = _within_content(conn, page_id, regions)
    regions, swallowed = _fold_contained(regions)

    def cells_of(r):
        c0 = max(0, min(n_cols - 1, int(r["L"] / CELL_PCT)))
        c1 = max(0, min(n_cols - 1, int(r["R"] / CELL_PCT)))
        r0 = max(0, min(n_rows - 1, int(r["T"] / cell_h_pct)))
        r1 = max(0, min(n_rows - 1, int(r["B"] / cell_h_pct)))
        return [(y, x) for y in range(r0, r1 + 1) for x in range(c0, c1 + 1)]

    occupied = {}                       # cell -> set of region indices
    for i, r in enumerate(regions):
        for cell in cells_of(r):
            counts[cell[0]][cell[1]] += 1
            occupied.setdefault(cell, set()).add(i)

    # Folded-away duplicates still occupy cells for junction purposes --
    # their ends are real -- but do not darken them.
    for k, r in enumerate(swallowed, start=len(regions)):
        for cell in cells_of(r):
            occupied.setdefault(cell, set()).add(k)

    # A junction is a cell holding the END of one separator and ANY part
    # of a different one. That is what a corner looks like: a rule stops
    # where another passes through or stops too.
    junction = [[False] * n_cols for _ in range(n_rows)]
    for i, r in enumerate(list(regions) + list(swallowed)):
        for (ex, ey) in _ends(r):
            cx = max(0, min(n_cols - 1, int(ex / CELL_PCT)))
            cy = max(0, min(n_rows - 1, int(ey / cell_h_pct)))
            others = occupied.get((cy, cx), set()) - {i}
            if others:
                junction[cy][cx] = True
    gutter = [False] * n_cols
    for gx in _gutter_centres(conn, page_id):
        cx = int(gx / CELL_PCT)
        if 0 <= cx < n_cols:
            gutter[cx] = True

    return (counts, junction, gutter, n_cols, n_rows,
            len(regions), len(swallowed))


def _blend(base: tuple, tint: tuple, amount: float) -> tuple:
    return tuple(int(b * (1 - amount) + t * amount) for b, t in zip(base, tint))


def render(counts, junction, gutter, n_cols, n_rows,
           title: str, out_path: str) -> str:
    pad_l, pad_t, pad_b = 34, 26, 8
    W = pad_l + n_cols * CELL_PX
    H = pad_t + n_rows * CELL_PX + pad_b
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    for y in range(n_rows):
        for x in range(n_cols):
            n = counts[y][x]
            x0, y0 = pad_l + x * CELL_PX, pad_t + y * CELL_PX
            if junction[y][x]:
                fill = JUNCTION
            elif n:
                v = int(255 * max(0.0, 1.0 - STEP_DARK * n))
                fill = (v, v, v)
            else:
                fill = None
            # The gutter tint is BLENDED rather than painted under, so a
            # rule sitting on the gutter centre stays visible. Seeing that
            # is the whole point of the overlay.
            if gutter[x]:
                fill = _blend(fill or (255, 255, 255), GUTTER, GUTTER_MIX)
            if fill:
                d.rectangle([x0, y0, x0 + CELL_PX, y0 + CELL_PX], fill=fill)
            d.rectangle([x0, y0, x0 + CELL_PX, y0 + CELL_PX],
                        outline=GRID_LINE)

    step = 20 if n_cols > 120 else 10
    for x in range(0, n_cols + 1, step):
        d.text((pad_l + x * CELL_PX - 6, 8), str(x), fill=LABEL)
    for y in range(0, n_rows + 1, step):
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
        counts, junc, gut, nc, nr, n, folded = build(conn, row["id"], args.clean)
        busiest = max(max(r) for r in counts)
        cells = sum(1 for r in counts for v in r if v)
        njunc = sum(1 for r in junc for v in r if v)
        kind = "cleaned" if args.clean else "raw"
        title = (f"{args.date} p{args.page} — {n} {kind} separators "
                 f"(+{folded} folded) · {cells} cells · busiest {busiest} "
                 f"· {njunc} junction (red) · {sum(gut)} gutter columns "
                 f"(yellow) · grid {nc}x{nr} at {CELL_PCT}% of width")
        suffix = "_clean" if args.clean else ""
        out = os.path.join(OUT_DIR, args.date,
                           f"p{args.page}_sepgrid{suffix}.png")
        print(" ", render(counts, junc, gut, nc, nr, title, out))
        print(f"  {n} regions kept, {folded} folded as contained, "
              f"{cells} cells touched, busiest {busiest}, {njunc} junctions")
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
