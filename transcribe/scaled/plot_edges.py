"""Plot the signal the grid fit rests on: BLOCK edge positions,
weighted by block HEIGHT.

Y is summed item height, not item count. A tall block running down a
column is strong evidence of that column's edge; a pile of small
fragments at the same x is not. hOCR lines are not plotted and not
fitted -- they are referred to only to derive the minimum height a block
must exceed.

Two panels, stacked, so the refinement step can be judged directly:

  TOP     before refinement -- every block, and the pass-1 rigid lattice
  BOTTOM  after refinement  -- stray blocks subsumed, and the pass-2
                              columns snapped to the majority alignment

If the typesetting model is right, the histogram should show spikes at
regular intervals, because every block was aligned to the same printed
column guides. A smooth or shapeless distribution would falsify the
premise.

No matplotlib on this machine, so this draws with PIL -- keeping the
package dependency-free, like the rest of `scaled`.

Usage::

    python3 -m transcribe.scaled.plot_edges 1980-04-06 --page 11
    python3 -m transcribe.scaled.plot_edges 1980-04-06
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from . import _support as _sup
from . import detect_grid as _grid

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "edge_plots")

W = 1400
PANEL_H = 300
PAD_L, PAD_R, PAD_T, PAD_B = 70, 24, 46, 46
BIN_PCT = 0.25

LEFT_COLOUR = (10, 90, 200)      # blue
RIGHT_COLOUR = (0, 95, 55)       # dark green
GRID_COLOUR = (235, 185, 10)     # yellow-gold
GUTTER_COLOUR = (252, 240, 190)  # pale wash marking the gutter band
DROP_COLOUR = (200, 60, 140)     # blocks removed by the refinement


def _hist(vals: list[float], nbins: int,
          weights: list[float] | None = None) -> list[float]:
    """Summed weight per bin. Weights are item HEIGHTS -- the same Y
    measure the fit uses -- so the chart shows what the detector sees,
    not a different quantity."""
    h = [0.0] * nbins
    for i, v in enumerate(vals):
        h[min(nbins - 1, max(0, int(v / BIN_PCT)))] += (weights[i] if weights else 1.0)
    return h


def _panel(d: ImageDraw.ImageDraw, top: int, title: str,
           lefts: list[float], rights: list[float],
           columns: list[tuple[float, float]],
           dropped: list[float], peak: float,
           wl: list[float] | None = None, wr: list[float] | None = None,
           wd: list[float] | None = None) -> None:
    """One histogram panel drawn at vertical offset `top`."""
    nbins = int(100 / BIN_PCT) + 1
    pw = W - PAD_L - PAD_R
    ph = PANEL_H - PAD_T - PAD_B
    base = top + PAD_T + ph

    # Gutters first, behind everything.
    for i in range(len(columns) - 1):
        xr = PAD_L + pw * (columns[i][1] / 100.0)
        xn = PAD_L + pw * (columns[i + 1][0] / 100.0)
        if xn > xr:
            d.rectangle([xr, top + PAD_T, xn, base], fill=GUTTER_COLOUR)

    # Axes
    d.line([(PAD_L, base), (PAD_L + pw, base)], fill=(0, 0, 0), width=2)
    d.line([(PAD_L, top + PAD_T), (PAD_L, base)], fill=(0, 0, 0), width=2)
    for pct in range(0, 101, 10):
        x = PAD_L + pw * (pct / 100.0)
        d.line([(x, base), (x, base + 5)], fill=(0, 0, 0), width=1)
        d.text((x - 8, base + 10), f"{pct}", fill=(0, 0, 0))
    for i in range(5):
        c = peak * i / 4
        y = base - ph * (c / peak)
        d.line([(PAD_L - 5, y), (PAD_L, y)], fill=(0, 0, 0), width=1)
        d.text((PAD_L - 38, y - 6), f"{c:.0f}", fill=(0, 0, 0))

    hl, hr = _hist(lefts, nbins, wl), _hist(rights, nbins, wr)
    hd = _hist(dropped, nbins, wd)
    bw = max(1, pw / nbins)

    # Bars at their TRUE x. Where series share a bin the taller is drawn
    # first so the shorter stays visible.
    for i in range(nbins):
        x = PAD_L + pw * (i * BIN_PCT / 100.0)
        stack = []
        if hl[i]:
            stack.append((hl[i], LEFT_COLOUR))
        if hr[i]:
            stack.append((hr[i], RIGHT_COLOUR))
        if hd[i]:
            stack.append((hd[i], DROP_COLOUR))
        for cnt, colour in sorted(stack, reverse=True):
            d.rectangle([x, base - ph * (cnt / peak), x + bw, base], fill=colour)

    # Column edges last, on top.
    for l, r in columns:
        for x_pct in (l, r):
            x = PAD_L + pw * (x_pct / 100.0)
            d.line([(x, top + PAD_T), (x, base)], fill=GRID_COLOUR, width=3)

    d.text((PAD_L, top + 10), title, fill=(0, 0, 0))


def plot_page(conn, page_row, out_path: str) -> str:
    pid = page_row["id"]
    res = _grid.detect(conn, pid)
    g = res.get("grid")

    blocks = _grid.page_blocks(conn, pid)
    if not blocks:
        return ""
    kept, subsumed = _grid.subsume_stray_blocks(blocks)

    # Same truncation the fit applies: taller than MIN_ITEM_HEIGHT_LINES
    # text lines, shorter than MAX_ITEM_HEIGHT_FRAC of the page.
    line_h = _grid.median_line_height(conn, pid)
    kept = [b for b in kept if _grid.usable(b, line_h)]
    blocks_u = [b for b in blocks if _grid.usable(b, line_h)]

    hgt = lambda bs: [b["B"] - b["T"] for b in bs]
    raw_l = [b["L"] for b in blocks_u]
    raw_r = [b["R"] for b in blocks_u]
    raw_h = hgt(blocks_u)
    kept_l = [b["L"] for b in kept]
    kept_r = [b["R"] for b in kept]
    kept_h = hgt(kept)
    drop_l = [b["L"] for b in subsumed]
    drop_h = hgt(subsumed)

    # Pass-1 lattice: rigid, every slot the same width.
    before_cols = []
    if g:
        before_cols = [(round(g["offset"] + k * g["pitch"], 2),
                        round(g["offset"] + k * g["pitch"] + g["col_width"], 2))
                       for k in range(g["n_columns"])]
    after_cols = [(c["left_pct"], c["right_pct"]) for c in res.get("columns", [])]

    nbins = int(100 / BIN_PCT) + 1
    peak = max([1.0] + _hist(raw_l, nbins, raw_h) + _hist(raw_r, nbins, raw_h))

    img = Image.new("RGB", (W, PANEL_H * 2 + 30), (255, 255, 255))
    d = ImageDraw.Draw(img)
    date = f"{page_row['year']}-{page_row['month']:02d}-{page_row['day']:02d}"

    _panel(d, 0,
           f"{date} p{page_row['page']} — BEFORE refinement: {len(blocks_u)} blocks "
           f"(of {len(blocks)}, short/full-height truncated), pass-1 rigid lattice"
           + (f" — {g['n_columns']} slots, pitch {g['pitch']}%, col {g['col_width']}%"
              if g else " (no fit)"),
           raw_l, raw_r, before_cols, [], peak, raw_h, raw_h, None)

    _panel(d, PANEL_H + 30,
           f"AFTER refinement: {len(kept)} blocks ({len(subsumed)} stray subsumed), "
           f"pass-2 columns snapped to majority alignment "
           f"({res.get('edges_snapped', 0)}/{res.get('edges_total', 0)} edges moved)",
           kept_l, kept_r, after_cols, drop_l, peak, kept_h, kept_h, drop_h)

    # Legend along the bottom.
    y = PANEL_H * 2 + 8
    lx = PAD_L
    for colour, label in ((LEFT_COLOUR, "left edges"), (RIGHT_COLOUR, "right edges"),
                          (DROP_COLOUR, "subsumed (lower panel)"),
                          (GRID_COLOUR, "column edges"), (GUTTER_COLOUR, "gutter")):
        d.rectangle([lx, y, lx + 16, y + 10], fill=colour)
        d.text((lx + 22, y - 2), label, fill=(0, 0, 0))
        lx += 30 + 7 * len(label)

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
        for r in conn.execute(sql + " ORDER BY page", params):
            p = os.path.join(OUT_DIR, args.date, f"p{r['page']}_edges.png")
            got = plot_page(conn, dict(r), p)
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
