"""Draw stage 3's horizontal alignments over the real page.

Same reason `render_overlay.py` exists: this project has repeatedly been
misled by derived numbers that looked healthy while the output was
wrong, and every one of those was caught by looking at the page. Stage
3's predecessor scored a 62%-tall heterogeneous band at 0.917.

Each alignment is drawn only across the COLUMNS IT SPANS, because that
span is the claim being made. A line drawn edge-to-edge would hide the
thing most worth checking.

Colour encodes where the evidence came from, not how good it is:
  orange  a printed horizontal rule contributed
  blue    photo / header evidence, no rule
  grey    block edges only
Line thickness grows with the number of agreeing columns.

Usage::

    python3 -m transcribe.scaled.render_hlines 1980-04-06 --page 11
    python3 -m transcribe.scaled.render_hlines 1980-04-06 --min-cols 3
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from . import _support as _sup
from . import detect_grid as _grid
from . import detect_hlines as _hl

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled")

RULE_COLOUR = (255, 140, 0)      # a printed rule contributed
SOFT_COLOUR = (10, 90, 200)      # photo / header, no rule
BLOCK_COLOUR = (150, 150, 150)   # block edges only
CONTENT_COLOUR = (200, 60, 140)  # page content top / bottom
COL_COLOUR = (225, 225, 225)     # stage-2 columns, for context


def render_page(conn, page_row, out_path: str, min_cols: int = 2) -> str | None:
    img_path = page_row["display_image_path"]
    if not img_path or not os.path.isfile(img_path):
        return None
    img = Image.open(img_path).convert("RGB")
    w, h = img.size

    band = 108
    canvas = Image.new("RGB", (w, h + band), (255, 255, 255))
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)

    res = _hl.detect(conn, page_row["id"])
    cols = _grid.detect(conn, page_row["id"]).get("columns") or []
    if not cols:
        return None

    # Columns first, faintly -- context for reading the spans.
    for c in cols:
        x0 = _sup.pct_to_px(c["left_pct"], w)
        x1 = _sup.pct_to_px(c["right_pct"], w)
        d.rectangle([x0, 0, x1, h - 1], outline=COL_COLOUR, width=1)

    shown = 0
    for a in res["alignments"]:
        if a["n_columns"] < min_cols:
            continue
        shown += 1
        y = _sup.pct_to_px(a["y_pct"], h)
        x0 = _sup.pct_to_px(cols[a["col_lo"]]["left_pct"], w)
        x1 = _sup.pct_to_px(cols[min(a["col_hi"], len(cols) - 1)]["right_pct"], w)
        if a["has_rule"]:
            colour = RULE_COLOUR
        elif "photo" in a["kinds"] or "header" in a["kinds"]:
            colour = SOFT_COLOUR
        else:
            colour = BLOCK_COLOUR
        d.line([(x0, y), (x1, y)], fill=colour, width=1 + min(3, a["n_columns"] // 3))

    for key, label in (("content_top", "top"), ("content_bottom", "bottom")):
        v = res.get(key)
        if v is None:
            continue
        y = _sup.pct_to_px(v, h)
        for x in range(0, w, 14):    # dashed, so it reads as a different kind
            d.line([(x, y), (x + 7, y)], fill=CONTENT_COLOUR, width=3)

    y = h + 8
    d.text((12, y), f"{page_row['year']}-{page_row['month']:02d}-"
                    f"{page_row['day']:02d} p{page_row['page']}  —  "
                    f"{shown} alignments shown (of {len(res['alignments'])}), "
                    f"min {min_cols} agreeing columns", fill=(0, 0, 0))
    y += 20
    for colour, label in (
        (RULE_COLOUR, "printed horizontal rule contributed"),
        (SOFT_COLOUR, "photo / heading evidence"),
        (BLOCK_COLOUR, "block edges only"),
        (CONTENT_COLOUR, f"content extent  {res['content_top']}% – "
                         f"{res['content_bottom']}%"),
    ):
        d.rectangle([12, y + 4, 46, y + 7], fill=colour)
        d.text((56, y), label, fill=(0, 0, 0))
        y += 18

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, quality=88)
    return out_path


def _cmd(args):
    conn = _sup.open_connection()
    try:
        y, m, dd = (int(x) for x in args.date.split("-"))
        sql = ("SELECT id, year, month, day, page, display_image_path FROM pages "
               "WHERE year=? AND month=? AND day=?")
        params = [y, m, dd]
        if args.page:
            sql += " AND page=?"
            params.append(args.page)
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY page", params)]
        suffix = f"_hl{args.min_cols}" if args.min_cols != 2 else "_hl"
        for r in rows:
            got = render_page(conn, r, os.path.join(
                OUT_DIR, args.date, f"p{r['page']}{suffix}.jpg"), args.min_cols)
            print(f"  {got or 'SKIP p%d' % r['page']}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date")
    p.add_argument("--page", type=int)
    p.add_argument("--min-cols", type=int, default=2,
                   help="only draw alignments agreed by at least this many columns")
    p.set_defaults(func=_cmd)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
