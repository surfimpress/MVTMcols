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
from .. import detect_captions as _captions

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "grids")

CELL_PCT = 0.5        # cell side, as a percentage of PAGE WIDTH
CELL_PX = 8           # on-screen size of one cell
STEP_DARK = 0.25      # each separator in a cell adds this much darkness
GRID_LINE = (215, 219, 226)
LABEL = (90, 96, 108)
JUNCTION = (200, 30, 40)   # a separator END sharing a cell with another
NEAR = (255, 45, 200)      # a separator END one cell away from another
GUTTER = (250, 214, 60)    # column gutter CENTRE line, blended in
GUTTER_MIX = 0.30          # how strongly the gutter tint shows through
PHOTO = (120, 180, 235)    # photo + caption PERIMETER, blended in
PHOTO_MIX = 0.45

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


def _gutter_centres(conn, page_id: str) -> tuple[list[float], list[float]]:
    """Vertical reference lines: gutter centres, and the content edges.

    Gutter CENTRES, not the column edges: the question this view exists to
    answer is whether the rules separate cleanly around a gutter, and a
    pair of edge lines would pre-empt that by drawing where the answer is
    supposed to be.

    The content area's own left and right edges are returned alongside.
    They are not gutters -- they are where the type starts and stops -- but
    they are the other place a vertical rule has a principled reason to
    sit, and the outer verticals on 1980-04-06 p13 (x 4.78, 94.42, 95.92)
    sit 10-12% from any gutter precisely because they belong to these
    instead.
    """
    cols = [dict(r) for r in conn.execute(
        "SELECT left_pct, right_pct FROM page_columns WHERE page_id=? "
        "AND method='grid' ORDER BY col_idx", (page_id,))]
    gutters = [(cols[i]["right_pct"] + cols[i + 1]["left_pct"]) / 2
               for i in range(len(cols) - 1)]
    r = conn.execute(
        "SELECT content_left_pct l, content_right_pct r FROM pages WHERE id=?",
        (page_id,)).fetchone()
    edges = [r["l"], r["r"]] if r and r["l"] is not None else []
    return gutters, edges


def _photo_units(conn, page_id: str) -> list[tuple]:
    """Encompassing rectangle per photo -- with its caption where found.

    The same unit stage 2c stores and the viewer draws, so the grid and
    the IIIF layer are describing the same thing.
    """
    out = []
    for pr in _captions.detect(conn, page_id)["pairs"]:
        p_, c = pr["photo"], pr["caption"]
        L, T, R, B = p_["L"], p_["T"], p_["R"], p_["B"]
        if c:
            L, T = min(L, c["left_pct"]), min(T, c["top_pct"])
            R, B = max(R, c["right_pct"]), max(B, c["bottom_pct"])
        out.append((L, T, R, B))
    return out


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
        """Cells a rule occupies: full extent along it, ONE cell across it.

        Across its thin axis a rule is placed by its CENTRE LINE, not by
        its bbox footprint. Measured on 1980-04-06 p3, the box at x 50-61
        is bounded by single rules 0.39% and 0.46% thick -- THINNER than
        the 0.5% cell -- but each straddles a cell boundary and so lit two
        columns of cells. The shading then read as two separators where
        there is one, which is exactly the wrong thing for a view whose
        legend says each region adds 25% grey.

        Thickness is not lost: it is a stored property of the region
        (`width_px`/`height_px`) and belongs in a measurement, not in a
        count of how many rules are present.
        """
        horizontal = (r["R"] - r["L"]) >= (r["B"] - r["T"])
        if horizontal:
            c0 = max(0, min(n_cols - 1, int(r["L"] / CELL_PCT)))
            c1 = max(0, min(n_cols - 1, int(r["R"] / CELL_PCT)))
            cy = max(0, min(n_rows - 1,
                            int(((r["T"] + r["B"]) / 2) / cell_h_pct)))
            return [(cy, x) for x in range(c0, c1 + 1)]
        r0 = max(0, min(n_rows - 1, int(r["T"] / cell_h_pct)))
        r1 = max(0, min(n_rows - 1, int(r["B"] / cell_h_pct)))
        cx = max(0, min(n_cols - 1, int(((r["L"] + r["R"]) / 2) / CELL_PCT)))
        return [(y, cx) for y in range(r0, r1 + 1)]

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

    # Two strengths of the same idea -- a rule ending where another rule
    # is. Rounded corners and scan skew mean the two often MISS each other
    # by a cell, so proximity has to count as well as coincidence.
    #
    #   junction (red)  the end shares a cell with a different separator
    #   near     (pink) the end is 8-adjacent to one, diagonals included
    #
    # A separator's own body sits next to its own end by definition, so
    # the rule's own cells are excluded from both tests. Gutter and
    # content-edge lines are NOT separators and deliberately take no part
    # here -- this asks only what the ruling itself does.
    junction = [[False] * n_cols for _ in range(n_rows)]
    near = [[False] * n_cols for _ in range(n_rows)]
    NEIGHBOURS = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                  if (dy, dx) != (0, 0)]
    for i, r in enumerate(list(regions) + list(swallowed)):
        for (ex, ey) in _ends(r):
            cx = max(0, min(n_cols - 1, int(ex / CELL_PCT)))
            cy = max(0, min(n_rows - 1, int(ey / cell_h_pct)))
            if occupied.get((cy, cx), set()) - {i}:
                junction[cy][cx] = True
                continue
            for dy, dx in NEIGHBOURS:
                ny, nx = cy + dy, cx + dx
                if not (0 <= ny < n_rows and 0 <= nx < n_cols):
                    continue
                if occupied.get((ny, nx), set()) - {i}:
                    near[cy][cx] = True
                    break
    # Both kinds of vertical reference get the same tint: the point is
    # "a rule here has a reason to be here", and the content edge is as
    # good a reason as a gutter.
    gutters, edges = _gutter_centres(conn, page_id)
    gutter = [False] * n_cols
    for gx in gutters + edges:
        cx = int(gx / CELL_PCT)
        if 0 <= cx < n_cols:
            gutter[cx] = True
    n_gut, n_edge = len(gutters), len(edges)

    # PERIMETER only, not the filled rectangle: this grid is a view of
    # boundaries, and flooding the interior would bury the rules and
    # junctions that the rest of it is about.
    photo = [[False] * n_cols for _ in range(n_rows)]
    units = _photo_units(conn, page_id)
    for (L, T, R, B) in units:
        c0 = max(0, min(n_cols - 1, int(L / CELL_PCT)))
        c1 = max(0, min(n_cols - 1, int(R / CELL_PCT)))
        r0 = max(0, min(n_rows - 1, int(T / cell_h_pct)))
        r1 = max(0, min(n_rows - 1, int(B / cell_h_pct)))
        for x in range(c0, c1 + 1):
            photo[r0][x] = photo[r1][x] = True
        for y in range(r0, r1 + 1):
            photo[y][c0] = photo[y][c1] = True

    return (counts, junction, near, gutter, photo, n_cols, n_rows,
            len(regions), len(swallowed), len(units), n_gut, n_edge)


def _blend(base: tuple, tint: tuple, amount: float) -> tuple:
    return tuple(int(b * (1 - amount) + t * amount) for b, t in zip(base, tint))


def render(counts, junction, near, gutter, photo, n_cols, n_rows,
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
            # Base layer: how much ruling is in this cell.
            fill = None
            if n:
                v = int(255 * max(0.0, 1.0 - STEP_DARK * n))
                fill = (v, v, v)
            # Reference tints are BLENDED, so a rule sitting on a gutter
            # centre or a photo perimeter stays visible underneath them.
            if gutter[x]:
                fill = _blend(fill or (255, 255, 255), GUTTER, GUTTER_MIX)
            if photo[y][x]:
                fill = _blend(fill or (255, 255, 255), PHOTO, PHOTO_MIX)
            # Corners go on TOP and SOLID -- never blended. They are the
            # finding this whole view exists to show, and tinting them
            # with whatever happened to be underneath made them read as
            # just another shade. Red wins over pink: sharing a cell is
            # stronger evidence than being next to one.
            if near[y][x]:
                fill = NEAR
            if junction[y][x]:
                fill = JUNCTION
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
        (counts, junc, nr_, gut, photo, nc, nr,
         n, folded, nphoto, n_gut, n_edge) = build(conn, row["id"], args.clean)
        busiest = max(max(r) for r in counts)
        cells = sum(1 for r in counts for v in r if v)
        njunc = sum(1 for r in junc for v in r if v)
        nnear = sum(1 for r in nr_ for v in r if v)
        kind = "cleaned" if args.clean else "raw"
        title = (f"{args.date} p{args.page} — {n} {kind} separators "
                 f"(+{folded} folded) · {cells} cells · busiest {busiest} "
                 f"· {njunc} junction (red) + {nnear} near (pink) "
                 f"· {n_gut} gutters + {n_edge} "
                 f"content edges (yellow) · {nphoto} photo+caption "
                 f"perimeters (blue) · grid {nc}x{nr} at {CELL_PCT}% of width")
        suffix = "_clean" if args.clean else ""
        out = os.path.join(OUT_DIR, args.date,
                           f"p{args.page}_sepgrid{suffix}.png")
        print(" ", render(counts, junc, nr_, gut, photo, nc, nr, title, out))
        print(f"  {n} regions kept, {folded} folded as contained, "
              f"{cells} cells touched, busiest {busiest}, "
              f"{njunc} junctions + {nnear} near")
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
