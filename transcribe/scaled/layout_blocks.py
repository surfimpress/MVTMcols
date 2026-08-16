"""Stage 1a — Tesseract's OWN layout analysis, which hOCR throws away.

`AnalyseLayout()` runs Tesseract's page layout phase WITHOUT recognition
and exposes two things the hOCR renderer discards:

  BlockPolygon()  the region's actual outline. Smith calls these isothetic
                  region polygons: edges alternate between horizontal and
                  parallel to the mean tab line, with the minimum number
                  of vertices such that no region's polygon intersects
                  another region's partitions. An L-shaped story wrapping
                  a display ad has an L-shaped polygon. **hOCR flattens
                  every one of them to a bounding box.** Measured on
                  2001-01-03 p7: 40 of 93 blocks are non-rectangular.

  BlockType()     the full PolyBlockType, 15 values, including the three
                  IMAGE variants and the two LINE variants that never
                  reach hOCR at all.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
The hOCR line classes we already parse are NOT typography labels, they
are this same block type projected onto lines. From `publictypes.h`:

    PT_FLOWING_TEXT   "Text that lives inside a column."
    PT_HEADING_TEXT   "Text that spans more than one column."
    PT_PULLOUT_TEXT   "Text that is in a cross-column pull-out region."

So `ocr_header` is a COLUMN-SPAN verdict, not a font observation.
Confirmed on this corpus against our own fitted lattice:

    class            n      median columns spanned   % spanning >1
    ocr_line     23,260              1                    20%
    ocr_textfloat 2,377              1                    43%
    ocr_header      503              2                    77%
    ocr_caption     319              2                    53%

That also retires a complaint recorded in `scaled_pipeline.md` 5j -- that
`ocr_caption` is unreliable because "it tags p7's page headline as a
caption". A page headline genuinely does span columns. Tesseract was
answering a different question from the one we were asking it.

CONFIG MATCHES THE hOCR RUN, deliberately: same image (`page_full.png`),
same Sauvola thresholding, same tessdata_best, same OEM. Layout analysis
is sensitive to all of them, and polygons that do not correspond to the
blocks we already store would be useless for cross-checking.

Usage::

    python3 -m transcribe.scaled.layout_blocks show 2001-01-03 --page 7
    python3 -m transcribe.scaled.layout_blocks run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import json
import os

from . import _support as _sup

# Same as ocr_llm's hOCR run. Sauvola (thresholding_method=2) is not
# cosmetic here: it is what stopped grey fold-shadow from blacking out a
# whole sidebar on greyscale scans.
THRESHOLDING_METHOD = "2"
TESSDATA = os.path.join(_sup.TRANSCRIBE_DIR, "work", "tessdata_best")


def _api():
    from tesserocr import PyTessBaseAPI, PSM, OEM
    td = TESSDATA if os.path.isfile(os.path.join(TESSDATA, "eng.traineddata")) \
        else "/opt/homebrew/share/tessdata"
    api = PyTessBaseAPI(path=td, psm=PSM.AUTO_ONLY, oem=OEM.LSTM_ONLY)
    api.SetVariable("thresholding_method", THRESHOLDING_METHOD)
    return api


def analyse(page_id: str, image_path: str) -> list[dict]:
    """Every layout block Tesseract finds, with its polygon, in PERCENT."""
    from tesserocr import RIL
    from PIL import Image
    with Image.open(image_path) as im:
        W, H = im.size
    out = []
    with _api() as api:
        api.SetImageFile(image_path)
        api.AnalyseLayout()
        it = api.GetIterator()
        if it is None:
            return out
        it.Begin()
        idx = 0
        while True:
            bt = it.BlockType()
            box = it.BoundingBox(RIL.BLOCK)
            poly = it.BlockPolygon()
            if box:
                x0, y0, x1, y1 = box
                pts = ([[_sup.px_to_pct(p[0], W), _sup.px_to_pct(p[1], H)]
                        for p in poly] if poly else None)
                out.append({
                    "idx": idx,
                    "block_type": _type_name(bt),
                    "left_pct": _sup.px_to_pct(x0, W),
                    "top_pct": _sup.px_to_pct(y0, H),
                    "right_pct": _sup.px_to_pct(x1, W),
                    "bottom_pct": _sup.px_to_pct(y1, H),
                    "n_points": len(poly) if poly else 0,
                    "polygon": pts,
                })
                idx += 1
            if not it.Next(RIL.BLOCK):
                break
    return out


def _type_name(bt) -> str:
    import tesserocr
    for name in dir(tesserocr.PT):
        if name.startswith("_"):
            continue
        if getattr(tesserocr.PT, name) == bt:
            return name
    return str(bt)


def _ensure_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS page_layout_blocks (
        id TEXT PRIMARY KEY, page_id TEXT NOT NULL, idx INTEGER NOT NULL,
        block_type TEXT NOT NULL,
        left_pct REAL NOT NULL, top_pct REAL NOT NULL,
        right_pct REAL NOT NULL, bottom_pct REAL NOT NULL,
        n_points INTEGER NOT NULL, polygon_json TEXT,
        created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_layout_blocks_page "
                 "ON page_layout_blocks(page_id)")
    conn.commit()


def store(conn, page_id: str, blocks: list[dict]) -> None:
    _ensure_schema(conn)
    conn.execute("DELETE FROM page_layout_blocks WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for b in blocks:
        conn.execute(
            """INSERT INTO page_layout_blocks
               (id, page_id, idx, block_type, left_pct, top_pct, right_pct,
                bottom_pct, n_points, polygon_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, b["idx"], b["block_type"],
             b["left_pct"], b["top_pct"], b["right_pct"], b["bottom_pct"],
             b["n_points"],
             json.dumps(b["polygon"]) if b["polygon"] else None, now))
    conn.commit()


def blocks_of(conn, page_id: str) -> list[dict]:
    _ensure_schema(conn)
    return [dict(r, polygon=json.loads(r["polygon_json"])
                 if r["polygon_json"] else None)
            for r in conn.execute(
                "SELECT * FROM page_layout_blocks WHERE page_id=? ORDER BY idx",
                (page_id,))]


def _image_for(conn, page_id: str) -> str | None:
    r = conn.execute("SELECT page_raw_path, display_image_path FROM pages "
                     "WHERE id=?", (page_id,)).fetchone()
    if not r:
        return None
    for k in ("page_raw_path", "display_image_path"):
        if r[k] and os.path.isfile(r[k]):
            return r[k]
    return None


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(v) for v in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? "
                           "AND day=? AND page=?", (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        img = _image_for(conn, row["id"])
        blocks = analyse(row["id"], img)
        nonrect = sum(1 for b in blocks if b["n_points"] > 4)
        print(f"  {len(blocks)} layout blocks, {nonrect} non-rectangular\n")
        from collections import Counter
        for k, n in Counter(b["block_type"] for b in blocks).most_common():
            print(f"    {k:16s} {n}")
        print()
        for b in blocks:
            if b["n_points"] > 4:
                print(f"    {b['block_type']:16s} "
                      f"x {b['left_pct']:6.2f}-{b['right_pct']:6.2f} "
                      f"y {b['top_pct']:6.2f}-{b['bottom_pct']:6.2f}  "
                      f"{b['n_points']} points")
    finally:
        conn.close()


# Colour-blind safe, and distinguished by weight as well as hue: the
# three TEXT variants differ only in how they relate to the column
# lattice, which is the whole point of recording them.
TYPE_COLOUR = {
    "FLOWING_TEXT": (10, 90, 200),      # lives inside one column
    "HEADING_TEXT": (0, 0, 0),          # spans more than one column
    "PULLOUT_TEXT": (224, 120, 0),      # cross-column pull-out
    "CAPTION_TEXT": (140, 60, 200),
    "FLOWING_IMAGE": (120, 180, 235),
    "PULLOUT_IMAGE": (255, 160, 60),
    "HEADING_IMAGE": (255, 160, 60),
    "TABLE": (0, 150, 140),
    "HORZ_LINE": (110, 110, 110),
    "VERT_LINE": (110, 110, 110),
}


def _cmd_render(args):
    """Draw the POLYGONS, not the bounding boxes -- the polygon is the
    thing hOCR discards, so a render that drew boxes would show nothing
    this stage adds."""
    from PIL import Image, ImageDraw
    conn = _sup.open_connection()
    try:
        y, m, d = (int(v) for v in args.date.split("-"))
        row = conn.execute("SELECT id, display_image_path FROM pages "
                           "WHERE year=? AND month=? AND day=? AND page=?",
                           (y, m, d, args.page)).fetchone()
        blocks = blocks_of(conn, row["id"])
        if not blocks:
            print("no stored layout blocks -- run `layout_blocks run` first")
            return
        im = Image.open(row["display_image_path"]).convert("RGB")
        W, H = im.size
        dr = ImageDraw.Draw(im)
        for b in blocks:
            col = TYPE_COLOUR.get(b["block_type"], (200, 30, 40))
            wide = b["block_type"] in ("HEADING_TEXT", "PULLOUT_TEXT")
            if b["polygon"]:
                pts = [(x / 100 * W, yy / 100 * H) for x, yy in b["polygon"]]
                dr.line(pts + [pts[0]], fill=col, width=5 if wide else 3)
            else:
                dr.rectangle([b["left_pct"] / 100 * W, b["top_pct"] / 100 * H,
                              b["right_pct"] / 100 * W, b["bottom_pct"] / 100 * H],
                             outline=col, width=3)
        out = os.path.join(_sup.REPO_ROOT, "preview", "scaled", "layout",
                           args.date, f"p{args.page}_layout.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        im.thumbnail((1000, 1500))
        im.save(out)
        nonrect = sum(1 for b in blocks if b["n_points"] > 4)
        print(f"  {len(blocks)} blocks, {nonrect} non-rectangular")
        print(" ", out)
    finally:
        conn.close()


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        sql = ("SELECT id, year, month, day, page FROM pages "
               "WHERE hocr_parsed_at IS NOT NULL")
        params: list = []
        if args.date:
            y, m, d = (int(x) for x in args.date.split("-"))
            sql += " AND year=? AND month=? AND day=?"
            params = [y, m, d]
        n = tot = nonrect = 0
        for r in conn.execute(sql + " ORDER BY year, month, day, page", params):
            img = _image_for(conn, r["id"])
            if not img:
                print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} "
                      f"p{r['page']}: no image on disk")
                continue
            blocks = analyse(r["id"], img)
            store(conn, r["id"], blocks)
            n += 1
            tot += len(blocks)
            nonrect += sum(1 for b in blocks if b["n_points"] > 4)
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(blocks)} blocks, "
                  f"{sum(1 for b in blocks if b['n_points'] > 4)} non-rect")
        print(f"\n{n} page(s), {tot} blocks, {nonrect} non-rectangular "
              f"({nonrect / tot * 100:.0f}%)" if tot else "")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("date")
    s.add_argument("--page", type=int, required=True)
    s.set_defaults(func=_cmd_show)
    r = sub.add_parser("run")
    r.add_argument("--date")
    r.set_defaults(func=_cmd_run)
    d = sub.add_parser("render")
    d.add_argument("date")
    d.add_argument("--page", type=int, required=True)
    d.set_defaults(func=_cmd_render)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
