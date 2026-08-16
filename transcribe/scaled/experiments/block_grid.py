"""EXPERIMENT — block and photo PERIMETERS on the grid.

A way of LOOKING at where item edges fall, at the same cell resolution
`separator_grid` uses for the ruling. Nothing derives from this yet.

WHY PERIMETERS AND NOT BOXES. The question this view exists to answer is
the one asked of the content area: how many things line up their top edges
here, or their right edges there. A filled box answers "what is covered";
a perimeter answers "where are the edges", and coincidence of edges is the
evidence. Each cell is shaded by HOW MANY perimeters pass through it, so
a cell where several items share an edge reads darker than a cell crossed
by one.

Shadow-derived separators are removed in a FIRST PASS, before anything is
drawn, and are shown separately so the removal itself can be checked
rather than trusted. The binding gutter and sheet edge come back from
Tesseract as long thin regions hard against the page edge, and they are
not printing.

Usage::

    python3 -m transcribe.scaled.experiments.block_grid 1980-04-06 --page 4
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from .. import _support as _sup
from . import separator_grid as _sg
from .. import sliver_pass as _sliver

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "grids")

# Colour-blind safe: blue / orange / black, distinguished by weight too.
BLOCK = (10, 90, 200)        # ocr_carea perimeter
PHOTO = (224, 120, 0)        # ocr_photo perimeter
RULE = (0, 0, 0)             # separator kept
SHADOW = (255, 25, 25)       # removed as a sliver -- drawn FILLED, see below
SHADOW_MIN_PX = 3            # a sliver is thinner than a pixel at this scale
STEP_DARK = 0.34             # each perimeter through a cell adds this much


def _items(conn, page_id: str):
    """Blocks, photos and separators, each tagged with its kind."""
    out = [dict(r, kind="ocr_carea") for r in conn.execute(
        "SELECT bbox_left_pct L, bbox_top_pct T, bbox_right_pct R, "
        "bbox_bottom_pct B FROM page_ocr_blocks WHERE page_id=? AND n_words>0",
        (page_id,))]
    out += [dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
        "region_class kind FROM page_hocr_regions WHERE page_id=?", (page_id,))]
    return [i for i in out if i["R"] > i["L"] and i["B"] > i["T"]]


def _perimeter_cells(i, cw, chh, n_cols, n_rows):
    """The cells an item's four edges pass through."""
    c = lambda v, s, n: max(0, min(n - 1, int(v / s)))
    l, r = c(i["L"], cw, n_cols), c(i["R"], cw, n_cols)
    t, b = c(i["T"], chh, n_rows), c(i["B"], chh, n_rows)
    cells = set()
    for x in range(l, r + 1):
        cells.add((t, x))
        cells.add((b, x))
    for y in range(t, b + 1):
        cells.add((y, l))
        cells.add((y, r))
    return cells


def _sliver_items(every, verdicts):
    """`every` re-keyed to the objects sliver_pass judged.

    Keyed on geometry, because the two modules run their own queries and
    the dicts are not the same objects. EVERY kind is re-keyed: an earlier
    version did separators only, so once photos became candidates their
    verdicts never reached the render and the shadow strips stayed drawn.
    """
    by_key = {(round(r["sep"]["L"], 4), round(r["sep"]["T"], 4),
               round(r["sep"]["R"], 4), round(r["sep"]["B"], 4),
               r["sep"]["kind"]): r["sep"]
              for r in verdicts}
    out = []
    for i in every:
        k = (round(i["L"], 4), round(i["T"], 4),
             round(i["R"], 4), round(i["B"], 4), i["kind"])
        out.append(by_key.get(k, i))
    return out


def build(conn, page_id: str):
    cw, chh = _sup.cell_size(conn, page_id)
    n_cols = int(round(100 / cw))
    n_rows = int(round(100 / chh))

    # FIRST PASS: drop the sliver separators, and keep them aside for the
    # render. The decision lives in `sliver_pass` -- one definition, so
    # this view cannot drift from the pass it is drawn to check.
    every = _items(conn, page_id)
    verdicts, _rim, _counts = _sliver.classify(conn, page_id)
    drop = {id(r["sep"]) for r in verdicts if r["verdict"] == "remove"}
    shadows = [r["sep"] for r in verdicts if r["verdict"] == "remove"]
    kept = [i for i in _sliver_items(every, verdicts) if id(i) not in drop]

    counts = [[0] * n_cols for _ in range(n_rows)]
    owner = [[None] * n_cols for _ in range(n_rows)]
    for i in kept:
        if i["kind"] == "ocr_separator":
            continue                      # rules are drawn, not counted
        if i["kind"] not in ("ocr_carea", "ocr_photo"):
            continue
        for (y, x) in _perimeter_cells(i, cw, chh, n_cols, n_rows):
            counts[y][x] += 1
            if owner[y][x] is None or i["kind"] == "ocr_photo":
                owner[y][x] = i["kind"]
    return counts, owner, kept, shadows, n_cols, n_rows, cw, chh


# A scan shadow is a SLIVER AT AN EDGE. Both conditions are needed and
# neither works alone, measured on 1980-04-06 p4:
#
#   item                       min dim   edge dist
#   photo  x  0.00- 2.34         4.7      0.0      shadow
#   photo  x 96.32-100.00        7.4      0.0      shadow
#   sep    x  1.13- 2.32         2.4      4.6      shadow
#   sep    x 81.17-81.50         0.7     37.0      a REAL column rule
#
# Proximity alone keeps the 1.13-2.32 shadow (it is 4.6 cells in, past a
# 4-cell band). Thinness alone kills the real column rule, which is
# thinner than any of them. Together they separate cleanly.
THIN_CELLS = 8.0
EDGE_BAND_CELLS = 8.0


MIN_AGREE = 2


def _agreement(i, others, cw, chh, vertical):
    """How many other items share this sliver's position on its long axis.

    A vertical sliver is asked about x, a horizontal one about y, and
    either of its two edges may be the one that matches -- a rule sitting
    in a gutter has the column to its left ending on it and the column to
    its right starting on it.
    """
    tol = cw if vertical else chh
    a1, a2 = (i["L"], i["R"]) if vertical else (i["T"], i["B"])
    n = 0
    for j in others:
        if j is i:
            continue
        b1, b2 = (j["L"], j["R"]) if vertical else (j["T"], j["B"])
        if min(abs(b1 - a1), abs(b2 - a2),
               abs(b1 - a2), abs(b2 - a1)) <= tol:
            n += 1
    return n


def _is_shadow(i, cw, chh, others=()):
    """A scan shadow: a sliver lying ALONG a page edge.

    The edge tested is the one the sliver runs PARALLEL to, which is the
    only one it can be hugging. A binding shadow or a sheet edge lies
    along the edge it came from; it does not cross the page.

    Taking the nearest edge in any direction instead was wrong, and
    1990-10-10 p5 showed it: a full-width horizontal rule at y 43.39-43.67
    spanning x 35.15-96.18 was dropped because its right END reaches
    within 7.6 cells of the right margin. It is 122 cells long, sits in
    the middle of the page, and is printing. Three of the eight items
    dropped on that page were this mistake, all perpendicular to the edge
    they were charged against.

    Separators and photos only. A block with words is real type, and the
    same test applied to blocks drops 664 of 8,969 corpus-wide -- narrow
    real columns, not artefacts.
    """
    if i["kind"] not in ("ocr_separator", "ocr_photo"):
        return False
    w, h = (i["R"] - i["L"]) / cw, (i["B"] - i["T"]) / chh
    if min(w, h) > THIN_CELLS:
        return False
    vertical = h >= w
    if vertical:
        edge = min(i["L"] / cw, (100 - i["R"]) / cw)
    else:
        edge = min(i["T"] / chh, (100 - i["B"]) / chh)
    if edge > EDGE_BAND_CELLS:
        return False
    # AND NOTHING AGREES WITH IT.
    #
    # Thin-and-near-an-edge alone drops real printing: the page margin is
    # 7-9 cells, so the band unavoidably reaches the content edge, and a
    # box border sitting ON that edge looks exactly like a shadow.
    # Measured, 244 of 773 items it dropped lay wholly INSIDE the content
    # area -- 32% false positives. 1990-10-10 p5's left panel border
    # (x 3.92-4.40, 19x5197px, the full height of the panel) was one.
    #
    # A shadow is corroborated by nothing; printing is corroborated by the
    # column it belongs to. On that page the panel border agrees with 15
    # other items, the right-hand rule with 11, and the true binding
    # shadow at x 99.11-99.31 with none.
    return _agreement(i, others, cw, chh, vertical) < MIN_AGREE


def render(counts, owner, kept, shadows, n_cols, n_rows, cw, chh,
           out_path: str) -> str:
    cell = max(1, min(_sg.PREVIEW_MAX_W // n_cols, _sg.PREVIEW_MAX_H // n_rows))
    W, H = n_cols * cell, n_rows * cell
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)

    for x in range(n_cols + 1):
        c = _sg.GRID_MAJOR if x % _sg.MAJOR_EVERY == 0 else _sg.GRID_LINE
        d.line([(x * cell, 0), (x * cell, H - 1)], fill=c)
    for y in range(n_rows + 1):
        c = _sg.GRID_MAJOR if y % _sg.MAJOR_EVERY == 0 else _sg.GRID_LINE
        d.line([(0, y * cell), (W - 1, y * cell)], fill=c)

    for y in range(n_rows):
        for x in range(n_cols):
            n = counts[y][x]
            if not n:
                continue
            tint = PHOTO if owner[y][x] == "ocr_photo" else BLOCK
            shade = _sg._blend((255, 255, 255), tint, min(1.0, n * STEP_DARK))
            d.rectangle([x * cell, y * cell,
                         (x + 1) * cell - 1, (y + 1) * cell - 1], fill=shade)

    for i in kept:
        if i["kind"] != "ocr_separator":
            continue
        d.rectangle([i["L"] / cw * cell, i["T"] / chh * cell,
                     max(i["R"] / cw * cell, i["L"] / cw * cell + 1),
                     max(i["B"] / chh * cell, i["T"] / chh * cell + 1)],
                    fill=RULE)
    # Every removed sliver, whatever its type, drawn FILLED so the FIRST
    # PASS can be judged rather than trusted. Outlining them was too faint:
    # a sliver is by definition thin, so at this scale its outline collapsed
    # to a 2px hairline that read as a smudge. Filled, and floored at
    # SHADOW_MIN_PX across, each removal is unmissable.
    for i in shadows:
        x0, y0 = i["L"] / cw * cell, i["T"] / chh * cell
        x1, y1 = i["R"] / cw * cell, i["B"] / chh * cell
        if x1 - x0 < SHADOW_MIN_PX:
            x1 = x0 + SHADOW_MIN_PX
        if y1 - y0 < SHADOW_MIN_PX:
            y1 = y0 + SHADOW_MIN_PX
        d.rectangle([x0, y0, x1, y1], fill=SHADOW)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date")
    p.add_argument("--page", type=int, required=True)
    a = p.parse_args()
    conn = _sup.open_connection()
    try:
        y, m, dd = (int(v) for v in a.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? "
                           "AND day=? AND page=?", (y, m, dd, a.page)).fetchone()
        if not row:
            print("no such page")
            return
        counts, owner, kept, shadows, nc, nr, cw, chh = build(conn, row["id"])
        n_block = sum(1 for i in kept if i["kind"] == "ocr_carea")
        n_photo = sum(1 for i in kept if i["kind"] == "ocr_photo")
        n_rule = sum(1 for i in kept if i["kind"] == "ocr_separator")
        busiest = max(max(r) for r in counts)
        lit = sum(1 for r in counts for v in r if v)
        print(f"  {a.date} p{a.page} — {n_block} blocks + {n_photo} photos "
              f"perimeters on a {nc}x{nr} grid · {n_rule} rules kept, "
              f"{len(shadows)} removed as shadow · {lit} cells lit, "
              f"busiest {busiest}")
        out = os.path.join(OUT_DIR, a.date, f"p{a.page}_blockgrid.png")
        print(" ", render(counts, owner, kept, shadows, nc, nr, cw, chh, out))
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
