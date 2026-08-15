"""Render detected structure over the real page image.

Exists because this project has already been burned by trusting derived
numbers: `post1980_layout_observations.md` records a run whose own
quality flags reported 97% clean while the cuts were, in the user's
words, "beyond useless" -- because the metrics shared authorship with
the code they graded. Counting rows in `page_columns` is internal
consistency, not validation. This puts the answer back on the pixels.

Colours are chosen to stay distinguishable for a colour-blind viewer
(blue / orange / black, varied dash patterns) rather than relying on a
red/green distinction.

Usage::

    python3 -m transcribe.scaled.render_overlay 1990-10-10 --page 2
    python3 -m transcribe.scaled.render_overlay 1980-04-06        # whole issue
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw

from . import _support as _sup
from . import detect_bands as _bands
from . import detect_columns as _cols

OUT_DIR = os.path.join(_sup.REPO_ROOT, "preview", "scaled")

# Blue / orange / black, distinguishable without hue discrimination.
STYLE = {
    "separator": {"colour": (0, 90, 200), "dash": None, "width": 5, "label": "separator rule"},
    "leftedge": {"colour": (230, 120, 0), "dash": (18, 12), "width": 4, "label": "left-edge cluster"},
    "valley": {"colour": (20, 20, 20), "dash": (6, 10), "width": 3, "label": "coverage valley"},
    "combined": {"colour": (200, 0, 120), "dash": None, "width": 3, "label": "COMBINED column edges"},
}


def _vline(draw, x, top, bottom, colour, width, dash):
    if not dash:
        draw.rectangle([x - width // 2, top, x + width // 2, bottom], fill=colour)
        return
    on, off = dash
    y = top
    while y < bottom:
        draw.rectangle([x - width // 2, y, x + width // 2, min(y + on, bottom)], fill=colour)
        y += on + off


def render_page(conn, page_row, out_path: str) -> str | None:
    img_path = page_row["display_image_path"]
    if not img_path or not os.path.isfile(img_path):
        return None
    img = Image.open(img_path).convert("RGB")
    w, h = img.size

    res = _cols.detect(conn, page_row["id"])

    # Widen the canvas so the legend never covers the page itself.
    band = 210
    canvas = Image.new("RGB", (w, h + band), (255, 255, 255))
    canvas.paste(img, (0, 0))
    d = ImageDraw.Draw(canvas)

    for name in ("separator", "leftedge", "valley"):
        st = STYLE[name]
        for b in res["signals"][name]:
            _vline(d, _sup.pct_to_px(b, w), 0, h, st["colour"], st["width"], st["dash"])

    st = STYLE["combined"]
    for e in res["edges"]:
        _vline(d, _sup.pct_to_px(e, w), 0, h, st["colour"], st["width"], st["dash"])

    # Photo regions Tesseract found -- free signal the LLM is currently
    # asked to derive by eye.
    for r in conn.execute(
        "SELECT left_pct, top_pct, right_pct, bottom_pct FROM page_hocr_regions "
        "WHERE page_id=? AND region_class='ocr_photo'", (page_row["id"],)
    ):
        d.rectangle(
            [_sup.pct_to_px(r["left_pct"], w), _sup.pct_to_px(r["top_pct"], h),
             _sup.pct_to_px(r["right_pct"], w), _sup.pct_to_px(r["bottom_pct"], h)],
            outline=(0, 150, 90), width=3)

    # Bands (stage 2 for 1980+): horizontal strip + its own column edges.
    bres = _bands.detect(conn, page_row["id"])
    for b in bres.get("bands", []):
        yt = _sup.pct_to_px(b["top_pct"], h)
        yb = _sup.pct_to_px(b["bottom_pct"], h)
        d.rectangle([2, yt, w - 3, yb], outline=(150, 0, 200), width=4)
        for e in b["edges"]:
            x = _sup.pct_to_px(e, w)
            d.rectangle([x - 2, yt, x + 2, yb], fill=(150, 0, 200))
        d.text((6, yt + 4), f"band {b['band_idx']}: {b['n_columns']} col "
                            f"reg={b['regularity']:.2f}", fill=(150, 0, 200))

    y = h + 10
    d.text((12, y), f"{page_row['year']}-{page_row['month']:02d}-{page_row['day']:02d} "
                    f"p{page_row['page']}   confidence={res['confidence']}   "
                    f"{'ESCALATE' if res['escalate'] else 'accepted'}", fill=(0, 0, 0))
    y += 18
    d.text((12, y), f"parts: {res['confidence_parts']}", fill=(60, 60, 60))
    y += 22
    for name in ("separator", "leftedge", "valley", "combined"):
        st = STYLE[name]
        d.rectangle([12, y + 4, 46, y + 8], fill=st["colour"])
        vals = res["edges"] if name == "combined" else res["signals"][name]
        d.text((56, y), f"{st['label']}: {[round(v, 1) for v in vals]}", fill=(0, 0, 0))
        y += 20
    d.rectangle([12, y + 4, 46, y + 8], fill=(0, 150, 90))
    d.text((56, y), "ocr_photo regions (Tesseract's own)", fill=(0, 0, 0))
    y += 20
    d.rectangle([12, y + 4, 46, y + 8], fill=(150, 0, 200))
    d.text((56, y), f"BANDS + per-band columns — conf={bres.get('confidence')} "
                    f"{bres.get('confidence_parts', '')}", fill=(0, 0, 0))

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
        sql += " ORDER BY page"
        rows = [dict(r) for r in conn.execute(sql, params)]
        if not rows:
            print(f"No pages for {args.date}")
            return
        out_dir = os.path.join(OUT_DIR, args.date)
        written = []
        for r in rows:
            p = os.path.join(out_dir, f"p{r['page']}_columns.jpg")
            got = render_page(conn, r, p)
            if got:
                written.append(got)
                print(f"  wrote {got}")
            else:
                print(f"  SKIP p{r['page']}: no display image on disk")
        if written:
            print(f"\n{len(written)} render(s) in {out_dir}")
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
