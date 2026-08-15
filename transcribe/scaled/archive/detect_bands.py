"""Stage 2 (1980+): band-first segmentation, then columns within a band.

Why this replaces full-height column detection for this era
-----------------------------------------------------------
`detect_columns.py` escalated 88/90 pages (97.8%). That was not a tuning
failure -- it is the wrong model. Measured on 1997-07-16 p11: a
full-height x-projection of its 69%-wide right region finds **zero**
gutters, because different stacks of display ads put their gutters in
different places and projecting the whole height fills every gap. Band
the same region at 10% of page height and consistent gutters appear at
50.1-50.6% and 73.2-73.4% across y=50-80%.

That is what `instructions/layout_observations.md` already records for
1980s-2000s issues: *"modular -- no page-level grid at all"*. So the
unit of layout here is a **band** (a horizontal strip bounded by a rule
or a whitespace gap), and columns exist only *within* a band.

Signals, all from hOCR geometry -- no pixels, no LLM:
  - band cuts: wide horizontal `ocr_separator` rules, plus y-coverage
    gaps that no text line crosses.
  - columns per band: x-coverage gaps within that band's own lines.

Usage::

    python3 -m transcribe.scaled.detect_bands run [--date YYYY-MM-DD]
    python3 -m transcribe.scaled.detect_bands show 1997-07-16 --page 11
    python3 -m transcribe.scaled.detect_bands report
"""

from __future__ import annotations

import argparse
import json
import statistics

from . import _support as _sup

# A horizontal separator must span at least this much of the page width
# to be treated as a band divider rather than an underline or box edge.
MIN_RULE_WIDTH_PCT = 25.0

# A y-gap this tall (% of page height) that no line crosses is a band cut.
MIN_Y_GAP_PCT = 1.5

# Bands thinner than this are strips of noise, not layout units.
MIN_BAND_HEIGHT_PCT = 6.0

# An x-gap this wide inside a band, crossed by no line, is a column gutter.
MIN_GUTTER_PCT = 1.2

# Below this many lines a band is not worth measuring.
MIN_LINES_PER_BAND = 8

BIN_PCT = 0.5


def _cover(vals: list[tuple[float, float]]) -> list[int]:
    n = int(100 / BIN_PCT) + 1
    cov = [0] * n
    for lo, hi in vals:
        a, b = int(lo // BIN_PCT), int(hi // BIN_PCT)
        for k in range(max(0, a), min(n - 1, b) + 1):
            cov[k] += 1
    return cov


def _gaps(cov: list[int], min_run_pct: float, bounded: bool = True) -> list[float]:
    """Centres of contiguous zero-coverage runs at least min_run_pct wide.
    `bounded` restricts the search to between the first and last covered
    bin, so page margins don't register as gaps."""
    used = [i for i, c in enumerate(cov) if c > 0]
    if not used:
        return []
    lo, hi = (used[0], used[-1]) if bounded else (0, len(cov) - 1)
    out, run = [], 0
    for k in range(lo, hi + 1):
        if cov[k] == 0:
            run += 1
        else:
            if run * BIN_PCT >= min_run_pct:
                out.append(round((k - run / 2) * BIN_PCT, 2))
            run = 0
    return out


def band_cuts(conn, page_id: str, lines: list[dict]) -> list[float]:
    cuts = {0.0, 100.0}
    for r in conn.execute(
        "SELECT top_pct, bottom_pct, left_pct, right_pct FROM page_hocr_regions "
        "WHERE page_id=? AND region_class='ocr_separator' AND orientation='horizontal'",
        (page_id,),
    ):
        if (r["right_pct"] - r["left_pct"]) >= MIN_RULE_WIDTH_PCT:
            cuts.add(round((r["top_pct"] + r["bottom_pct"]) / 2, 2))
    cuts.update(_gaps(_cover([(l["top_pct"], l["bottom_pct"]) for l in lines]),
                      MIN_Y_GAP_PCT))
    return sorted(cuts)


def columns_in_band(lines: list[dict], top: float, bottom: float) -> dict | None:
    sub = [l for l in lines if l["top_pct"] >= top and l["bottom_pct"] <= bottom]
    if len(sub) < MIN_LINES_PER_BAND:
        return None
    cov = _cover([(l["left_pct"], l["right_pct"]) for l in sub])
    gutters = _gaps(cov, MIN_GUTTER_PCT)
    used = [i for i, c in enumerate(cov) if c > 0]
    left = round(used[0] * BIN_PCT, 2)
    right = round(used[-1] * BIN_PCT, 2)
    edges = [left] + gutters + [right]
    widths = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    if len(widths) >= 2:
        m = statistics.mean(widths)
        regularity = max(0.0, 1.0 - (statistics.pstdev(widths) / m if m else 1.0))
    else:
        regularity = 0.5
    return {"edges": edges, "n_columns": len(widths),
            "regularity": round(regularity, 3), "n_lines": len(sub)}


def detect(conn, page_id: str) -> dict:
    lines = [dict(r) for r in conn.execute(
        "SELECT left_pct, right_pct, top_pct, bottom_pct FROM page_hocr_lines "
        "WHERE page_id=?", (page_id,))]
    if not lines:
        return {"bands": [], "confidence": 0.0, "escalate": True,
                "note": "no hOCR lines"}

    cuts = band_cuts(conn, page_id, lines)
    bands = []
    for i in range(len(cuts) - 1):
        top, bottom = cuts[i], cuts[i + 1]
        if bottom - top < MIN_BAND_HEIGHT_PCT:
            continue
        cols = columns_in_band(lines, top, bottom)
        if cols is None:
            continue
        bands.append({"band_idx": len(bands), "top_pct": top, "bottom_pct": bottom,
                      **cols})

    if not bands:
        return {"bands": [], "confidence": 0.0, "escalate": True,
                "note": "no measurable bands"}

    # Coverage: what share of the page's lines fall inside a measured
    # band. A page whose text mostly sits outside any band we resolved is
    # not understood, however tidy the bands we did find look.
    inside = sum(b["n_lines"] for b in bands)
    coverage = min(1.0, inside / len(lines))
    multi = [b for b in bands if b["n_columns"] > 1]
    structured = len(multi) / len(bands)
    regularity = statistics.mean([b["regularity"] for b in multi]) if multi else 0.0

    conf = round(0.45 * coverage + 0.25 * structured + 0.30 * regularity, 3)
    return {
        "bands": bands,
        "confidence": conf,
        "confidence_parts": {"coverage": round(coverage, 3),
                             "structured_bands": round(structured, 3),
                             "regularity": round(regularity, 3),
                             "n_bands": len(bands)},
        "escalate": conf < 0.60,
    }


def store(conn, page_id: str, res: dict) -> None:
    conn.execute("DELETE FROM page_bands WHERE page_id=?", (page_id,))
    now = _sup.now_iso()
    for b in res["bands"]:
        conn.execute(
            """INSERT INTO page_bands
               (id, page_id, band_idx, top_pct, bottom_pct, n_columns,
                column_edges_json, regularity, n_lines, confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, b["band_idx"], b["top_pct"], b["bottom_pct"],
             b["n_columns"], json.dumps(b["edges"]), b["regularity"], b["n_lines"],
             res["confidence"], now))
    conn.commit()


def pages_to_run(conn, date: str | None) -> list[dict]:
    sql = ("SELECT id, year, month, day, page FROM pages "
           "WHERE hocr_parsed_at IS NOT NULL")
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
        esc = 0
        for r in rows:
            res = detect(conn, r["id"])
            store(conn, r["id"], res)
            esc += bool(res["escalate"])
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{len(res['bands'])} bands  conf={res['confidence']:.2f}"
                  f"{'  ESCALATE' if res['escalate'] else ''}")
        print(f"\n{len(rows)} page(s). Escalation: {esc}/{len(rows)} "
              f"({esc/max(1,len(rows))*100:.1f}%)")
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
        for b in res["bands"]:
            print(f"  band {b['band_idx']}: y {b['top_pct']:6.2f}-{b['bottom_pct']:6.2f}%  "
                  f"{b['n_columns']} col  reg={b['regularity']:.2f}  "
                  f"lines={b['n_lines']:4d}  edges={[round(e,1) for e in b['edges']]}")
        print(f"\n  confidence {res['confidence']}  {res.get('confidence_parts')}")
        print(f"  escalate: {res['escalate']}")
    finally:
        conn.close()


def _cmd_report(args):
    conn = _sup.open_connection()
    try:
        rows = conn.execute(
            "SELECT page_id, confidence, count(*) n FROM page_bands GROUP BY page_id"
        ).fetchall()
        if not rows:
            print("no results; run first")
            return
        confs = [r["confidence"] for r in rows]
        esc = [r for r in rows if r["confidence"] < 0.60]
        print(f"pages: {len(rows)}   bands: {sum(r['n'] for r in rows)}")
        print(f"confidence min={min(confs):.2f} median={statistics.median(confs):.2f} "
              f"max={max(confs):.2f}")
        print(f"ESCALATION: {len(esc)}/{len(rows)} = {len(esc)/len(rows)*100:.1f}%")
        import collections
        c = collections.Counter(r["n"] for r in rows)
        print("bands per page:", dict(sorted(c.items())))
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
