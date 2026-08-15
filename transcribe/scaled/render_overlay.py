"""Draw the fitted grid over the real page image.

Exists because this project has been burned by trusting derived numbers.
`post1980_layout_observations.md` records a run whose own quality flags
reported 97% clean while the cuts were "beyond useless" -- the metrics
shared authorship with the code they graded. This experiment reproduced
that failure three times (see transcribe/scaled/archive/README.md), each
caught only by looking at the page.

So: `detect_grid` reports a fit number, and this puts that number back on
the pixels. Counting rows in `page_columns` is internal consistency, not
validation.

Colours stay distinguishable without hue discrimination (orange grid,
green photo regions, varied stroke).

Usage::

    python3 -m transcribe.scaled.render_overlay 1980-04-06 --page 11
    python3 -m transcribe.scaled.render_overlay 1980-04-06
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from . import _support as _sup
from . import detect_grid as _grid

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled")

GRID_COLOUR = (255, 140, 0)
PHOTO_COLOUR = (0, 150, 90)


def render_page(conn, page_row, out_path: str) -> str | None:
    img_path = page_row["display_image_path"]
    if not img_path or not os.path.isfile(img_path):
        return None
    img = Image.open(img_path).convert("RGB")
    w, h = img.size

    band = 96
    canvas = Image.new("RGB", (w, h + band), (255, 255, 255))
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)

    res = _grid.detect(conn, page_row["id"])
    g = res.get("grid")

    # Grid slots. An ad or photo spanning 2-3 slots is expected and
    # correct -- slots are the page's underlying measure, not a claim
    # about where visible text columns happen to fall.
    if g:
        for i in range(len(g["edges"]) - 1):
            x0 = _sup.pct_to_px(g["edges"][i], w)
            x1 = _sup.pct_to_px(min(100.0, g["edges"][i] + g["col_width"]), w)
            d.rectangle([x0, 0, x1, h - 1], outline=GRID_COLOUR, width=2)
            d.rectangle([x0 - 1, 0, x0 + 1, h], fill=GRID_COLOUR)

    # Tesseract's own photo regions -- free signal, drawn for context.
    for r in conn.execute(
        "SELECT left_pct, top_pct, right_pct, bottom_pct FROM page_hocr_regions "
        "WHERE page_id=? AND region_class='ocr_photo'", (page_row["id"],)
    ):
        d.rectangle(
            [_sup.pct_to_px(r["left_pct"], w), _sup.pct_to_px(r["top_pct"], h),
             _sup.pct_to_px(r["right_pct"], w), _sup.pct_to_px(r["bottom_pct"], h)],
            outline=PHOTO_COLOUR, width=3)

    y = h + 10
    d.text((12, y), f"{page_row['year']}-{page_row['month']:02d}-"
                    f"{page_row['day']:02d} p{page_row['page']}", fill=(0, 0, 0))
    y += 20
    d.rectangle([12, y + 4, 46, y + 8], fill=GRID_COLOUR)
    if g:
        d.text((56, y), f"COLUMNS — {g['n_columns']} slots · "
                        f"pitch {g['pitch']}% · column {g['col_width']}% · "
                        f"gutter {g['gutter']}% · fit {res['fit']:.2f} (diagnostic)",
               fill=(0, 0, 0))
        y += 20
        d.text((56, y), f"edges: {[round(e, 1) for e in g['edges']]}", fill=(60, 60, 60))
    else:
        d.text((56, y), f"COLUMNS — no fit ({res.get('note', '')})",
               fill=(0, 0, 0))
        y += 20
    y += 20
    d.rectangle([12, y + 4, 46, y + 8], fill=PHOTO_COLOUR)
    d.text((56, y), "ocr_photo regions (Tesseract's own)", fill=(0, 0, 0))

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
        if not rows:
            print(f"No pages for {args.date}")
            return
        out_dir = os.path.join(OUT_DIR, args.date)
        n = 0
        for r in rows:
            got = render_page(conn, r, os.path.join(out_dir, f"p{r['page']}_grid.jpg"))
            if got:
                n += 1
                print(f"  wrote {got}")
            else:
                print(f"  SKIP p{r['page']}: no display image on disk")
        if n:
            print(f"\n{n} render(s) in {out_dir}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--page", type=int, help="single page (default: whole issue)")
    p.set_defaults(func=_cmd)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
