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

TWO DERIVATIONS LIVE HERE, and the newer one is not yet the stored value.
`content_box` is the original, from text LINES, and is what `store()`
writes. `content_box_blocks` is the AGREEMENT derivation, from every item
type that survives stage 1b, and both are drawn as IIIF layers under the
`content` variant so they can be compared on the page. See §5r.

The rest of this docstring describes the ORIGINAL derivation.

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
the 0.00% failures.

What the rim guard actually buys, stated precisely because an earlier
version of this docstring overclaimed: `MIN_BIN_LINES` requires a
histogram bin to hold >=2 lines before that bin can define an edge, then
takes the extreme WITHIN that bin. So the returned margin is still one
specific line's edge -- measured, it is held by a single line on 73/90
pages at the left and 71/90 at the right. What the guard bounds is the
EXCURSION: the margin can only sit within one bin width (0.25%) of a
position where at least two lines genuinely align. Two spurious >=2-word
lines landing in the same bin will still set it outright. This is a
bounded error, not immunity, and the difference matters to anyone
deciding how far to trust `content_left_pct` downstream.

Usage::

    python3 -m transcribe.scaled.detect_content_area show 1980-04-06 --page 4
    python3 -m transcribe.scaled.detect_content_area run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup
from . import sliver_pass as _sliver

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

# A bin must hold at least this many lines before it may set the margin.
# Guards the RIM of the winning cluster, where merging can drag in a lone
# outlier. Two, not more: a narrow real column can be sparse, and the
# share floor already vouches for the cluster as a whole.
MIN_BIN_LINES = 2

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

    # Within the winning cluster take the extreme, not the median: the
    # margin is where the column STARTS, and lines that begin a paragraph
    # with an indent sit to the right of it.
    #
    # But the extreme is taken only over bins holding MIN_BIN_LINES or
    # more. Merging before the floor is right for CHOOSING the cluster --
    # it is what recovers ragged-right ad text -- but it also pulls a
    # one-line outlier into the cluster if it sits within
    # CLUSTER_MERGE_PCT, and an unfiltered extreme then lets that single
    # line set the margin. Measured across 90 pages, the unfiltered form
    # made the margin equal to the single outermost line on the page on
    # 70/90 pages at the left and 56/90 at the right -- exactly what this
    # module's docstring says must not happen.
    #
    # The share floor guards the cluster; this guards its rim.
    solid = [b for b in grp if len(hist[b]) >= MIN_BIN_LINES]
    xs = [x for b in (solid or grp) for x in hist[b]]
    return round(min(xs) if leftmost else max(xs), 2)


# --- the block derivation ------------------------------------------------
#
# AN EDGE MUST BE AGREED BY AT LEAST THIS MANY ITEMS.
#
# The content edge is a position that several items SHARE, not the
# outermost thing on the page. Measured, an extreme is set by a single
# item on 45-71 of 90 pages depending on the edge -- so extremes are
# essentially unconfirmed, which is what let 1994-01-05 p12 report a right
# edge of 49.93% on a page whose text runs to 94.08%.
#
# TWO, and not more, because the required agreement must not exceed what
# the page has to offer. 1980-04-06 p3 is a photo page whose top is set by
# exactly two photos (y 2.11-23.24 and 2.45-25.92, within one cell of each
# other). At K=2 they confirm each other and the top is right at 2.11%; at
# K=3 there is no third item up there, the walk continues inward, and the
# top lands at 54.21% -- half way down the page.
MIN_AGREE = 2

# WHY AGREEMENT IS COUNTED ACROSS ALL TYPES, with no per-type rule.
# Measured, for an item of each kind, how many others share its edge
# within one cell (% of items with >=2 agreeing):
#
#     kind             left   right    top   bottom
#     ocr_carea         80%     75%    39%      41%
#     ocr_separator     73%     75%    61%      61%
#     ocr_photo         68%     73%    47%      45%
#
# Left and right agree strongly for EVERY type, because everything sits on
# the column grid. Top and bottom agree weakly for every type, because
# vertical position is not quantised -- ads are sold by the column inch,
# see 5h. No type earns exclusion, and none needs a whitelist: on a photo
# page the top is set by photos because photos are what is there.

# A margin outside this band is reported in `sanity`, never corrected.
SANE_MARGIN_CELLS = (1.0, 30.0)


def _agreed_edge(items, key, outermost_low, tol):
    """The outermost position at least MIN_AGREE items share, and the count.

    Walk in from the page edge and stop at the first position that more
    than one item agrees on. Returns (position, how many agreed) so the
    frequency travels with the value and a caller can see how well
    attested the edge is.
    """
    vals = sorted((i[key] for i in items), reverse=not outermost_low)
    for x in vals:
        n = sum(1 for y in vals if abs(y - x) <= tol)
        if n >= MIN_AGREE:
            return round(x, 2), n
    return None, 0


def content_box_blocks(conn, page_id: str) -> dict:
    """The content rectangle: the extremes of the TEXT BLOCKS.

    One rule, no tuned parameters. Take every `ocr_carea` that has words,
    drop any sitting hard against a page edge, and take the extremes.

    WHY BLOCKS AND NOT LINES. An individual line's right edge is wherever
    its last word happened to end, so right edges are RAGGED and do not
    cluster the way left edges do -- body text is set flush left, so
    hundreds of lines share an x, and nothing equivalent is true on the
    right. `_edge_cluster` assumes both edges cluster, and its four tuned
    parameters exist to cope when they do not. Measured, it still failed:
    on 1994-01-05 p12 the ragged right edges scattered into clusters that
    each fell under `MIN_CLUSTER_SHARE`, all were discarded, and the
    content right edge came back as 49.93% on a page whose text runs to
    94.08% -- half the page excluded. A BLOCK's bbox already spans its own
    ragged lines, so raggedness never has to be modelled.

    PHOTOS COUNT, IF THEY ARE BIG ENOUGH. On a photo page they are most
    of the content: 1980-04-06 p3 carries two photos spanning y 2.11-23.24
    and 2.45-25.92 above its first text block, and text blocks alone put
    the content top at 22.44% -- a fifth of the way down a page whose
    content starts at 2.11%. But photo regions are also where the binding
    shadow ends up (2001-01-03 p5: an ocr_photo at x 98.17-100.00 spanning
    y 0.02-100.00), so they are admitted only at MIN_PHOTO_CELLS on both
    axes. A real photo is a substantial rectangle and a shadow is a
    sliver.

    RULES ARE NOT USED, only validated against. A separator is thin by
    nature, so the sliver test that separates real photos from shadows
    cannot separate real rules from them.

    MEASURED against the line derivation, 90 pages:

        derivation             outside   p12 right edge   p3 top
        lines                    14.5%       49.93% wrong   22.44% wrong
        blocks, no photos         7.8%       94.08% right   22.44% wrong
        blocks + photos >=8       4.7%       94.08% right    2.11% right

    The margins come out about 2 cells tighter, which is expected rather
    than wrong: a block's bbox is at least as wide as the lines inside it.
    """
    cw, chh = _sup.cell_size(conn, page_id)
    every = _sliver.items_of(conn, page_id)
    items = _sliver.survivors(conn, page_id)
    if len(items) < MIN_AGREE:
        return {"left": None, "right": None, "top": None, "bottom": None,
                "n_items": len(items), "note": "too few items"}

    left, nl = _agreed_edge(items, "L", True, cw)
    right, nr = _agreed_edge(items, "R", False, cw)
    top, nt = _agreed_edge(items, "T", True, chh)
    bottom, nb = _agreed_edge(items, "B", False, chh)
    if None in (left, right, top, bottom):
        return {"left": left, "right": right, "top": top, "bottom": bottom,
                "n_items": len(items), "note": "an edge had no agreement"}

    outside = sum(1 for i in every if i["L"] < left - 0.01 or i["R"] > right + 0.01
                  or i["T"] < top - 0.01 or i["B"] > bottom + 0.01)
    lo, hi = SANE_MARGIN_CELLS
    sanity = [n for n, v in (("left", left / cw), ("right", (100 - right) / cw),
                             ("top", top / chh), ("bottom", (100 - bottom) / chh))
              if v < lo or v > hi]
    return {"left": left, "right": right, "top": top, "bottom": bottom,
            "width": round(right - left, 2), "height": round(bottom - top, 2),
            "agree": {"left": nl, "right": nr, "top": nt, "bottom": nb},
            "n_items": len(items), "n_all": len(every), "n_outside": outside,
            "sanity": sanity, "fell_back": []}

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
