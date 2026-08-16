"""Stage 1c — the PAGE CONTENT AREA. Runs before columns.

Everything downstream is measured from this rectangle, so it has to be
right before anything else can be. It exists as its own step for exactly
that reason: the column fitter was deriving its own left and right bounds
from the extremes of the block edge distribution, and a single scan
artefact at the sheet edge was enough to anchor the whole lattice to the
physical page edge instead of the text.

MEASURED, the failure this fixes (90 pages):

    grid text_left was 0.00% on many pages, up to 7.2% left of where
    text actually starts -- 1980-04-06 p4: text_left 0.00%, real content
    left 7.22%. Every column on those pages was displaced.
    |error| > 1.5% on 31% of pages at the left edge and 53% at the right.

WHY LINES, NOT BLOCKS
---------------------
A block bbox can be inflated by a scan artefact swept into it -- a
binding shadow, a torn sheet edge, a speck of dust the binariser kept. A
text LINE with two or more recognised words is a real run of type. The
two-word minimum is not arbitrary: 1997-07-16 p4 reported a content top
of 0.46% from a single one-word line reading '"a' at the sheet edge,
where the real top is 2.42% ("OPINION").

WHY THE EDGES ARE FOUND DIFFERENTLY FROM TOP AND BOTTOM
-------------------------------------------------------
Top and bottom are EXTREMES: the first and last line of type. Nothing
above the first line and nothing below the last, so the extreme is the
answer once artefacts are excluded.

Left and right are not extremes -- they are CLUSTERS. Body text is set
flush left in every column, so hundreds of lines start at the same x, and
the content's left edge is the leftmost position that a meaningful number
of lines actually start at. Taking the minimum instead is what produced
the 0.00% failures. A hanging indent, a stray overhanging headline or one
mis-segmented line must not be able to move the margin.

Usage::

    python3 -m transcribe.scaled.detect_content_area show 1980-04-06 --page 4
    python3 -m transcribe.scaled.detect_content_area run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup

# A line must carry at least this many words to count as content. See the
# 1997-07-16 p4 case in the module docstring.
MIN_WORDS = 2

# Histogram bin for the left/right edge clusters, in page percent. Matches
# detect_grid's own binning so the two stages describe the page at the
# same resolution.
BIN_PCT = 0.25

# A cluster must hold at least this share of all line-edge mass to be
# treated as a real margin. Low on purpose: the outermost column can be a
# narrow ad column with few lines, and it is still the content edge.
# Raising this walks the margin inward past sparse real columns.
MIN_CLUSTER_SHARE = 0.015

# Adjacent bins within this distance are one cluster -- absorbs scan skew
# across the page height, which smears a single printed margin over
# several bins.
CLUSTER_MERGE_PCT = 0.6

# Nothing outside this is content: it is sheet edge, binding shadow or
# scanner backing. Deliberately generous -- this is a sanity bound, not
# the measurement.
HARD_MARGIN_PCT = 0.5


def _lines(conn, page_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
        "FROM page_hocr_lines WHERE page_id=? AND n_words >= ? "
        "AND right_pct > left_pct", (page_id, MIN_WORDS))]


def _edge_cluster(vals: list[float], leftmost: bool) -> float | None:
    """The outermost x at which a meaningful number of lines align.

    Not the minimum/maximum: that is a single line, and one artefact
    then sets the margin. Bins are merged into clusters, clusters below
    MIN_CLUSTER_SHARE are discarded, and the outermost survivor wins.
    """
    if not vals:
        return None
    hist: dict[int, list[float]] = {}
    for v in vals:
        hist.setdefault(int(v / BIN_PCT), []).append(v)
    total = len(vals)

    # MERGE FIRST, then apply the share floor to the whole cluster.
    #
    # The reverse order threw away real content. Filtering bins
    # individually favours JUSTIFIED text, where every line ends at the
    # same x and one bin holds them all, and penalises RAGGED-RIGHT text,
    # where the same number of lines is spread over dozens of bins and no
    # single one clears the floor. Ads are set ragged right.
    #
    # 1980-04-06 p9: 100 of 297 lines end beyond x=60%, a third of the
    # page, spread across ~48 bins. Every bin failed, so the content right
    # edge came back as 61.42% -- the justified editorial column -- and the
    # whole right-hand run of ads fell outside the content area and was
    # discarded downstream.
    bins = sorted(hist)
    groups, run = [], [bins[0]]
    for b in bins[1:]:
        if (b - run[-1]) * BIN_PCT <= CLUSTER_MERGE_PCT:
            run.append(b)
        else:
            groups.append(run)
            run = [b]
    groups.append(run)
    groups = [g for g in groups
              if sum(len(hist[b]) for b in g) >= total * MIN_CLUSTER_SHARE]
    if not groups:
        return None

    grp = groups[0] if leftmost else groups[-1]
    xs = [x for b in grp for x in hist[b]]
    # Within the winning cluster take the extreme, not the median: the
    # margin is where the column STARTS, and lines that begin a paragraph
    # with an indent sit to the right of it.
    return round(min(xs) if leftmost else max(xs), 2)


def content_box(conn, page_id: str) -> dict:
    """The page's content rectangle, plus what it was derived from."""
    lines = [l for l in _lines(conn, page_id)
             if l["L"] >= HARD_MARGIN_PCT and l["R"] <= 100 - HARD_MARGIN_PCT]
    if len(lines) < 10:
        return {"left": None, "right": None, "top": None, "bottom": None,
                "n_lines": len(lines), "note": "too few text lines"}

    left = _edge_cluster([l["L"] for l in lines], leftmost=True)
    right = _edge_cluster([l["R"] for l in lines], leftmost=False)

    # Top and bottom ARE extremes -- see the module docstring.
    top = round(min(l["T"] for l in lines), 2)
    bottom = round(max(l["B"] for l in lines), 2)

    # Fall back to the extremes if no cluster cleared the share floor,
    # rather than returning nothing: a poor bound still beats none, and
    # the caller can see which happened.
    fell_back = []
    if left is None:
        left = round(min(l["L"] for l in lines), 2)
        fell_back.append("left")
    if right is None:
        right = round(max(l["R"] for l in lines), 2)
        fell_back.append("right")

    return {"left": left, "right": right, "top": top, "bottom": bottom,
            "width": round(right - left, 2), "height": round(bottom - top, 2),
            "n_lines": len(lines), "fell_back": fell_back}


def store(conn, page_id: str, box: dict) -> None:
    conn.execute(
        "UPDATE pages SET content_left_pct=?, content_right_pct=?, "
        "content_top_pct=?, content_bottom_pct=? WHERE id=?",
        (box.get("left"), box.get("right"), box.get("top"), box.get("bottom"),
         page_id))
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
        b = content_box(conn, row["id"])
        if b.get("note"):
            print("  ", b["note"])
            return
        print(f"  content left  : {b['left']}%")
        print(f"  content right : {b['right']}%   (width {b['width']}%)")
        print(f"  content top   : {b['top']}%")
        print(f"  content bottom: {b['bottom']}%   (height {b['height']}%)")
        print(f"  from {b['n_lines']} text lines of >= {MIN_WORDS} words")
        if b["fell_back"]:
            print(f"  NOTE: fell back to extremes for {', '.join(b['fell_back'])}"
                  " -- no cluster cleared the share floor")
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
        n = 0
        widths = []
        for r in conn.execute(sql + " ORDER BY year, month, day, page", params):
            b = content_box(conn, r["id"])
            if b.get("left") is None:
                print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} "
                      f"p{r['page']}: {b.get('note')}")
                continue
            store(conn, r["id"], b)
            widths.append(b["width"])
            n += 1
        print(f"\n{n} page(s).")
        if widths:
            print(f"content width: median {statistics.median(widths):.2f}%  "
                  f"min {min(widths):.2f}%  max {max(widths):.2f}%")
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
