"""Stage 2a — PHOTOS THAT ARE ACTUALLY ADS. Runs just before boxed zones.

Tesseract labels a display ad `ocr_photo` when the ad is mostly artwork
with type set into it. Those are boxed zones, not pictures, and stage 2b
never sees them because it derives rectangles from rule corners and an ad
of this kind often has no ruled border at all.

This stage does not reclassify anything destructively. The `ocr_photo`
record is untouched; what it emits is a zone CANDIDATE that stage 2b takes
alongside its corner-derived rectangles, tagged `source='photo'`. If a
conversion is wrong, the photo layer still shows the photo.

WHAT SEPARATES AN AD FROM A PHOTO
---------------------------------
Line count, and it separates hard. Measured against
`items.item_type='display_ad'` from the production route -- a label
produced from the page IMAGE, so separators and corners contribute nothing
to it -- over the 83 pages that carry production items:

    lines inside the photo    n     coincide with an ad (IoU>=0.5)
        0                    853              0%
        1-2                  257              1%
        3-5                   88              3%
        6-9                   30             23%
        10-19                 45             51%

Two thirds of all photos (853 of 1313) carry no text at all and are never
considered. The gate only ever looks at ~200.

THE THREE THINGS IT MUST NOT SWEEP UP
-------------------------------------
*words read out of the image itself* -- a road sign, a shop front, a
 registration plate. One or two lines, clustered in one band. Real
 example: a 120x62-cell photo whose only line reads 'fas i AR RL SA |'.
 Excluded by MIN_LINES and by the span test.

*a headline sitting over a photo* -- one line, large x_size, at the top
 edge. 'Monumental ceremony' on a 91x75-cell photo. Excluded by MIN_LINES.

*a caption inside the photo's own bbox* -- this one needed care. The
 obvious rule, "veto if stage 2c found a caption", is WRONG: measured, it
 cut recall from 74% to 51%, because stage 2c pairs ad copy as a caption
 just as readily as it pairs a real one. Captioned-ness is not a veto.
 Instead the caption's own lines are SUBTRACTED from the count, so a
 photo whose only text is its caption cannot pass.

Usage::

    python3 -m transcribe.scaled.detect_photo_ads show 2001-01-03 --page 7
    python3 -m transcribe.scaled.detect_photo_ads audit
"""

from __future__ import annotations

import argparse

from . import _support as _sup
from . import detect_captions as _captions
from . import sliver_pass as _sliver

# FREE PARAMETERS, both read off the table in the docstring rather than
# derived, and both fitted on only 47 positive examples. Treat them the
# way MIN_SIDE_CELLS is treated in ad_rectangles: a knob someone chose.
#
# MIN_LINES sits deliberately ABOVE the 1-5 band, which is where the road
# signs, the OCR noise and the overlapping headlines live (345 photos, ~3%
# of them ads). Dropping it to 3 raises recall 79% -> 85% and drops
# precision 32% -> 20%.
MIN_LINES = 6

# The text must be distributed down the photo, not sitting in one band.
# An ad fills its box; a sign or a headline occupies a strip.
MIN_SPAN = 0.5


def _lines(conn, page_id):
    return [dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, n_words, "
        "x_size, text FROM page_hocr_lines WHERE page_id=? AND n_words>0",
        (page_id,))]


def _inside(box, it):
    return (box["L"] <= (it["L"] + it["R"]) / 2 <= box["R"]
            and box["T"] <= (it["T"] + it["B"]) / 2 <= box["B"])


def classify(conn, page_id: str) -> list[dict]:
    """Every surviving photo, with a verdict and the evidence for it."""
    photos = [i for i in _sliver.survivors(conn, page_id)
              if i["kind"] == "ocr_photo"]
    if not photos:
        return []
    lines = _lines(conn, page_id)

    # Stage 2c's caption for each photo, so its lines can be discounted.
    caption_of = {}
    for pr in _captions.detect(conn, page_id)["pairs"]:
        p, c = pr["photo"], pr["caption"]
        if c:
            caption_of[(round(p["L"], 2), round(p["T"], 2))] = {
                "L": c["left_pct"], "T": c["top_pct"],
                "R": c["right_pct"], "B": c["bottom_pct"]}

    out = []
    for ph in photos:
        cap = caption_of.get((round(ph["L"], 2), round(ph["T"], 2)))
        inside = [l for l in lines if _inside(ph, l)]
        # Subtract the caption's own lines: a photo whose only text is its
        # caption is a photo.
        body = [l for l in inside if not (cap and _inside(cap, l))]
        n = len(body)
        span = ((max(l["B"] for l in body) - min(l["T"] for l in body))
                / (ph["B"] - ph["T"])) if body and ph["B"] > ph["T"] else 0.0

        if n < MIN_LINES:
            verdict, why = "photo", f"{n} body line(s), needs {MIN_LINES}"
        elif span < MIN_SPAN:
            verdict, why = "photo", f"text spans {span:.0%} of the box, needs {MIN_SPAN:.0%}"
        else:
            verdict, why = "ad", f"{n} lines spanning {span:.0%} of the box"
        out.append({"photo": ph, "verdict": verdict, "why": why,
                    "n_lines": n, "n_all_lines": len(inside),
                    "n_caption_lines": len(inside) - n,
                    "span": round(span, 2)})
    return out


def zone_candidates(conn, page_id: str) -> list[dict]:
    """Rectangles for stage 2b, in PAGE PERCENT, one per converted photo."""
    return [{"L": r["photo"]["L"], "T": r["photo"]["T"],
             "R": r["photo"]["R"], "B": r["photo"]["B"], "why": r["why"]}
            for r in classify(conn, page_id) if r["verdict"] == "ad"]


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(v) for v in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? "
                           "AND day=? AND page=?", (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        cw, chh = _sup.cell_size(conn, row["id"])
        recs = classify(conn, row["id"])
        print(f"  {len(recs)} photos, "
              f"{sum(1 for r in recs if r['verdict'] == 'ad')} converted\n")
        for r in sorted(recs, key=lambda r: r["photo"]["T"]):
            p = r["photo"]
            cap = (f" (+{r['n_caption_lines']} caption)"
                   if r["n_caption_lines"] else "")
            print(f"    {r['verdict']:6s} x {p['L']:6.2f}-{p['R']:6.2f} "
                  f"y {p['T']:6.2f}-{p['B']:6.2f}  "
                  f"{(p['R']-p['L'])/cw:5.1f}x{(p['B']-p['T'])/chh:5.1f} cells  "
                  f"{r['n_lines']:3d} lines{cap:16s} {r['why']}")
    finally:
        conn.close()


def _cmd_audit(args):
    """Agreement with the production route's display_ad label.

    Reported as a DISTRIBUTION, not a precision figure. The two boxes are
    drawn by different processes -- Tesseract's photo region and an LLM's
    item bbox -- so they differ in extent even when they describe the same
    ad, and a single IoU threshold turns that into a false negative. An
    earlier version of this audit quoted 32% precision purely because it
    demanded IoU>=0.5.
    """
    conn = _sup.open_connection()
    try:
        def iou(a, b):
            ix = max(0, min(a["R"], b["R"]) - max(a["L"], b["L"]))
            iy = max(0, min(a["B"], b["B"]) - max(a["T"], b["T"]))
            inter = ix * iy
            ua = ((a["R"] - a["L"]) * (a["B"] - a["T"])
                  + (b["R"] - b["L"]) * (b["B"] - b["T"]) - inter)
            return inter / ua if ua else 0

        band = {">=0.5": 0, "0.2-0.5": 0, "0-0.2": 0, "none": 0}
        n_conv = n_pages = 0
        for row in conn.execute(
                "SELECT id, year, month, day, page FROM pages "
                "WHERE hocr_parsed_at IS NOT NULL ORDER BY year, month, day, page"):
            has = conn.execute(
                "SELECT 1 FROM items WHERE year=? AND month=? AND day=? "
                "AND page=? LIMIT 1",
                (row["year"], row["month"], row["day"], row["page"])).fetchone()
            cands = zone_candidates(conn, row["id"])
            n_conv += len(cands)
            if cands:
                n_pages += 1
            if not has:
                continue
            ads = [{"L": x["bbox_left_pct"], "T": x["bbox_top_pct"],
                    "R": x["bbox_right_pct"], "B": x["bbox_bottom_pct"]}
                   for x in conn.execute(
                       "SELECT * FROM items WHERE year=? AND month=? AND day=? "
                       "AND page=? AND item_type='display_ad'",
                       (row["year"], row["month"], row["day"], row["page"]))]
            for c in cands:
                best = max((iou(c, a) for a in ads), default=0)
                band[">=0.5" if best >= 0.5 else "0.2-0.5" if best >= 0.2
                     else "0-0.2" if best > 0 else "none"] += 1
        print(f"{n_conv} photos converted across {n_pages} pages\n")
        print("  overlap with a production display_ad (labelled pages only):")
        for k in (">=0.5", "0.2-0.5", "0-0.2", "none"):
            print(f"    IoU {k:8s} {band[k]:4d}")
        tot = sum(band.values())
        if tot:
            print(f"\n  some overlap: {tot - band['none']} of {tot} "
                  f"({(tot - band['none']) / tot * 100:.0f}%)")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("date")
    s.add_argument("--page", type=int, required=True)
    s.set_defaults(func=_cmd_show)
    a = sub.add_parser("audit")
    a.set_defaults(func=_cmd_audit)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
