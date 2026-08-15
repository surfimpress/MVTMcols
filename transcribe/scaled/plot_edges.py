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


def plot_page(conn, page_row, out_path: str) -> str:
    rows = conn.execute(
        "SELECT bbox_left_pct L FROM page_ocr_blocks WHERE page_id=?",
        (page_row["id"],)).fetchall()
    lefts = [r["L"] for r in rows]
    if not lefts:
        return ""

    nbins = int(100 / BIN_PCT) + 1
    hist = [0] * nbins
    for v in lefts:
        hist[min(nbins - 1, int(v / BIN_PCT))] += 1
    peak = max(hist) or 1

    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    pw = W - PAD_L - PAD_R
    ph = H - PAD_T - PAD_B

    # The fitted grid, drawn behind the bars so the bars stay readable.
    res = _grid.detect(conn, page_row["id"])
    g = res.get("grid")
    if g:
        for e in g["edges"]:
            x = PAD_L + pw * (e / 100.0)
            d.line([(x, PAD_T), (x, PAD_T + ph)], fill=(255, 190, 120), width=3)

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

    # Bars
    bw = max(1, pw / nbins)
    for i, c in enumerate(hist):
        if not c:
            continue
        x = PAD_L + pw * (i * BIN_PCT / 100.0)
        y = PAD_T + ph - ph * (c / peak)
        d.rectangle([x, y, x + bw, PAD_T + ph], fill=(10, 90, 200))

    date = f"{page_row['year']}-{page_row['month']:02d}-{page_row['day']:02d}"
    d.text((PAD_L, 14), f"{date} p{page_row['page']} — left edges of "
                        f"{len(lefts)} blocks, {BIN_PCT}% bins", fill=(0, 0, 0))
    if g:
        d.text((PAD_L, 32), f"orange = fitted grid: {g['n_columns']} cols, "
                            f"pitch {g['pitch']}%, col {g['col_width']}%, "
                            f"gutter {g['gutter']}%, fit {res['confidence']:.2f}",
               fill=(190, 110, 0))
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
