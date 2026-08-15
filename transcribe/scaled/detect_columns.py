"""Stage 2: column detection from hOCR geometry alone -- no pixels, no LLM.

Three *independently derived* signals, deliberately kept separate so
their disagreement is measurable rather than averaged away:

  1. `separator`  -- tall vertical `ocr_separator` regions. Tesseract
     already found the printed column rules; a vertical rule IS a column
     boundary. Strongest evidence when present.
  2. `leftedge`   -- 1-D clustering of body-line left edges. Newspaper
     body text is flush-left within its column, so left edges pile up at
     column starts. Always available, even with no printed rules.
  3. `valley`     -- gaps in x-axis coverage of line boxes. A gutter is a
     vertical strip no line crosses.

Why three: measured on the 90 parsed pages, **22 have no interior
vertical rule at all**, so a separator-only detector would simply fail
on a quarter of the corpus. Conversely `leftedge` alone can't tell a
column gutter from an indented paragraph. Agreement between independent
derivations is also the only honest confidence signal available --
`post1980_layout_observations.md` records that this project has already
been burned once by quality flags authored by the code they graded.

Thresholds below were measured on the real corpus, not chosen a priori:
see `instructions/scaled_pipeline.md` for the distributions.

Usage::

    python3 -m transcribe.scaled.detect_columns run                 # all parsed pages
    python3 -m transcribe.scaled.detect_columns run --date 1990-10-10
    python3 -m transcribe.scaled.detect_columns show 1990-10-10 --page 2
    python3 -m transcribe.scaled.detect_columns report              # escalation rate
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup

# --- measured thresholds (see module docstring) ----------------------

# A vertical separator must be at least this tall (% of page height) to
# count as a column rule. Measured: the two genuine column rules on
# 1990-10-10 p2 are 29.7% tall; the median separator overall is 11.3%
# (mostly short box edges and underlines).
MIN_RULE_HEIGHT_PCT = 25.0

# Rules this close to either page edge are scan/page-edge artefacts, not
# column boundaries. Measured: ~27% of tall vertical rules sit here.
EDGE_MARGIN_PCT = 2.0
EDGE_MARGIN_RIGHT_PCT = 97.0

# Two boundaries closer than this fraction of the median column width
# are the same boundary found twice. The classical pipeline hard-codes
# 7% of page width; detection_methods_review.md §12 flags that as
# era-wrong (it assumes ~10% pitch) and recommends a relative measure,
# which is what this is.
MERGE_FRACTION = 0.5

# A column narrower than this fraction of the median is a sliver, not a
# column -- usually a margin strip picked up by an edge artefact.
MIN_COL_FRACTION = 0.35

# Body lines only: lines whose x_size is wildly off the page median are
# headlines or floats, which span columns and would smear the clustering.
BODY_XSIZE_TOLERANCE = 1.6

# Confidence at or above this is accepted without an LLM. Below it, the
# page is flagged for escalation. Deliberately a named constant: the
# fraction of pages landing below it is the headline result of the
# whole experiment.
CONFIDENCE_GATE = 0.60

# How close two boundaries from different signals must be (% of page
# width) to count as agreeing.
AGREEMENT_TOL_PCT = 2.5


# --- signals ---------------------------------------------------------

def separator_boundaries(conn, page_id: str) -> list[float]:
    """Column boundaries from tall interior vertical rules."""
    rows = conn.execute(
        """SELECT left_pct, right_pct, top_pct, bottom_pct
             FROM page_hocr_regions
            WHERE page_id=? AND region_class='ocr_separator'
              AND orientation='vertical'""",
        (page_id,),
    ).fetchall()
    out = []
    for r in rows:
        if (r["bottom_pct"] - r["top_pct"]) < MIN_RULE_HEIGHT_PCT:
            continue
        centre = (r["left_pct"] + r["right_pct"]) / 2
        if centre < EDGE_MARGIN_PCT or centre > EDGE_MARGIN_RIGHT_PCT:
            continue
        out.append(round(centre, 2))
    return sorted(out)


def _body_lines(conn, page_id: str) -> list[dict]:
    """Ordinary body lines, excluding headings/floats and outliers by
    x_size -- those span columns and would blur every signal."""
    rows = [dict(r) for r in conn.execute(
        """SELECT left_pct, right_pct, x_size FROM page_hocr_lines
            WHERE page_id=? AND line_class='ocr_line' AND x_size IS NOT NULL""",
        (page_id,),
    )]
    if not rows:
        return []
    med = statistics.median([r["x_size"] for r in rows])
    lo, hi = med / BODY_XSIZE_TOLERANCE, med * BODY_XSIZE_TOLERANCE
    return [r for r in rows if lo <= r["x_size"] <= hi]


def leftedge_boundaries(conn, page_id: str, bin_pct: float = 1.0) -> list[float]:
    """Column *starts* from clustered body-line left edges, converted to
    boundaries by taking the midpoint between adjacent column starts.

    A boundary is reported between two adjacent clusters, not at a
    cluster itself: the cluster marks where a column's text begins, so
    the gutter lies between one column's start and the previous column's
    right extent.
    """
    lines = _body_lines(conn, page_id)
    if len(lines) < 8:
        return []

    hist: dict[int, int] = {}
    for l in lines:
        hist[int(l["left_pct"] // bin_pct)] = hist.get(int(l["left_pct"] // bin_pct), 0) + 1
    if not hist:
        return []

    # A cluster is a bin holding a meaningful share of lines. 4% of the
    # page's body lines is low enough to catch a short column but high
    # enough to reject stray indents.
    floor = max(2, int(len(lines) * 0.04))
    peaks = sorted(b for b, n in hist.items() if n >= floor)
    if len(peaks) < 2:
        return []

    # Collapse runs of adjacent bins into one cluster (a column's left
    # edge jitters by a pixel or two across lines).
    clusters, run = [], [peaks[0]]
    for b in peaks[1:]:
        if b - run[-1] <= 1:
            run.append(b)
        else:
            clusters.append(run)
            run = [b]
    clusters.append(run)

    starts = []
    for run in clusters:
        weight = sum(hist[b] for b in run)
        centre = sum(b * hist[b] for b in run) / weight * bin_pct
        starts.append(centre)

    # Right extent of the lines belonging to each column start.
    bounds = []
    for i in range(len(starts) - 1):
        lo, hi = starts[i], starts[i + 1]
        rights = [l["right_pct"] for l in lines if lo - bin_pct <= l["left_pct"] < hi]
        gutter_left = max(rights) if rights else lo
        bounds.append(round((min(gutter_left, hi) + hi) / 2, 2))
    return sorted(bounds)


def valley_boundaries(conn, page_id: str, bin_pct: float = 0.5) -> list[float]:
    """Boundaries at gaps in x-axis coverage: strips no body line crosses."""
    lines = _body_lines(conn, page_id)
    if len(lines) < 8:
        return []
    nbins = int(100 / bin_pct) + 1
    cover = [0] * nbins
    for l in lines:
        a, b = int(l["left_pct"] // bin_pct), int(l["right_pct"] // bin_pct)
        for i in range(max(0, a), min(nbins - 1, b) + 1):
            cover[i] += 1

    text_bins = [i for i, c in enumerate(cover) if c > 0]
    if not text_bins:
        return []
    lo, hi = text_bins[0], text_bins[-1]

    out, run = [], []
    for i in range(lo, hi + 1):
        if cover[i] == 0:
            run.append(i)
        elif run:
            # Ignore hairline gaps -- a real gutter is at least ~1% wide.
            if len(run) * bin_pct >= 1.0:
                out.append(round((run[0] + run[-1]) / 2 * bin_pct, 2))
            run = []
    return sorted(out)


# --- combine ---------------------------------------------------------

def _merge_close(vals: list[float], min_gap: float) -> list[float]:
    if not vals:
        return []
    out, run = [], [vals[0]]
    for v in vals[1:]:
        if v - run[-1] <= min_gap:
            run.append(v)
        else:
            out.append(round(sum(run) / len(run), 2))
            run = [v]
    out.append(round(sum(run) / len(run), 2))
    return out


def _text_extent(conn, page_id: str) -> tuple[float, float]:
    lines = _body_lines(conn, page_id)
    if not lines:
        return 0.0, 100.0
    return (round(min(l["left_pct"] for l in lines), 2),
            round(max(l["right_pct"] for l in lines), 2))


def detect(conn, page_id: str) -> dict:
    """Run all three signals, combine, and score confidence."""
    sig = {
        "separator": separator_boundaries(conn, page_id),
        "leftedge": leftedge_boundaries(conn, page_id),
        "valley": valley_boundaries(conn, page_id),
    }

    # Union, then merge duplicates. Separator boundaries are kept as the
    # anchor when several signals land together (a printed rule is more
    # trustworthy than an inferred gutter).
    combined = _merge_close(sorted(sum(sig.values(), [])), min_gap=AGREEMENT_TOL_PCT)

    left, right = _text_extent(conn, page_id)
    interior = [b for b in combined if left + 1 < b < right - 1]

    edges = [left] + interior + [right]
    widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    if widths:
        med_w = statistics.median(widths)
        interior = _merge_close(interior, min_gap=med_w * MERGE_FRACTION)
        edges = [left] + interior + [right]
        widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
        # Drop slivers by removing the boundary that creates them.
        keep = []
        for i, b in enumerate(interior):
            if widths[i] >= med_w * MIN_COL_FRACTION and widths[i + 1] >= med_w * MIN_COL_FRACTION:
                keep.append(b)
        interior = keep
        edges = [left] + interior + [right]
        widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]

    unexplained = unexplained_columns(conn, page_id, edges)
    conf, why = _confidence(sig, interior, widths, unexplained)
    return {
        "signals": sig,
        "boundaries": interior,
        "edges": edges,
        "columns": [{"col_idx": i, "left_pct": edges[i], "right_pct": edges[i + 1]}
                    for i in range(len(edges) - 1)],
        "unexplained_columns": unexplained,
        "confidence": conf,
        "confidence_parts": why,
        "escalate": conf < CONFIDENCE_GATE,
    }


def unexplained_columns(conn, page_id: str, edges: list[float]) -> list[int]:
    """Indices of detected columns that still contain internal vertical
    structure -- i.e. a boundary we failed to find.

    This is the RECALL check, and it exists because visual inspection
    caught the confidence score flattering itself. On 1997-07-16 p11 the
    detector found one boundary, swallowed the entire right-hand display-ad
    region into a single 69%-wide 'column', and still scored 0.85 --
    because corroboration and rule_support both only ask "are the
    boundaries I found well-supported?", never "did I find them all?".

    The test uses ALL lines, not just body lines. That is the whole
    point: the missed region on that page was display-ad text, which the
    body-line x_size filter deliberately discards, so any check built on
    body lines alone is blind to exactly the case that failed.

    The test is run per horizontal BAND, not over the full column
    height. That is not a refinement, it is the whole reason the check
    works. Measured on 1997-07-16 p11's 69%-wide column: a full-height
    x-projection finds **zero** zero-coverage strips, because different
    stacks of display ads put their gutters in different places, and
    projecting the full height fills every gap. Banding the same region
    at 10% of page height reveals consistent gutters at 50.1-50.6% and
    73.2-73.4% across the y=50-80% bands. That page's right side is a
    *modular* layout, not a column grid -- which is exactly what
    instructions/layout_observations.md records for 1980s-2000s issues.
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT left_pct, right_pct, top_pct, bottom_pct FROM page_hocr_lines "
        "WHERE page_id=?", (page_id,))]
    if not rows:
        return []
    bin_pct, band_pct = 0.5, 10.0
    out = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if hi - lo < 8.0:
            continue  # too narrow to hide another column
        inner = [r for r in rows if r["left_pct"] >= lo - 0.5 and r["right_pct"] <= hi + 0.5]
        if len(inner) < 8:
            continue
        nbins = int((hi - lo) / bin_pct) + 1
        hits = 0
        for band in range(int(100 / band_pct)):
            t, b = band * band_pct, (band + 1) * band_pct
            sub = [r for r in inner if r["top_pct"] >= t and r["bottom_pct"] <= b]
            if len(sub) < 5:
                continue
            cover = [0] * nbins
            for r in sub:
                a = int((r["left_pct"] - lo) / bin_pct)
                e = int((r["right_pct"] - lo) / bin_pct)
                for k in range(max(0, a), min(nbins - 1, e) + 1):
                    cover[k] += 1
            run = 0
            for k in range(1, nbins - 1):
                if cover[k] == 0:
                    run += 1
                    if run * bin_pct >= 1.5:
                        hits += 1
                        break
                else:
                    run = 0
        # One band with an internal gutter could be a wide headline above
        # narrow text. Two or more is real unsplit structure.
        if hits >= 2:
            out.append(i)
    return out


def _agrees(b: float, others: list[float]) -> bool:
    return any(abs(b - o) <= AGREEMENT_TOL_PCT for o in others)


def _confidence(sig: dict, interior: list[float], widths: list[float],
                unexplained: list[int] | None = None) -> tuple[float, dict]:
    """Confidence = precision x recall, not precision alone.

    The first three components all measure *precision* -- "are the
    boundaries I found trustworthy?". Precision alone is what produced a
    0.85 score on a page where the detector missed most of the columns
    (1997-07-16 p11). `completeness` is the recall term, and it MULTIPLIES
    rather than adds: a detector that missed half the page should not be
    rescuable by being very sure about the half it got. See
    unexplained_columns().

    Every component is reported alongside the score so a low number can
    be diagnosed rather than merely distrusted.
    """
    if not interior:
        return 0.0, {"reason": "no interior boundaries found"}

    # (a) corroboration: what share of accepted boundaries are backed by
    #     more than one independently derived signal.
    backed = 0
    for b in interior:
        n = sum(1 for name in ("separator", "leftedge", "valley") if _agrees(b, sig[name]))
        if n >= 2:
            backed += 1
    corroboration = backed / len(interior)

    # (b) rule support: printed rules are the strongest single evidence.
    ruled = sum(1 for b in interior if _agrees(b, sig["separator"])) / len(interior)

    # (c) regularity: real newspaper grids have near-equal column widths.
    if len(widths) >= 2:
        m = statistics.mean(widths)
        cv = (statistics.pstdev(widths) / m) if m else 1.0
        regularity = max(0.0, 1.0 - cv)
    else:
        regularity = 0.5  # single column: nothing to be regular about

    # (d) completeness (RECALL): how much of the page is explained. Each
    #     column still holding unsplit content is a boundary we missed.
    n_cols = len(widths) if widths else 1
    n_bad = len(unexplained or [])
    completeness = max(0.0, 1.0 - (n_bad / n_cols)) if n_cols else 0.0

    precision = 0.45 * corroboration + 0.25 * ruled + 0.30 * regularity
    score = precision * completeness
    return round(score, 3), {
        "corroboration": round(corroboration, 3),
        "rule_support": round(ruled, 3),
        "regularity": round(regularity, 3),
        "completeness": round(completeness, 3),
        "precision": round(precision, 3),
        "n_boundaries": len(interior),
        "unexplained_cols": n_bad,
    }


# --- persistence -----------------------------------------------------

def store(conn, page_id: str, result: dict) -> None:
    """Persist both the combined answer and each raw signal, so a later
    review can see *why* a page scored the way it did without re-running."""
    conn.execute("DELETE FROM page_columns WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for c in result["columns"]:
        conn.execute(
            """INSERT INTO page_columns
               (id, page_id, col_idx, left_pct, right_pct, method, confidence, created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, c["col_idx"], c["left_pct"], c["right_pct"],
             "combined", result["confidence"], now,
             None if not result["escalate"] else "below confidence gate -- escalate"),
        )
    for name, bounds in result["signals"].items():
        for i, b in enumerate(bounds):
            conn.execute(
                """INSERT INTO page_columns
                   (id, page_id, col_idx, left_pct, right_pct, method, confidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (_sup.new_uuid(), page_id, i, b, b, name, None, now),
            )
    conn.commit()


def pages_to_run(conn, date: str | None) -> list[dict]:
    sql = ("SELECT id, year, month, day, page FROM pages "
           "WHERE hocr_parsed_at IS NOT NULL")
    params: list = []
    if date:
        y, m, d = (int(x) for x in date.split("-"))
        sql += " AND year=? AND month=? AND day=?"
        params += [y, m, d]
    sql += " ORDER BY year, month, day, page"
    return [dict(r) for r in conn.execute(sql, params)]


# --- CLI -------------------------------------------------------------

def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = pages_to_run(conn, args.date)
        if not rows:
            print("No parsed pages. Run `python3 -m transcribe.scaled.hocr_parse backfill` first.")
            return
        esc = 0
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            esc += bool(res["escalate"])
            flag = "  ESCALATE" if res["escalate"] else ""
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(res['columns'])} cols  conf={res['confidence']:.2f}{flag}")
        print(f"\n{len(rows)} page(s). Escalation rate: {esc}/{len(rows)} "
              f"({esc / len(rows) * 100:.1f}%) below gate {CONFIDENCE_GATE}")
    finally:
        conn.close()


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        row = conn.execute(
            "SELECT id FROM pages WHERE year=? AND month=? AND day=? AND page=?",
            (y, m, d, args.page)).fetchone()
        if row is None:
            print(f"No page {args.date} p{args.page}")
            return
        res = detect(conn, row["id"])
        for name, b in res["signals"].items():
            print(f"  {name:10s}: {b}")
        print(f"\n  combined boundaries: {res['boundaries']}")
        print(f"  columns ({len(res['columns'])}):")
        for c in res["columns"]:
            print(f"    {c['col_idx']}: {c['left_pct']:6.2f}% -> {c['right_pct']:6.2f}%  "
                  f"(w={c['right_pct'] - c['left_pct']:.2f}%)")
        print(f"\n  confidence: {res['confidence']}  {res['confidence_parts']}")
        print(f"  escalate: {res['escalate']}")
    finally:
        conn.close()


def _cmd_report(args):
    conn = _sup.open_connection()
    try:
        rows = conn.execute(
            """SELECT p.year, p.month, p.day, p.page, c.confidence,
                      count(*) AS n_cols
                 FROM page_columns c JOIN pages p ON p.id=c.page_id
                WHERE c.method='combined'
             GROUP BY c.page_id ORDER BY c.confidence""").fetchall()
        if not rows:
            print("No results. Run `run` first.")
            return
        confs = [r["confidence"] for r in rows]
        esc = [r for r in rows if r["confidence"] < CONFIDENCE_GATE]
        print(f"pages scored: {len(rows)}")
        print(f"confidence   min={min(confs):.2f} median={statistics.median(confs):.2f} "
              f"max={max(confs):.2f}")
        print(f"ESCALATION RATE: {len(esc)}/{len(rows)} = {len(esc) / len(rows) * 100:.1f}% "
              f"(gate {CONFIDENCE_GATE})")
        import collections
        print("column counts:", dict(sorted(collections.Counter(
            r["n_cols"] for r in rows).items())))
        print("\nlowest-confidence pages (inspect these first):")
        for r in rows[:10]:
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"conf={r['confidence']:.2f} cols={r['n_cols']}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pr = sub.add_parser("run", help="Detect and store columns for parsed pages")
    pr.add_argument("--date", help="YYYY-MM-DD (default: all parsed pages)")
    pr.set_defaults(func=_cmd_run)

    ps = sub.add_parser("show", help="Show one page's signals without writing")
    ps.add_argument("date")
    ps.add_argument("--page", type=int, required=True)
    ps.set_defaults(func=_cmd_show)

    pp = sub.add_parser("report", help="Escalation rate and confidence spread")
    pp.set_defaults(func=_cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
