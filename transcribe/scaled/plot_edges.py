"""Plot the raw signal the grid fit rests on: a histogram of block
left-edge x-positions.

No matplotlib on this machine, so this draws with PIL -- which keeps the
package dependency-free and consistent with the rest of `scaled`.

The point of looking at this directly: if the typesetting model is right,
this histogram should show *spikes at regular intervals*, because every
block was aligned to the same printed column guides. A smooth or
shapeless distribution would falsify the premise.

Usage::

    python3 -m transcribe.scaled.plot_edges 1980-04-06 --page 11
    python3 -m transcribe.scaled.plot_edges 1980-04-06          # all pages
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from . import _support as _sup
from . import detect_grid as _grid

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "edge_plots")

W, H = 1400, 520
PAD_L, PAD_R, PAD_T, PAD_B = 70, 24, 60, 70
BIN_PCT = 0.25

LEFT_COLOUR = (10, 90, 200)      # blue
RIGHT_COLOUR = (0, 95, 55)       # dark green
GRID_COLOUR = (235, 185, 10)     # yellow-gold; read as too red at (200,120,0)
GUTTER_COLOUR = (252, 240, 190)  # pale wash marking the gutter band


def plot_page(conn, page_row, out_path: str) -> str:
    rows = conn.execute(
        "SELECT bbox_left_pct L, bbox_right_pct R FROM page_ocr_blocks WHERE page_id=?",
        (page_row["id"],)).fetchall()
    lefts = [r["L"] for r in rows]
    rights = [r["R"] for r in rows]
    if not lefts:
        return ""

    nbins = int(100 / BIN_PCT) + 1
    hist_l = [0] * nbins
    hist_r = [0] * nbins
    for v in lefts:
        hist_l[min(nbins - 1, int(v / BIN_PCT))] += 1
    for v in rights:
        hist_r[min(nbins - 1, int(v / BIN_PCT))] += 1
    peak = max(max(hist_l), max(hist_r)) or 1

    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    pw = W - PAD_L - PAD_R
    ph = H - PAD_T - PAD_B

    res = _grid.detect(conn, page_row["id"])
    g = res.get("grid")

    # Axes
    d.line([(PAD_L, PAD_T + ph), (PAD_L + pw, PAD_T + ph)], fill=(0, 0, 0), width=2)
    d.line([(PAD_L, PAD_T), (PAD_L, PAD_T + ph)], fill=(0, 0, 0), width=2)
    for pct in range(0, 101, 10):
        x = PAD_L + pw * (pct / 100.0)
        d.line([(x, PAD_T + ph), (x, PAD_T + ph + 6)], fill=(0, 0, 0), width=1)
        d.text((x - 8, PAD_T + ph + 12), f"{pct}", fill=(0, 0, 0))
    step = max(1, peak // 5)
    for c in range(0, peak + 1, step):
        y = PAD_T + ph - ph * (c / peak)
        d.line([(PAD_L - 6, y), (PAD_L, y)], fill=(0, 0, 0), width=1)
        d.text((PAD_L - 34, y - 6), f"{c}", fill=(0, 0, 0))

    # (gutter wash is painted before the bars -- see below)
    if g:
        for i in range(len(g["edges"]) - 1):
            l = g["edges"][i]
            r = min(100.0, l + g["col_width"])
            nxt = g["edges"][i + 1]
            xr = PAD_L + pw * (r / 100.0)
            xn = PAD_L + pw * (nxt / 100.0)
            if xn > xr:
                d.rectangle([xr, PAD_T, xn, PAD_T + ph], fill=GUTTER_COLOUR)

    # Bars sit at their TRUE x position -- no side-by-side offset, so a
    # bar's position is exactly the measured edge position. Where both
    # series land in the same bin, the taller is drawn first so the
    # shorter stays visible on top of it.
    bw = max(1, pw / nbins)
    for i in range(nbins):
        x = PAD_L + pw * (i * BIN_PCT / 100.0)
        pair = []
        if hist_l[i]:
            pair.append((hist_l[i], LEFT_COLOUR))
        if hist_r[i]:
            pair.append((hist_r[i], RIGHT_COLOUR))
        for cnt, colour in sorted(pair, reverse=True):
            y = PAD_T + ph - ph * (cnt / peak)
            d.rectangle([x, y, x + bw, PAD_T + ph], fill=colour)

    # Predicted grid LAST, so it reads on top of the bars. Both edges of
    # every column are drawn -- start AND start+col_width -- so the
    # GUTTER between one column's right edge and the next column's left
    # edge is visible as a gap, rather than being implied by a single
    # line per slot.
    if g:
        for i in range(len(g["edges"]) - 1):
            l = g["edges"][i]
            r = min(100.0, l + g["col_width"])
            for x_pct in (l, r):
                x = PAD_L + pw * (x_pct / 100.0)
                d.line([(x, PAD_T), (x, PAD_T + ph)], fill=GRID_COLOUR, width=3)

    date = f"{page_row['year']}-{page_row['month']:02d}-{page_row['day']:02d}"
    d.text((PAD_L, 12), f"{date} p{page_row['page']} — block edges, "
                        f"{len(lefts)} blocks, {BIN_PCT}% bins", fill=(0, 0, 0))
    lx = PAD_L
    d.rectangle([lx, 32, lx + 16, 42], fill=LEFT_COLOUR)
    d.text((lx + 22, 30), "left edges", fill=(0, 0, 0))
    lx += 110
    d.rectangle([lx, 32, lx + 16, 42], fill=RIGHT_COLOUR)
    d.text((lx + 22, 30), "right edges", fill=(0, 0, 0))
    lx += 118
    d.rectangle([lx, 32, lx + 16, 42], fill=GUTTER_COLOUR)
    d.rectangle([lx, 32, lx + 2, 42], fill=GRID_COLOUR)
    d.rectangle([lx + 14, 32, lx + 16, 42], fill=GRID_COLOUR)
    if g:
        d.text((lx + 22, 30), f"column edges + gutter — {g['n_columns']} slots, "
                              f"pitch {g['pitch']}%, col {g['col_width']}%, "
                              f"gutter {g['gutter']}%, fit {res['fit']:.2f}",
               fill=(0, 0, 0))
    else:
        d.text((lx + 22, 30), "predicted grid — no fit", fill=(0, 0, 0))
    d.text((PAD_L + pw // 2 - 60, H - 26), "x position (% of page width)", fill=(0, 0, 0))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.save(out_path)
    return out_path


def _cmd(args):
    conn = _sup.open_connection()
    try:
        y, m, dd = (int(x) for x in args.date.split("-"))
        sql = ("SELECT id, year, month, day, page FROM pages "
               "WHERE year=? AND month=? AND day=?")
        params = [y, m, dd]
        if args.page:
            sql += " AND page=?"
            params.append(args.page)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY page", params)]
        for r in rows:
            p = os.path.join(OUT_DIR, args.date, f"p{r['page']}_left_edges.png")
            got = plot_page(conn, r, p)
            print(f"  {got or '(no blocks) p%d' % r['page']}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date")
    p.add_argument("--page", type=int)
    p.set_defaults(func=_cmd)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
