"""Stage 2: recover the page's underlying column grid.

Premise (see instructions/typesetting_practice.md): a newspaper page is
assembled on a fixed grid that the compositor aligned to -- non-repro
blue guides on a pasteboard, later master-page guides in PageMaker or
QuarkXPress. The grid is an INPUT to the page. It is four numbers:

    left margin | column width | gutter | column count

and every photo, ad and story block occupies an INTEGER number of
columns, never a fraction:

    width = n * col + (n - 1) * gutter

So this does not hunt for boundaries. It **fits four parameters** to the
edges the page already gives us, and treats anything that misses the
lattice as noise rather than as evidence of a new column width.

Per PAGE, not per issue -- by direction, because the photography of
these pages is highly variable (skew, scale, crop differ page to page),
and a single page carries plenty of blocks to fit four numbers.

Method
------
1. Pool the left and right edges of every block and text line.
2. Left edges pile up at column starts, right edges at column ends
   (body text is flush-left, and justified text is flush-right too).
   Histogram both.
3. Grid-search pitch (col + gutter) and offset; score by how many pooled
   edges land within tolerance of the lattice. The winning pitch is the
   one the page was actually set on.
4. Derive column width from the offset->edge distances, and the column
   count from the span of the text area.

Usage::

    python3 -m transcribe.scaled.detect_grid run [--date YYYY-MM-DD]
    python3 -m transcribe.scaled.detect_grid show 1980-04-06 --page 11
    python3 -m transcribe.scaled.detect_grid report
"""

from __future__ import annotations

import argparse
import statistics

from . import _support as _sup

# An edge counts as "on the grid" within this distance (% of page width).
# Generous enough to absorb scan skew and OCR bbox jitter, tight enough
# that a wrong pitch cannot score well.
SNAP_TOL_PCT = 0.9

# Plausible column pitch as % of page width. A 1-column page would be
# ~100%; 12 narrow classified columns ~8%. Outside this, it isn't a
# newspaper column grid.
MIN_PITCH_PCT = 6.0
MAX_PITCH_PCT = 55.0

PITCH_STEP = 0.05     # % of page width -- fine enough to land on real pitches
OFFSET_STEP = 0.05

# Edges closer together than this are the same edge seen twice.
EDGE_MERGE_PCT = 0.6

MIN_EDGES = 12        # below this a page cannot support a fit

# `fit` below is a DIAGNOSTIC, not a gate. Confidence scoring with an
# escalation threshold was tried and abandoned -- see
# transcribe/scaled/archive/README.md. Report the number, look at the
# page, don't let a self-authored score decide anything.


def pooled_edges(conn, page_id: str) -> tuple[list[float], list[float]]:
    """Left and right edges of every block and line on the page.

    Blocks and lines are pooled deliberately: they are set to the same
    grid, so pooling gives the fit more evidence without adding any new
    assumption.
    """
    lefts, rights = [], []
    for sql in (
        "SELECT bbox_left_pct L, bbox_right_pct R FROM page_ocr_blocks WHERE page_id=?",
        "SELECT left_pct L, right_pct R FROM page_hocr_lines WHERE page_id=?",
    ):
        for r in conn.execute(sql, (page_id,)):
            if r["R"] - r["L"] < 0.5:      # degenerate box
                continue
            lefts.append(round(r["L"], 2))
            rights.append(round(r["R"], 2))
    return lefts, rights


def _peaks(vals: list[float], bin_pct: float = 0.25,
           min_share: float = 0.015) -> list[tuple[float, int]]:
    """Cluster centres of a 1-D edge distribution, with their weights."""
    if not vals:
        return []
    hist: dict[int, list[float]] = {}
    for v in vals:
        hist.setdefault(int(v / bin_pct), []).append(v)
    floor = max(2, int(len(vals) * min_share))
    keep = sorted(b for b, xs in hist.items() if len(xs) >= floor)
    if not keep:
        return []
    out, run = [], [keep[0]]
    for b in keep[1:]:
        if (b - run[-1]) * bin_pct <= EDGE_MERGE_PCT:
            run.append(b)
        else:
            out.append(run)
            run = [b]
    out.append(run)
    peaks = []
    for grp in out:
        xs = [x for b in grp for x in hist[b]]
        peaks.append((round(statistics.median(xs), 2), len(xs)))
    return peaks


def _score_lattice(peaks: list[tuple[float, int]], offset: float, pitch: float,
                   lo: float, hi: float) -> float:
    """Chance-corrected share of edges landing on offset + k*pitch.

    The raw hit rate CANNOT be used directly: a finer lattice catches
    more edges by luck, so raw score rises monotonically as pitch falls
    and the fit collapses to the smallest allowed pitch. (Observed: a
    known 7-column page fitted as 15 columns at 6% pitch.)

    Each lattice line accepts a band of +/-SNAP_TOL_PCT, so a uniformly
    random edge hits with probability min(1, 2*tol/pitch). Subtracting
    that and renormalising gives Cohen's-kappa-style agreement: 0 means
    "no better than a lattice of that density would do by chance", 1
    means every alignment position is explained.

    Scored over PEAKS (weighted by how many edges formed each), not over
    raw edges. Most edges on a newspaper page are not grid-aligned at all
    -- ad interiors, centred headlines, captions -- so scoring every edge
    understates a correct grid badly: 1980-04-06 p11, visually verified
    as landing on the page's real printed rules, scored only 0.20 that
    way. A peak is an alignment position the page actually uses, which is
    what the grid is supposed to explain.
    """
    if pitch <= 0:
        return 0.0
    hit = tot = 0
    for e, wgt in peaks:
        if e < lo - SNAP_TOL_PCT or e > hi + SNAP_TOL_PCT:
            continue
        tot += wgt
        k = round((e - offset) / pitch)
        if abs((offset + k * pitch) - e) <= SNAP_TOL_PCT:
            hit += wgt
    if not tot:
        return 0.0
    observed = hit / tot
    chance = min(1.0, (2 * SNAP_TOL_PCT) / pitch)
    if chance >= 1.0:
        return 0.0
    return max(0.0, (observed - chance) / (1.0 - chance))


def fit_grid(lefts: list[float], rights: list[float]) -> dict | None:
    """Fit margin / column width / gutter / column count."""
    if len(lefts) + len(rights) < MIN_EDGES:
        return None

    left_peaks = _peaks(lefts)
    right_peaks = _peaks(rights)
    if not left_peaks or not right_peaks:
        return None
    all_peaks = left_peaks + right_peaks

    text_left = min(p for p, _ in left_peaks)
    text_right = max(p for p, _ in right_peaks)
    span = text_right - text_left
    if span < MIN_PITCH_PCT:
        return None

    # Search pitch directly. An earlier version forced pitch = span/n,
    # which silently assumes the LAST column *starts* at the text right
    # edge -- it actually *ends* there, so that formula understates pitch
    # by roughly gutter/n and drags the whole lattice left. On
    # 1980-04-06 p11 it put lines at 49.5 and 72.1 while the page's real
    # alignment peaks sat at 52 and 75.
    #
    # Correct relation, from the typesetting model:
    #     span = (n - 1) * pitch + col_width
    # Rather than solve that with col_width unknown, scan pitch and let
    # the score decide; n then falls out of the span.
    best = None
    steps = int((MAX_PITCH_PCT - MIN_PITCH_PCT) / PITCH_STEP) + 1
    for i in range(steps):
        pitch = MIN_PITCH_PCT + i * PITCH_STEP
        ncols = int(round(span / pitch))
        if ncols < 1 or ncols > 20:
            continue
        # Offset may drift off text_left with skew or a hanging indent.
        for d in range(-8, 9):
            offset = text_left + d * OFFSET_STEP
            sc = _score_lattice(all_peaks, offset, pitch, text_left, text_right)
            if best is None or sc > best["score"] + 1e-9:
                best = {"score": round(sc, 3), "offset": round(offset, 2),
                        "pitch": round(pitch, 2), "n_columns": ncols}
    if best is None:
        return None

    # Column width = start -> the column's DOMINANT right-edge peak.
    # Use the heaviest peak, not the furthest: body text right-aligns at
    # the column end, but stray boxes (rules, overhanging headlines) sit
    # further right and would swallow the gutter. Taking max() gave a
    # 0.3% gutter on a page whose real gutter is ~1 pica.
    widths = []
    for k in range(best["n_columns"]):
        start = best["offset"] + k * best["pitch"]
        end = start + best["pitch"]
        inside = [(cnt, p) for p, cnt in right_peaks
                  if start < p <= end + SNAP_TOL_PCT]
        if inside:
            widths.append(max(inside)[1] - start)
    col_w = round(statistics.median(widths), 2) if widths else round(best["pitch"], 2)
    gutter = round(max(0.0, best["pitch"] - col_w), 2)

    edges_out = [round(best["offset"] + k * best["pitch"], 2)
                 for k in range(best["n_columns"] + 1)]
    return {**best, "text_left": round(text_left, 2), "text_right": round(text_right, 2),
            "col_width": col_w, "gutter": gutter, "edges": edges_out,
            "n_edges": len(lefts) + len(rights), "n_peaks": len(all_peaks)}


def detect(conn, page_id: str) -> dict:
    lefts, rights = pooled_edges(conn, page_id)
    grid = fit_grid(lefts, rights)
    if grid is None:
        return {"grid": None, "fit": 0.0,
                "note": f"insufficient edges ({len(lefts) + len(rights)})"}

    # Confidence IS the fit quality: the share of the page's own edges
    # that land on the lattice. No separate corroboration/regularity
    # terms -- on a designed grid, "do the edges land on it?" is the
    # whole question.
    return {"grid": grid, "fit": grid["score"]}


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_columns WHERE page_id=? AND method='grid'",
                 (page_id,))
    g = res.get("grid")
    if not g:
        return
    now = _sup.now_iso()
    for i in range(len(g["edges"]) - 1):
        conn.execute(
            """INSERT INTO page_columns
               (id, page_id, col_idx, left_pct, right_pct, method, confidence,
                created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, i, g["edges"][i],
             round(g["edges"][i] + g["col_width"], 2), "grid", res["fit"], now,
             f"pitch={g['pitch']} col={g['col_width']} gutter={g['gutter']}"))
    conn.commit()


def pages_to_run(conn, date: str | None) -> list[dict]:
    sql = "SELECT id, year, month, day, page FROM pages WHERE hocr_parsed_at IS NOT NULL"
    params: list = []
    if date:
        y, m, d = (int(x) for x in date.split("-"))
        sql += " AND year=? AND month=? AND day=?"
        params += [y, m, d]
    return [dict(r) for r in conn.execute(sql + " ORDER BY year,month,day,page", params)]


def _cmd_run(args):
    conn = _sup.open_connection()
    try:
        rows = pages_to_run(conn, args.date)
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            g = res.get("grid")
            desc = (f"{g['n_columns']} col  pitch={g['pitch']:.2f}%  "
                    f"col={g['col_width']:.2f}%  gutter={g['gutter']:.2f}%"
                    if g else "no fit")
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{desc}  fit={res['fit']:.2f}")
        print(f"\n{len(rows)} page(s) fitted.")
    finally:
        conn.close()


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
        g = res.get("grid")
        if not g:
            print("  no fit:", res.get("note"))
            return
        print(f"  text area   : {g['text_left']}% .. {g['text_right']}%")
        print(f"  columns     : {g['n_columns']}")
        print(f"  pitch       : {g['pitch']}%  (column {g['col_width']}% + gutter {g['gutter']}%)")
        print(f"  edges       : {g['edges']}")
        print(f"  fit          : {g['score']:.2f}  "
              f"(peak weight explained, chance-corrected)")
    finally:
        conn.close()


def _cmd_report(args):
    conn = _sup.open_connection()
    try:
        rows = conn.execute(
            "SELECT page_id, confidence, count(*) n FROM page_columns "
            "WHERE method='grid' GROUP BY page_id").fetchall()
        if not rows:
            print("no results; run first")
            return
        confs = [r["confidence"] for r in rows]
        print(f"pages fitted: {len(rows)}")
        print(f"fit  min={min(confs):.2f} median={statistics.median(confs):.2f} "
              f"max={max(confs):.2f}   (diagnostic only, not a gate)")
        import collections
        print("column counts:", dict(sorted(collections.Counter(
            r["n"] for r in rows).items())))
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("run"); pr.add_argument("--date"); pr.set_defaults(func=_cmd_run)
    ps = sub.add_parser("show"); ps.add_argument("date")
    ps.add_argument("--page", type=int, required=True); ps.set_defaults(func=_cmd_show)
    pp = sub.add_parser("report"); pp.set_defaults(func=_cmd_report)
    a = p.parse_args(); a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
