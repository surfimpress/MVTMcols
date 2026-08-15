"""Stage 3 — HORIZONTAL alignments: where the mosaic's tiles meet.

Stage 2 fits the vertical grid. This finds the horizontal edges that cut
across it: the top and bottom of a photo, a cutoff rule between stacked
stories, the top of a headline, the edge of a boxed ad — plus the page's
own content top and bottom.

WHY THE PREVIOUS ATTEMPT FAILED, AND WHAT IS DIFFERENT
-----------------------------------------------------
`archive/detect_bands.py` cut the page into horizontal STRIPS bounded by
a page-wide rule or by a y-gap no text line crosses. It produced a band
covering 62% of a page that contained several unrelated articles, and
scored itself 0.917 while doing so.

Two things were wrong, and both are fixed here:

1. **It required page-wide extent.** Measured across the corpus, there
   are 2,226 horizontal `ocr_separator` rules but only 20 span 8+
   columns. 1,240 span a single column, 581 span two, 196 span three.
   The band approach discarded ~99% of the available evidence. On a
   post-1980 mosaic page an alignment is LOCAL by nature: columns 3-5
   break while 1-2 run on. So an alignment here carries a COLUMN SPAN
   and is never required to cross the page.

2. **It scored its own trustworthiness.** Nothing here does. Strength is
   reported as the number of distinct columns that agree, which is a
   count of evidence, not a self-assessment. See `archive/README.md`.

THE UNIT OF EVIDENCE
--------------------
A horizontal alignment is real when INDEPENDENT COLUMNS AGREE ON IT.
Strength is therefore the number of distinct columns contributing an
edge at that y — never the raw number of edges. One column full of
fragments cannot manufacture an alignment; two columns breaking at the
same y is the actual signal, and it is exactly what "a multi-column
span" means.

This is the horizontal counterpart of stage 2's discipline, but it is
NOT a lattice fit: vertical rhythm is not quantised the way column pitch
is. Ads are sold by the column INCH, so heights vary continuously. There
is no vertical pitch to fit, and pretending otherwise would repeat the
error typesetting_practice.md warns about in the other direction.

Usage::

    python3 -m transcribe.scaled.detect_hlines show 1980-04-06 --page 11
    python3 -m transcribe.scaled.detect_hlines run [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup
from . import detect_grid as _grid

# --- tuning, all with a stated reason ---------------------------------

# Two edges are the same alignment if they sit within this many text
# lines of each other. Derived per page from the median line height, not
# a fixed percentage: a page scanned larger has proportionally taller
# lines, and the tolerance must follow.
MERGE_LINES = 0.75

# An alignment must be agreed by at least this many distinct columns.
# The user's framing is "multi-column spans", so a single column's own
# block boundary is not an alignment -- it is just a block edge.
MIN_COLUMNS = 2

# Evidence weight by kind. Same discipline as stage 2: a printed rule or
# a photo edge is real but looser than the mass of text.  A horizontal
# rule is a deliberate cutoff between stories, so unlike the vertical
# case it is NOT downweighted -- it is the most direct evidence there is.
WEIGHT = {
    "rule": 1.0,        # ocr_separator, horizontal -- a printed cutoff
    "photo": 0.75,      # ocr_photo top/bottom -- placed, but loosely
    "header": 1.0,      # ocr_header line top -- Tesseract's own class
    "block": 0.5,       # ordinary block top/bottom -- numerous, weakest
}

# A rule narrower than this is furniture inside one item (a table rule, a
# coupon's dashes), not a structural divider.
MIN_RULE_WIDTH_PCT = 2.0

# Content extent: ignore items in the outer margins of the page. Scan
# artefacts (sheet edge, binding shadow) cluster there and would push the
# content line off the actual text. Same rationale as stage 2's
# EDGE_MARGIN_PCT, applied to the other axis.
CONTENT_MARGIN_PCT = 1.5

# A line must carry at least this many words to define the content edge.
# MEASURED: 1997-07-16 p4 reported a content top of 0.46% from a single
# one-word line reading '"a' at the sheet edge; the real top is 2.42%
# ("OPINION"). An isolated one-word line at the page margin is
# overwhelmingly scan noise, and the content line is an EXTREME, so a
# single artefact moves it -- unlike a median, which would absorb it.
MIN_CONTENT_WORDS = 2


def column_span(lo_pct: float, hi_pct: float,
                cols: list[dict]) -> tuple[int, int] | None:
    """Which contiguous run of columns an x-range covers.

    A column counts as covered when the range overlaps the majority of
    it, so a rule that overshoots slightly into the gutter does not claim
    a neighbour it never reached.
    """
    hit = []
    for c in cols:
        w = c["right_pct"] - c["left_pct"]
        if w <= 0:
            continue
        ov = min(hi_pct, c["right_pct"]) - max(lo_pct, c["left_pct"])
        if ov > w * 0.5:
            hit.append(c["col_idx"])
    if not hit:
        return None
    return min(hit), max(hit)


def candidates(conn, page_id: str, cols: list[dict]) -> list[dict]:
    """Every horizontal edge on the page, tagged with kind and columns.

    Both the TOP and BOTTOM of an extended item are emitted: a photo's
    top starts a section and its bottom ends one, and they are
    independent alignments.
    """
    out = []

    def add(y, x0, x1, kind):
        span = column_span(x0, x1, cols)
        if span is None:
            return
        out.append({"y": round(y, 2), "lo": span[0], "hi": span[1],
                    "kind": kind, "w": WEIGHT[kind]})

    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, region_class, "
        "orientation FROM page_hocr_regions WHERE page_id=?", (page_id,),
    ):
        if r["region_class"] == "ocr_separator":
            if r["orientation"] != "horizontal":
                continue
            if r["R"] - r["L"] < MIN_RULE_WIDTH_PCT:
                continue
            # A rule has thickness; its centre is the alignment.
            add((r["T"] + r["B"]) / 2, r["L"], r["R"], "rule")
        elif r["region_class"] == "ocr_photo":
            add(r["T"], r["L"], r["R"], "photo")
            add(r["B"], r["L"], r["R"], "photo")

    # A heading's TOP is where its section begins. Its bottom is just the
    # gap before the body and carries no structural meaning, so only the
    # top is emitted.
    for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R FROM page_hocr_lines "
        "WHERE page_id=? AND line_class='ocr_header'", (page_id,),
    ):
        add(r["T"], r["L"], r["R"], "header")

    for b in _grid.page_blocks(conn, page_id):
        add(b["T"], b["L"], b["R"], "block")
        add(b["B"], b["L"], b["R"], "block")

    return out


def cluster(cands: list[dict], tol: float) -> list[dict]:
    """Group edges into alignments by y, then measure column agreement.

    Strength is `n_columns` -- how many DISTINCT columns contributed --
    deliberately not the edge count. A column that Tesseract fragmented
    into twenty blocks must not out-vote two columns that genuinely break
    at the same height.
    """
    if not cands:
        return []
    out = []
    run = [cands[0]]
    for c in sorted(cands, key=lambda x: x["y"])[1:]:
        if c["y"] - run[-1]["y"] <= tol:
            run.append(c)
        else:
            out.append(run)
            run = [c]
    out.append(run)

    aligns = []
    for grp in out:
        colset = set()
        for c in grp:
            colset.update(range(c["lo"], c["hi"] + 1))
        if len(colset) < MIN_COLUMNS:
            continue
        # Weight-biased y, so a printed rule pins the alignment rather
        # than being averaged away by the block edges around it.
        tw = sum(c["w"] for c in grp)
        y = sum(c["y"] * c["w"] for c in grp) / tw
        kinds = sorted({c["kind"] for c in grp})
        aligns.append({
            "y_pct": round(y, 2),
            "col_lo": min(colset), "col_hi": max(colset),
            "n_columns": len(colset), "n_edges": len(grp),
            "weight": round(tw, 2), "kinds": ",".join(kinds),
            "has_rule": any(c["kind"] == "rule" for c in grp),
        })
    return sorted(aligns, key=lambda a: a["y_pct"])


def content_extent(conn, page_id: str) -> tuple[float | None, float | None]:
    """The page's content top and bottom lines.

    Taken from TEXT LINES, not blocks: a block bbox can be inflated by a
    scan artefact swept into it, while a line is a real run of recognised
    words. Items in the outer margins are ignored for the same reason
    stage 2 ignores page-edge separators.
    """
    ys = [(r["T"], r["B"]) for r in conn.execute(
        "SELECT top_pct T, bottom_pct B, left_pct L, right_pct R "
        "FROM page_hocr_lines WHERE page_id=? AND n_words >= ?",
        (page_id, MIN_CONTENT_WORDS))
        if r["L"] >= CONTENT_MARGIN_PCT and r["R"] <= 100 - CONTENT_MARGIN_PCT]
    if not ys:
        return None, None
    return round(min(t for t, _ in ys), 2), round(max(b for _, b in ys), 2)


def detect(conn, page_id: str) -> dict:
    grid = _grid.detect(conn, page_id)
    cols = grid.get("columns") or []
    if not cols:
        return {"alignments": [], "note": "no column grid", "grid": None}

    line_h = _grid.median_line_height(conn, page_id)
    tol = max(0.2, line_h * MERGE_LINES)
    aligns = cluster(candidates(conn, page_id, cols), tol)
    top, bottom = content_extent(conn, page_id)

    return {"grid": grid.get("grid"), "n_columns": len(cols),
            "alignments": aligns, "content_top": top, "content_bottom": bottom,
            "tol_pct": round(tol, 2),
            "low_evidence": grid.get("low_evidence", False)}


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_hlines WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for a in res.get("alignments", []):
        conn.execute(
            """INSERT INTO page_hlines
               (id, page_id, y_pct, col_lo, col_hi, n_columns, n_edges,
                weight, kinds, has_rule, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, a["y_pct"], a["col_lo"], a["col_hi"],
             a["n_columns"], a["n_edges"], a["weight"], a["kinds"],
             1 if a["has_rule"] else 0, now))
    conn.execute(
        "UPDATE pages SET content_top_pct=?, content_bottom_pct=? WHERE id=?",
        (res.get("content_top"), res.get("content_bottom"), page_id))
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
        if not res["alignments"] and res.get("note"):
            print("  ", res["note"])
            return
        print(f"  columns      : {res['n_columns']}")
        print(f"  content top  : {res['content_top']}%")
        print(f"  content btm  : {res['content_bottom']}%")
        print(f"  merge tol    : {res['tol_pct']}% (0.75 text lines)")
        print(f"  alignments   : {len(res['alignments'])}"
              + ("   [LOW EVIDENCE PAGE]" if res["low_evidence"] else ""))
        print(f"\n  {'y':>7}  {'columns':>9}  {'cols':>4} {'edges':>5}  kinds")
        for a in res["alignments"]:
            span = (f"{a['col_lo']}-{a['col_hi']}" if a["col_lo"] != a["col_hi"]
                    else str(a["col_lo"]))
            star = " *" if a["has_rule"] else "  "
            print(f"  {a['y_pct']:6.2f}%  {span:>9}  {a['n_columns']:4d} "
                  f"{a['n_edges']:5d}{star} {a['kinds']}")
        print("\n  * = a printed horizontal rule contributed")
    finally:
        conn.close()


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = _grid.pages_to_run(conn, args.date)
        n = 0
        for r in rows:
            res = detect(conn, r["id"])
            if not res["alignments"]:
                continue
            store(conn, r["id"], res)
            n += 1
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(res['alignments'])} alignments  "
                  f"content {res['content_top']}%-{res['content_bottom']}%")
        print(f"\n{n} page(s).")
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
