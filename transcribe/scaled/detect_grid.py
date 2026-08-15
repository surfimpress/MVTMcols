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


# --- refinement: subsume stray blocks -------------------------------
# A block wholly inside another, much narrower than it, and only a few
# lines tall, is not an independent layout element -- it is a fragment
# Tesseract split out of its parent (a drop cap, a price, a stray line
# of an ad). Left in place, these fragments contribute edges at
# arbitrary x positions and blur the grid fit.

MAX_SUBSUME_WIDTH_FRAC = 0.5   # narrower than half the parent
MAX_SUBSUME_LINES = 3          # and no more than this many hOCR lines


def page_blocks(conn, page_id: str) -> list[dict]:
    """Blocks with their hOCR line counts, ready for refinement."""
    counts = {r["block_idx"]: r["n"] for r in conn.execute(
        "SELECT block_idx, count(*) AS n FROM page_hocr_lines "
        "WHERE page_id=? GROUP BY block_idx", (page_id,))}
    out = []
    for r in conn.execute(
        "SELECT block_idx, bbox_left_pct L, bbox_top_pct T, bbox_right_pct R, "
        "bbox_bottom_pct B FROM page_ocr_blocks WHERE page_id=? ORDER BY block_idx",
        (page_id,),
    ):
        if r["R"] - r["L"] < 0.5:
            continue
        out.append({"block_idx": r["block_idx"], "L": r["L"], "T": r["T"],
                    "R": r["R"], "B": r["B"], "n_lines": counts.get(r["block_idx"], 0)})
    return out


def _contains(outer: dict, inner: dict, tol: float = 0.3) -> bool:
    return (inner["L"] >= outer["L"] - tol and inner["R"] <= outer["R"] + tol
            and inner["T"] >= outer["T"] - tol and inner["B"] <= outer["B"] + tol)


def subsume_stray_blocks(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge stray fragments into the block that encloses them.

    A block is subsumed when ALL of:
      - it lies wholly within another block,
      - it is narrower than MAX_SUBSUME_WIDTH_FRAC of that parent,
      - it has at most MAX_SUBSUME_LINES hOCR lines.

    The parent's bbox is unchanged -- the child was already inside it, so
    there is nothing to grow. Returns (kept blocks, subsumed blocks).
    """
    kept, subsumed = [], []
    for i, b in enumerate(blocks):
        if b["n_lines"] > MAX_SUBSUME_LINES:
            kept.append(b)
            continue
        bw = b["R"] - b["L"]
        parent = None
        for j, other in enumerate(blocks):
            if i == j:
                continue
            ow = other["R"] - other["L"]
            if ow <= bw:
                continue
            if bw >= ow * MAX_SUBSUME_WIDTH_FRAC:
                continue
            if _contains(other, b):
                # Smallest qualifying enclosing block is the true parent.
                if parent is None or ow < (parent["R"] - parent["L"]):
                    parent = other
        if parent is None:
            kept.append(b)
        else:
            subsumed.append({**b, "parent_block_idx": parent["block_idx"]})
    return kept, subsumed


def edges_from_blocks(blocks: list[dict]) -> tuple[list[float], list[float]]:
    return ([round(b["L"], 2) for b in blocks], [round(b["R"], 2) for b in blocks])


def line_edges(conn, page_id: str,
               exclude_block_idx: set | None = None) -> tuple[list[float], list[float]]:
    """Line edges, optionally dropping the lines of subsumed blocks."""
    lefts, rights = [], []
    for r in conn.execute(
        "SELECT block_idx, left_pct L, right_pct R FROM page_hocr_lines WHERE page_id=?",
        (page_id,),
    ):
        if r["R"] - r["L"] < 0.5:
            continue
        if exclude_block_idx and r["block_idx"] in exclude_block_idx:
            continue
        lefts.append(round(r["L"], 2))
        rights.append(round(r["R"], 2))
    return lefts, rights


def pooled_edges(conn, page_id: str) -> tuple[list[float], list[float]]:
    """Left and right edges of every block and line on the page.

    Blocks and lines are pooled deliberately: they are set to the same
    grid, so pooling gives the fit more evidence without adding any new
    assumption.
    """
    bl, br = edges_from_blocks(page_blocks(conn, page_id))
    ll, lr = line_edges(conn, page_id)
    return bl + ll, br + lr


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


# How far a column edge may be pulled to meet the majority alignment in
# pass 2. Wide enough to absorb scan scale drift across the page, narrow
# enough that a column cannot migrate into its neighbour's slot.
SNAP_SEARCH_PCT = 2.0

# For the LAST column only. The right margin is ragged: most lines stop
# short of the column edge, so the heaviest right-edge cluster sits LEFT
# of the truth and snapping to it makes the last column too narrow.
#
# Only items at least this fraction of the column width contribute a
# right edge for that decision.
#
# MEASURED, and the result is worth stating: sweeping this from 0.0 to
# 0.9 changes the outcome on ONE page in 90. The reason is that short
# items end further LEFT, so they cannot bias a rightmost-selection at
# all -- taking the max already immunises against them. The filter's real
# and much narrower job is to stop a thin OVERHANGING fragment from
# setting the edge, which is rare here. Kept as a cheap guard, not as a
# tuning knob: do not expect gains from adjusting it.
LAST_COL_MIN_ITEM_FRAC = 0.60


def analyse(conn, page_id: str, blocks: list[dict] | None = None,
            exclude_block_idx: set | None = None) -> dict | None:
    """THE reusable analysis: edges -> fitted grid.

    Run once on the raw blocks and again after refinement, so both passes
    are guaranteed to be the same computation on different input.
    """
    blocks = page_blocks(conn, page_id) if blocks is None else blocks
    bl, br = edges_from_blocks(blocks)
    ll, lr = line_edges(conn, page_id, exclude_block_idx)
    return fit_grid(bl + ll, br + lr)


def _snap(predicted: float, peaks: list[tuple[float, int]]) -> tuple[float, bool]:
    """Pull a predicted edge to the heaviest peak within SNAP_SEARCH_PCT.

    Ties in weight are broken by proximity, so a distant peak never wins
    over an equally-supported near one.
    """
    near = [(w, -abs(x - predicted), x) for x, w in peaks
            if abs(x - predicted) <= SNAP_SEARCH_PCT]
    if not near:
        return predicted, False
    return round(max(near)[2], 2), True


def wide_right_edges(conn, page_id: str, kept: list[dict],
                     exclude_block_idx: set, min_width_pct: float) -> list[float]:
    """Right edges of blocks and lines at least `min_width_pct` wide.

    Short items are excluded deliberately: at the ragged right margin
    they mark where a line of text happened to stop, not where the column
    ends.
    """
    out = [b["R"] for b in kept if (b["R"] - b["L"]) >= min_width_pct]
    for r in conn.execute(
        "SELECT block_idx, left_pct L, right_pct R FROM page_hocr_lines WHERE page_id=?",
        (page_id,),
    ):
        if r["block_idx"] in exclude_block_idx:
            continue
        if (r["R"] - r["L"]) >= min_width_pct:
            out.append(round(r["R"], 2))
    return out


def _snap_rightmost(predicted: float, peaks: list[tuple[float, int]],
                    window: float) -> tuple[float, bool]:
    """Pull an edge to the most RIGHTWARD significant peak in a window.

    For the final column only. `peaks` should already be built from
    WIDE items alone (see wide_right_edges) -- short items at a ragged
    margin mark where text stopped, not where the column ends.

    `predicted` comes from the column pitch established by the other
    columns, so this stays anchored to the grid rather than free-running.
    """
    near = [x for x, _w in peaks if abs(x - predicted) <= window]
    if not near:
        return predicted, False
    return round(max(near), 2), True


def detect(conn, page_id: str) -> dict:
    """Two passes.

    PASS 1 establishes the likely columns -- pitch, offset, column width,
    column count -- from the raw blocks. That is a rigid lattice, and a
    rigid lattice cannot follow the scan's own scale drift across the
    page (measured: edges landing ~1.3% right of their slot at the
    right-hand end while fitting well on the left).

    Refinement then subsumes stray fragment blocks, which contribute
    edges at arbitrary x and blur the peaks.

    PASS 2 re-runs the same analysis on the cleaned blocks and then
    refines each column to the MAJORITY ALIGNMENT: every column edge is
    pulled to the heaviest nearby edge peak. The result follows the page
    as printed rather than holding a perfect lattice the page never had.
    """
    blocks = page_blocks(conn, page_id)
    first = analyse(conn, page_id, blocks)

    kept, subsumed = subsume_stray_blocks(blocks)
    dropped = {b["block_idx"] for b in subsumed}
    second = analyse(conn, page_id, kept, exclude_block_idx=dropped)

    grid = second or first
    if grid is None:
        return {"grid": None, "fit": 0.0, "note": "insufficient edges",
                "n_blocks": len(blocks), "n_kept": len(kept),
                "subsumed": len(subsumed), "subsumed_blocks": subsumed}

    # Majority-alignment refinement, using the cleaned edge distribution.
    bl, br = edges_from_blocks(kept)
    ll, lr = line_edges(conn, page_id, dropped)
    left_peaks = _peaks(bl + ll)
    right_peaks = _peaks(br + lr)
    wide_peaks = _peaks(wide_right_edges(
        conn, page_id, kept, dropped,
        grid["col_width"] * LAST_COL_MIN_ITEM_FRAC))

    last = grid["n_columns"] - 1
    columns, snapped = [], 0
    for k in range(grid["n_columns"]):
        pl = grid["offset"] + k * grid["pitch"]
        left, hit_l = _snap(pl, left_peaks)
        predicted_right = left + grid["col_width"]
        if k == last:
            # Ragged right margin -- lean rightward using WIDE items only,
            # anchored on the pitch prediction from the other columns.
            # Window widened a little: this is the edge most likely to be
            # under-reached.
            right, hit_r = _snap_rightmost(predicted_right, wide_peaks,
                                           SNAP_SEARCH_PCT * 1.5)
        else:
            right, hit_r = _snap(predicted_right, right_peaks)
        if right - left < grid["col_width"] * 0.5:   # snapped onto itself
            right = round(predicted_right, 2)
            hit_r = False
        # Columns may not overlap: a snap that crosses the previous
        # column's right edge would put one column inside another.
        if columns and left < columns[-1]["right_pct"]:
            mid = round((left + columns[-1]["right_pct"]) / 2, 2)
            columns[-1]["right_pct"] = mid
            left = mid
        snapped += int(hit_l) + int(hit_r)
        columns.append({"col_idx": k, "left_pct": round(left, 2),
                        "right_pct": round(right, 2),
                        "snapped_left": hit_l, "snapped_right": hit_r})

    return {"grid": grid, "fit": grid["score"],
            "fit_before_refine": first["score"] if first else None,
            "columns": columns,
            "edges_snapped": snapped, "edges_total": 2 * grid["n_columns"],
            "n_blocks": len(blocks), "n_kept": len(kept),
            "subsumed": len(subsumed), "subsumed_blocks": subsumed}


def store(conn, page_id: str, res: dict) -> None:
    """Persist the pass-2 (majority-aligned) columns -- that is the
    answer. The pass-1 lattice parameters go in `notes` so the rigid fit
    the refinement started from stays inspectable."""
    conn.execute("DELETE FROM page_columns WHERE page_id=? AND method='grid'",
                 (page_id,))
    g = res.get("grid")
    if not g or not res.get("columns"):
        return
    now = _sup.now_iso()
    for c in res["columns"]:
        conn.execute(
            """INSERT INTO page_columns
               (id, page_id, col_idx, left_pct, right_pct, method, confidence,
                created_at, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, c["col_idx"], c["left_pct"], c["right_pct"],
             "grid", res["fit"], now,
             f"pass1 pitch={g['pitch']} col={g['col_width']} gutter={g['gutter']}; "
             f"snapped L={c['snapped_left']} R={c['snapped_right']}"))
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
                  f"{desc}  fit={res['fit']:.2f}  "
                  f"subsumed={res.get('subsumed', 0)}")
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
        print(f"  refinement   : {res['n_blocks']} blocks -> {res['n_kept']} "
              f"({res['subsumed']} stray subsumed); "
              f"{res['edges_snapped']}/{res['edges_total']} edges snapped "
              f"to majority alignment")
        print("  pass 2 columns (majority-aligned):")
        for c in res["columns"]:
            gut = ""
            nxt = next((x for x in res["columns"] if x["col_idx"] == c["col_idx"] + 1), None)
            if nxt:
                gut = f"  gutter {nxt['left_pct'] - c['right_pct']:+.2f}%"
            print(f"    col {c['col_idx']}: {c['left_pct']:6.2f}% -> {c['right_pct']:6.2f}%"
                  f"  (w {c['right_pct'] - c['left_pct']:5.2f}%){gut}")
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
