"""Stage 2c — attach CAPTIONS to their photos.

A photo and its caption are one editorial unit. This finds the caption
strip beneath each photo and records the pair.

WHY NOT JUST USE TESSERACT'S ocr_caption CLASS
-----------------------------------------------
Because it is not reliable enough to build on. MEASURED on 1980-04-06:

  * Only 5-7 `ocr_caption` lines per page, against far more real caption
    lines than that.
  * The SAME caption block is split across classes. The two-column
    caption under p1's main photo has its right column tagged
    `ocr_caption` and its left column tagged `ocr_textfloat` and
    `ocr_header` -- one block, three classes.
  * It fires on things that are not captions at all: p7's page headline
    "Almonte celebrates IOOth anniversary of town" is tagged
    `ocr_caption`.

So `ocr_caption` is used as CORROBORATION (recorded in `kinds`), never as
the test. Geometry decides.

THE MODEL
---------
A caption is the strip DIRECTLY BENEATH a photo, confined to the photo's
own x-extent, running down until something ends it. That one description
covers both shapes seen on these pages:

  1980-04-06 p1   the caption under the main photo is set in TWO columns
                  that do NOT follow the page grid, and is closed by a
                  horizontal rule at y 68.44 spanning x 5.0-65.5 -- which
                  matches the photo's own extent (5.1-65.3) almost
                  exactly. That rule is the terminator.

  1980-04-06 p3   a photo feature page: captions simply run beneath each
                  photo and above the next one. The next photo is the
                  terminator.

Because the caption may be set in its own columns, the unit stored is the
STRIP, not a column: sub-columns within it are reported as `n_runs` but
do not split the caption into separate records. A caption set in two legs
is still one caption.

Usage::

    python3 -m transcribe.scaled.detect_captions show 1980-04-06 --page 1
    python3 -m transcribe.scaled.detect_captions run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse

from . import _support as _sup
from . import detect_grid as _grid

# --- which ocr_photo regions are real photos --------------------------
# Tesseract reports the sheet edge and binding shadow as ocr_photo. On
# 1980-04-06 p1 alone: a 0.3%-wide sliver at x 99.7-100.0 and a 1.4%-wide
# strip at x 0.0-1.4 running half the page height. Same class of artefact
# the vertical-rule filter already excludes, on the same reasoning.
PHOTO_MIN_WIDTH_PCT = 4.0
PHOTO_MIN_HEIGHT_PCT = 2.0
PHOTO_EDGE_MARGIN_PCT = 2.0

# A line belongs to the caption if this much of it sits within the
# photo's x-extent. Generous: a caption often overhangs its photo very
# slightly, and Tesseract's photo bbox is only approximate.
MIN_X_OVERLAP = 0.6

# How far below the photo the caption may start, and how large a gap may
# appear inside it, both in text lines. A caption sits tight under its
# photo; a gap of several lines means the next story has begun.
MAX_START_GAP_LINES = 4.0
MAX_INNER_GAP_LINES = 1.8

# A caption runs to a horizontal rule whose extent matches the photo's.
# Matching matters: a rule that spans the whole page belongs to the page,
# not to this photo.
RULE_EXTENT_TOL_PCT = 3.0

# Runs separated by more than this are separate legs of a multi-column
# caption -- reported, not split.
RUN_GAP_PCT = 1.5

# A caption line may not extend beyond its photo's edges by more than
# this. Overlap alone was not enough: on 1980-04-06 p3 a wide line
# running from x 41.3 into the neighbouring column still cleared the 60%
# overlap test against a photo starting at 51.5, and dragged the caption
# box left across the gutter.
MAX_OVERHANG_PCT = 2.5

# A caption must carry at least this many words in total. Guards against
# a stray fragment being adopted as a caption -- 1980-04-06 p7 produced a
# one-line "caption" reading 'oi'.
MIN_CAPTION_WORDS = 4


def real_photos(conn, page_id: str) -> list[dict]:
    """ocr_photo regions that are actually photographs."""
    out = []
    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_photo' "
        "ORDER BY top_pct", (page_id,),
    ):
        w, h = r["R"] - r["L"], r["B"] - r["T"]
        cx = (r["L"] + r["R"]) / 2
        if w < PHOTO_MIN_WIDTH_PCT or h < PHOTO_MIN_HEIGHT_PCT:
            continue
        if cx < PHOTO_EDGE_MARGIN_PCT or cx > 100 - PHOTO_EDGE_MARGIN_PCT:
            continue
        out.append({"L": r["L"], "T": r["T"], "R": r["R"], "B": r["B"]})

    # Drop any photo wholly inside another. Tesseract reports both a
    # whole halftone block and its constituent panels, and on 1980-04-06
    # p7 the nested pair (x 4.5-38.2 inside x 4.9-95.7) was handed the
    # SAME caption twice -- one caption cannot belong to two photos.
    # The container is kept: it is the region the caption sits under.
    keep = []
    for a in out:
        inside = any(b is not a
                     and b["L"] <= a["L"] + 0.5 and b["R"] >= a["R"] - 0.5
                     and b["T"] <= a["T"] + 0.5 and b["B"] >= a["B"] - 0.5
                     for b in out)
        if not inside:
            keep.append(a)
    return keep


def _terminator(conn, page_id: str, photo: dict, photos: list[dict]) -> float:
    """The y at which this photo's caption must stop.

    Whichever comes first below the photo:
      * a horizontal rule whose x-extent matches the photo's (p1's case)
      * the top of the next photo that overlaps this one in x (p3's case)
      * the bottom of the page
    """
    stop = 100.0
    for r in conn.execute(
        "SELECT left_pct L, right_pct R, top_pct T, bottom_pct B "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_separator' "
        "AND orientation='horizontal'", (page_id,),
    ):
        y = (r["T"] + r["B"]) / 2
        if y <= photo["B"]:
            continue
        if (abs(r["L"] - photo["L"]) <= RULE_EXTENT_TOL_PCT
                and abs(r["R"] - photo["R"]) <= RULE_EXTENT_TOL_PCT):
            stop = min(stop, y)

    for p in photos:
        if p is photo or p["T"] <= photo["B"]:
            continue
        ov = min(p["R"], photo["R"]) - max(p["L"], photo["L"])
        if ov > 0:
            stop = min(stop, p["T"])
    return stop


def _runs(lines: list[dict]) -> int:
    """How many horizontal legs the caption is set in.

    A caption under a wide photo is often set in two or three legs that
    do NOT follow the page grid (1980-04-06 p1). Counted and reported;
    it does not split the caption.
    """
    if not lines:
        return 0
    xs = sorted((l["L"], l["R"]) for l in lines)
    runs, cur = 1, xs[0][1]
    for lo, hi in xs[1:]:
        if lo - cur > RUN_GAP_PCT:
            runs += 1
        cur = max(cur, hi)
    return runs


def caption_for(conn, page_id: str, photo: dict, photos: list[dict],
                line_h: float) -> dict | None:
    stop = _terminator(conn, page_id, photo, photos)
    pw = photo["R"] - photo["L"]
    if pw <= 0:
        return None

    cands = []
    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, line_class, "
        "x_size, text FROM page_hocr_lines WHERE page_id=? AND n_words > 0 "
        "AND top_pct >= ? AND top_pct < ? ORDER BY top_pct",
        (page_id, photo["B"] - line_h, stop),
    ):
        lw = r["R"] - r["L"]
        if lw <= 0:
            continue
        ov = min(r["R"], photo["R"]) - max(r["L"], photo["L"])
        if ov / lw < MIN_X_OVERLAP:
            continue
        # ...and it must not spill past the photo's edges. See
        # MAX_OVERHANG_PCT.
        if (photo["L"] - r["L"] > MAX_OVERHANG_PCT
                or r["R"] - photo["R"] > MAX_OVERHANG_PCT):
            continue
        cands.append(dict(r))
    if not cands:
        return None

    # Walk down from the photo, stopping at the first gap too large to be
    # inside one caption. Without this a caption would swallow the body
    # text beneath it whenever no rule or photo intervened.
    kept = []
    prev = photo["B"]
    for c in cands:
        gap = c["T"] - prev
        limit = MAX_START_GAP_LINES if not kept else MAX_INNER_GAP_LINES
        if gap > line_h * limit:
            break
        kept.append(c)
        prev = max(prev, c["B"])
    if not kept:
        return None

    if sum(len((c["text"] or "").split()) for c in kept) < MIN_CAPTION_WORDS:
        return None

    kinds = sorted({c["line_class"] for c in kept})
    return {
        "left_pct": round(min(c["L"] for c in kept), 2),
        "right_pct": round(max(c["R"] for c in kept), 2),
        # Clamp to the photo: a caption starts where the photo ends. The
        # search begins a line early to tolerate bbox jitter, which would
        # otherwise report a caption starting above its own photo.
        "top_pct": round(max(photo["B"], min(c["T"] for c in kept)), 2),
        "bottom_pct": round(max(c["B"] for c in kept), 2),
        "n_lines": len(kept),
        "n_runs": _runs(kept),
        "kinds": ",".join(kinds),
        "tesseract_caption": int(any(c["line_class"] == "ocr_caption"
                                     for c in kept)),
        "text": " ".join((c["text"] or "").strip() for c in kept).strip(),
    }


def detect(conn, page_id: str) -> dict:
    photos = real_photos(conn, page_id)
    line_h = _grid.median_line_height(conn, page_id)
    pairs = []
    for p in photos:
        cap = caption_for(conn, page_id, p, photos, line_h)
        pairs.append({
            "photo": {k: round(v, 2) for k, v in p.items()},
            "caption": cap,
        })
    return {"pairs": pairs, "n_photos": len(photos),
            "n_captioned": sum(1 for x in pairs if x["caption"])}


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_photo_captions WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for i, pr in enumerate(res["pairs"]):
        p, c = pr["photo"], pr["caption"]
        conn.execute(
            """INSERT INTO page_photo_captions
               (id, page_id, idx, photo_left_pct, photo_top_pct,
                photo_right_pct, photo_bottom_pct, caption_left_pct,
                caption_top_pct, caption_right_pct, caption_bottom_pct,
                n_lines, n_runs, kinds, tesseract_caption, caption_text,
                created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, i, p["L"], p["T"], p["R"], p["B"],
             c and c["left_pct"], c and c["top_pct"], c and c["right_pct"],
             c and c["bottom_pct"], c and c["n_lines"], c and c["n_runs"],
             c and c["kinds"], c and c["tesseract_caption"],
             c and c["text"][:2000], now))
    conn.commit()


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? AND day=? "
                           "AND page=?", (y, m, d, args.page)).fetchone()
        if not row:
            print("no such page")
            return
        res = detect(conn, row["id"])
        print(f"  {res['n_photos']} real photo(s), "
              f"{res['n_captioned']} with a caption\n")
        for i, pr in enumerate(res["pairs"]):
            p, c = pr["photo"], pr["caption"]
            print(f"  [{i}] PHOTO   x {p['L']:5.1f}-{p['R']:5.1f}  "
                  f"y {p['T']:5.1f}-{p['B']:5.1f}")
            if not c:
                print("      CAPTION  none found")
                continue
            print(f"      CAPTION x {c['left_pct']:5.1f}-{c['right_pct']:5.1f}  "
                  f"y {c['top_pct']:5.1f}-{c['bottom_pct']:5.1f}  "
                  f"{c['n_lines']} lines, {c['n_runs']} run(s)"
                  + ("  [ocr_caption]" if c["tesseract_caption"] else ""))
            print(f"      {c['text'][:96]!r}")
    finally:
        conn.close()


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = _grid.pages_to_run(conn, args.date)
        np = nc = 0
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            np += res["n_photos"]
            nc += res["n_captioned"]
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{res['n_photos']} photos, {res['n_captioned']} captioned")
        print(f"\n{np} photos, {nc} captioned"
              + (f" ({nc / np:.0%})" if np else ""))
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
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
