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

from PIL import Image, ImageDraw, ImageFont

from .. import _support as _sup
from .. import detect_boxes as _boxes
from .. import detect_captions as _captions

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "grids")

CELL_PCT = 0.5        # cell side, as a percentage of PAGE WIDTH
CELL_PX = 10          # on-screen size of one cell
STEP_DARK = 0.25      # each separator in a cell adds this much darkness

# Graph-paper ruling: a very faint minor line, a slightly stronger one
# every MAJOR_EVERY cells. The minor line WAS (215,219,226) on every cell
# edge, which at 8px cells put ~23% of the image's pixels into grid ink --
# enough that any downscaling greyed the whole render out. Faint minor
# lines keep the grid readable while leaving the data to carry the image.
GRID_LINE = (238, 240, 244)
GRID_MAJOR = (206, 211, 220)
MAJOR_EVERY = 10
LABEL = (60, 66, 78)
FONT_PATHS = ("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/Library/Fonts/Arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_AXIS = 15
FONT_TITLE = 17
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


def _font(size: int):
    """A real TrueType face. PIL's default is a tiny bitmap font, which is
    what made the axis labels unreadable once the image was scaled."""
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# --- deriving boxes from the corner map ------------------------------
# A candidate edge counts as ruled if at least this share of the cells
# along it hold a separator. Below 1.0 because a rule's ends stop short of
# the corners (the rounded-corner inset, measured at 0.5-3.9%) and because
# Tesseract fragments rules.
EDGE_SUPPORT = 0.80
BOX_MIN_CELLS = 4          # a box smaller than this is furniture


def corner_points(junction, near, n_cols, n_rows) -> list[tuple]:
    """Corner positions, from the two kinds of mark.

    RED is a corner outright -- an end sharing a cell with another rule.

    PINK is only a corner when TWO pink cells sit DIAGONALLY adjacent.
    That pairing is the signature of a rounded corner: the horizontal
    stops short and the vertical stops short, so their two ends land on
    opposite diagonal cells with the true corner between them. A lone pink
    cell is a rule passing near another and means nothing on its own.

    Touching corner cells are then merged, so one physical corner yields
    one point however many cells it lit.
    """
    cand = set()
    for y in range(n_rows):
        for x in range(n_cols):
            if junction[y][x]:
                cand.add((y, x))
    for y in range(n_rows):
        for x in range(n_cols):
            if not near[y][x]:
                continue
            for dy, dx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < n_rows and 0 <= nx < n_cols and near[ny][nx]:
                    cand.add((y, x))
                    cand.add((ny, nx))
                    break

    seen, points = set(), []
    for c in sorted(cand):
        if c in seen:
            continue
        blob, stack = [], [c]
        seen.add(c)
        while stack:
            cy, cx = stack.pop()
            blob.append((cy, cx))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    n = (cy + dy, cx + dx)
                    if n in cand and n not in seen:
                        seen.add(n)
                        stack.append(n)
        ys = [b[0] for b in blob]
        xs = [b[1] for b in blob]
        points.append((sum(ys) / len(ys), sum(xs) / len(xs)))
    return points


def boxes_from_corners(points, counts, n_cols, n_rows, tol=1.6):
    """Rectangles whose four corners are all marked and whose four edges
    are actually ruled.

    Corners alone would give a combinatorial explosion of rectangles, so
    every candidate is checked against the ruling: an edge must be a real
    line on the page, not merely the shortest path between two corners.
    """
    xs = sorted({round(p[1]) for p in points})
    ys = sorted({round(p[0]) for p in points})

    def has_corner(y, x):
        return any(abs(p[0] - y) <= tol and abs(p[1] - x) <= tol
                   for p in points)

    def ruled_h(y, x0, x1):
        cells = [counts[y2][x] for x in range(x0, x1 + 1)
                 for y2 in (y - 1, y, y + 1) if 0 <= y2 < n_rows]
        hit = sum(1 for x in range(x0, x1 + 1)
                  if any(0 <= y2 < n_rows and counts[y2][x]
                         for y2 in (y - 1, y, y + 1)))
        return hit / max(1, x1 - x0 + 1) >= EDGE_SUPPORT

    def ruled_v(x, y0, y1):
        hit = sum(1 for y in range(y0, y1 + 1)
                  if any(0 <= x2 < n_cols and counts[y][x2]
                         for x2 in (x - 1, x, x + 1)))
        return hit / max(1, y1 - y0 + 1) >= EDGE_SUPPORT

    out = []
    for i, x0 in enumerate(xs):
        for x1 in xs[i + 1:]:
            if x1 - x0 < BOX_MIN_CELLS:
                continue
            for j, y0 in enumerate(ys):
                for y1 in ys[j + 1:]:
                    if y1 - y0 < BOX_MIN_CELLS:
                        continue
                    if not (has_corner(y0, x0) and has_corner(y0, x1)
                            and has_corner(y1, x0) and has_corner(y1, x1)):
                        continue
                    if not (ruled_h(y0, x0, x1) and ruled_h(y1, x0, x1)
                            and ruled_v(x0, y0, y1) and ruled_v(x1, y0, y1)):
                        continue
                    out.append((y0, x0, y1, x1))
    # Drop a box that duplicates one already found at the same place.
    keep = []
    for b in sorted(out, key=lambda b: -((b[2] - b[0]) * (b[3] - b[1]))):
        if not any(abs(b[0] - k[0]) <= tol and abs(b[1] - k[1]) <= tol
                   and abs(b[2] - k[2]) <= tol and abs(b[3] - k[3]) <= tol
                   for k in keep):
            keep.append(b)
    return keep


def _blend(base: tuple, tint: tuple, amount: float) -> tuple:
    return tuple(int(b * (1 - amount) + t * amount) for b, t in zip(base, tint))


def render(counts, junction, near, gutter, photo, n_cols, n_rows,
           title: str, out_path: str, boxes=None) -> str:
    pad_l, pad_t, pad_b = 46, 34, 30
    f_axis, f_title = _font(FONT_AXIS), _font(FONT_TITLE)
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

    # Grid lines LAST, so they sit over the fills without being repainted
    # per cell, and only one line per edge rather than one per cell.
    for x in range(n_cols + 1):
        major = x % MAJOR_EVERY == 0
        gx = pad_l + x * CELL_PX
        d.line([(gx, pad_t), (gx, pad_t + n_rows * CELL_PX)],
               fill=GRID_MAJOR if major else GRID_LINE)
    for y in range(n_rows + 1):
        major = y % MAJOR_EVERY == 0
        gy = pad_t + y * CELL_PX
        d.line([(pad_l, gy), (pad_l + n_cols * CELL_PX, gy)],
               fill=GRID_MAJOR if major else GRID_LINE)

    step = 20 if n_cols > 120 else 10
    for x in range(0, n_cols + 1, step):
        d.text((pad_l + x * CELL_PX, pad_t - 20), str(x),
               fill=LABEL, font=f_axis, anchor="mm")
    for y in range(0, n_rows + 1, step):
        d.text((pad_l - 22, pad_t + y * CELL_PX), str(y),
               fill=LABEL, font=f_axis, anchor="mm")

    # Derived boxes LAST, drawn 1px down the CENTRE LINE of the corner
    # cells -- the corner is inside that cell, not at its edge, so a
    # cell-boundary rectangle would sit half a cell out on every side.
    for (y0, x0, y1, x1) in (boxes or []):
        gx0 = pad_l + x0 * CELL_PX + CELL_PX // 2
        gx1 = pad_l + x1 * CELL_PX + CELL_PX // 2
        gy0 = pad_t + y0 * CELL_PX + CELL_PX // 2
        gy1 = pad_t + y1 * CELL_PX + CELL_PX // 2
        d.rectangle([gx0, gy0, gx1, gy1], outline=(0, 0, 0), width=1)

    d.text((pad_l, H - pad_b + 6), title, fill=(0, 0, 0), font=f_title)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


# The OVERLAY needs its OWN palette. The standalone chart draws separators
# in light grey against pure white, which is legible there -- but composited
# over a newspaper that grey is almost exactly the value of the paper and
# disappears. The page is greyscale, so the overlay uses SATURATED colours,
# which no part of the scan can compete with.
#
# Chosen to stay distinguishable without relying on hue discrimination
# (orange / blue / magenta / black, no red-green pairing).
OVERLAY_RULE = (235, 120, 0)      # separator -- orange, the main signal
OVERLAY_JUNCTION = (0, 0, 0)      # end meets end -- black, maximum contrast
OVERLAY_NEAR = (255, 0, 180)      # end near another -- magenta
OVERLAY_GUTTER = (245, 200, 40)   # reference lines sit back
OVERLAY_PHOTO = (30, 120, 220)

# White is fully transparent so the page reads through. Everything else is
# graded by how much it is meant to assert.
OVERLAY_ALPHA = {"rule": 235, "gutter": 85, "photo": 150,
                 "near": 255, "junction": 255}


def render_overlay(conn, page_id, counts, junction, near, gutter, photo,
                   n_cols, n_rows, out_path: str) -> str:
    """A page-sized RGBA overlay, for painting onto the IIIF canvas.

    Built at ONE PIXEL PER CELL and then scaled up with NEAREST to the
    canvas's exact pixel dimensions. That guarantees the overlay registers
    with the page rather than drifting by a rounding error, and keeps the
    cells hard-edged instead of interpolated.
    """
    row = conn.execute(
        "SELECT display_width_px w, display_height_px h FROM pages WHERE id=?",
        (page_id,)).fetchone()
    W, H = row["w"], row["h"]

    small = Image.new("RGBA", (n_cols, n_rows), (255, 255, 255, 0))
    px = small.load()
    for y in range(n_rows):
        for x in range(n_cols):
            rgb, a = None, 0
            # Reference lines first, so a real rule always paints over them.
            if gutter[x]:
                rgb, a = OVERLAY_GUTTER, OVERLAY_ALPHA["gutter"]
            if photo[y][x]:
                rgb, a = OVERLAY_PHOTO, OVERLAY_ALPHA["photo"]
            if counts[y][x]:
                rgb, a = OVERLAY_RULE, OVERLAY_ALPHA["rule"]
            if near[y][x]:
                rgb, a = OVERLAY_NEAR, OVERLAY_ALPHA["near"]
            if junction[y][x]:
                rgb, a = OVERLAY_JUNCTION, OVERLAY_ALPHA["junction"]
            if rgb:
                px[x, y] = (rgb[0], rgb[1], rgb[2], a)

    big = small.resize((W, H), Image.NEAREST)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    big.save(out_path)
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
        pts = corner_points(junc, nr_, nc, nr)
        derived = boxes_from_corners(pts, counts, nc, nr)
        title += f" · {len(pts)} corners -> {len(derived)} boxes"
        print(" ", render(counts, junc, nr_, gut, photo, nc, nr, title, out,
                          boxes=derived))
        ov = os.path.join(OUT_DIR, args.date,
                          f"p{args.page}_overlay{suffix}.png")
        print(" ", render_overlay(conn, row["id"], counts, junc, nr_, gut,
                                  photo, nc, nr, ov))
        print(f"  {n} regions kept, {folded} folded as contained, "
              f"{cells} cells touched, busiest {busiest}, "
              f"{njunc} junctions + {nnear} near, "
              f"{len(pts)} corners -> {len(derived)} boxes")
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
