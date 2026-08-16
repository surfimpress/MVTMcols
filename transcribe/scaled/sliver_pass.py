"""Stage 1b — SLIVERS at the page rim. Runs before the content area.

Separators, and photos that SIZE as slivers -- the binding shadow comes
back as an `ocr_photo` at least as often as a separator (1980-04-06 p4:
x 0.00-2.34 spanning y 0.75-81.56, a full-height strip down the binding).
A photo big enough to be a picture is never a candidate: it fails the same
THIN_CELLS test the separators use, so one size rule covers both.

Blocks are read as evidence but never removed: a block with words is real
type.

THE PROBLEM THIS SOLVES
-----------------------
The binding gutter and the sheet edge come back from Tesseract as
`ocr_separator` regions, and they are not printing. Removing them by
"thin and near an edge" alone does not work: the measured page margin is
7-9 cells, so any band wide enough to catch the shadows also reaches the
content edge, and a box border sitting ON that edge looks identical.
Measured, that test put 244 of 773 removals wholly INSIDE the content
area -- 32% false positives.

THE APPROACH, in three tiers
----------------------------
TIER 1 -- SAFE. A sliver lying entirely within the outer RIM of the page
is removed outright. Nothing is printed in the margin, so anything wholly
inside it is an artefact. No alignment test needed.

TIER 2 -- CANDIDATE. A sliver that reaches past the rim is removed only
if NOTHING ALIGNS WITH IT. A rule that is real belongs to the column
structure and something else shares its position: the column it separates,
the blocks that stop against it, the rules above and below it in the same
gutter. A shadow is corroborated by nothing.

TIER 3 -- THE RIM IS NOT FIXED. If content blocks stray into the rim --
especially more than one -- then the margin on that page really is narrow,
which is a quirk of how the page was photographed, not evidence that the
content is an artefact. The rim is pulled in to stop short of the
outermost intruding block, per side. So the safe tier can never remove
something at or beyond where content demonstrably starts.

Usage::

    python3 -m transcribe.scaled.sliver_pass 1980-04-06 --page 4
    python3 -m transcribe.scaled.sliver_pass --all
"""

from __future__ import annotations

import argparse

from . import _support as _sup

# The nominal rim, in cells. The corpus page margin measures 7-9 cells, so
# 4 sits comfortably inside it -- printing does not begin this far out.
RIM_CELLS = 4.0

# A separator this thin across its short axis is a sliver. A real column
# rule is thinner still, so this is not what distinguishes them; position
# is. It exists only to keep genuinely broad regions out of the pass.
THIN_CELLS = 8.0

# Tier 2: something must share the sliver's position on its long axis.
MIN_ALIGN = 2

# Tier 3: this many blocks intruding into the rim means the margin really
# is narrow. ONE is enough to pull the rim in -- a single block that far
# out is either real content or a badly segmented one, and in both cases
# removing a rule beyond it is a guess. The user's "especially more than
# one" is reported as `n_intruders` so the strength of the signal travels
# with the decision.
MIN_INTRUDERS = 1


def _items(conn, page_id):
    out = [dict(r, kind="ocr_carea") for r in conn.execute(
        "SELECT bbox_left_pct L, bbox_top_pct T, bbox_right_pct R, "
        "bbox_bottom_pct B FROM page_ocr_blocks WHERE page_id=? AND n_words>0",
        (page_id,))]
    out += [dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
        "region_class kind FROM page_hocr_regions WHERE page_id=?", (page_id,))]
    return [i for i in out if i["R"] > i["L"] and i["B"] > i["T"]]


def effective_rim(items, cw, chh):
    """The rim per side, pulled in by any content block that intrudes.

    Returned in CELLS, keyed by side. A side whose rim is 0.0 has content
    hard against the page edge, and nothing on that side can be removed by
    the safe tier.
    """
    blocks = [i for i in items if i["kind"] == "ocr_carea"]
    out, counts = {}, {}
    for side in ("left", "right", "top", "bottom"):
        if side == "left":
            dist = [b["L"] / cw for b in blocks]
        elif side == "right":
            dist = [(100 - b["R"]) / cw for b in blocks]
        elif side == "top":
            dist = [b["T"] / chh for b in blocks]
        else:
            dist = [(100 - b["B"]) / chh for b in blocks]
        intruders = [d for d in dist if d < RIM_CELLS]
        counts[side] = len(intruders)
        out[side] = (min(intruders) if len(intruders) >= MIN_INTRUDERS
                     else RIM_CELLS)
    return out, counts


def _is_rim_sliver(i, cw, chh):
    """Any item, of any type, that is itself thin and lying at the rim."""
    w, h = (i["R"] - i["L"]) / cw, (i["B"] - i["T"]) / chh
    if min(w, h) > THIN_CELLS:
        return False
    if h >= w:
        return min(i["L"] / cw, (100 - i["R"]) / cw) <= RIM_CELLS
    return min(i["T"] / chh, (100 - i["B"]) / chh) <= RIM_CELLS


def _aligns(s, items, cw, chh, vertical):
    """How many other items share the sliver's position on its long axis.

    A RIM SLIVER DOES NOT COUNT AS CORROBORATION, whatever its type. Two
    shadows lying along the same edge agree with each other perfectly, and
    without this the pass talks itself out of every removal it should make.

    1980-04-06 p4: the left shadow separator at x 1.13-2.32 was kept on
    "2 items align", and both were the binding shadow photo at x 0.00-2.34
    (spanning y 0.75-81.56, a full-height strip) and a second edge region.
    Neither is printing. Corroboration has to come from something that is
    not itself a candidate.
    """
    tol = cw if vertical else chh
    a1, a2 = (s["L"], s["R"]) if vertical else (s["T"], s["B"])
    n = 0
    for j in items:
        if j is s or _is_rim_sliver(j, cw, chh):
            continue
        b1, b2 = (j["L"], j["R"]) if vertical else (j["T"], j["B"])
        if min(abs(b1 - a1), abs(b2 - a2),
               abs(b1 - a2), abs(b2 - a1)) <= tol:
            n += 1
    return n


def classify(conn, page_id):
    """Every separator, with a verdict and the reason for it."""
    cw, chh = _sup.cell_size(conn, page_id)
    items = _items(conn, page_id)
    rim, counts = effective_rim(items, cw, chh)

    out = []
    for s in items:
        # Separators, and PHOTOS THAT SIZE AS SLIVERS. A photo wide enough
        # to be a picture is never a candidate -- it falls out below as
        # "not a sliver" on the same THIN_CELLS test the separators use, so
        # there is one size rule, not two. Blocks are never candidates: a
        # block with words is real type.
        if s["kind"] not in ("ocr_separator", "ocr_photo"):
            continue
        w, h = (s["R"] - s["L"]) / cw, (s["B"] - s["T"]) / chh
        vertical = h >= w
        # Distance from each end of the SHORT axis to the page edge, which
        # is the axis a sliver hugs. A vertical sliver hugs left or right.
        if vertical:
            near, far, side = s["L"] / cw, s["R"] / cw, "left"
            if (100 - s["R"]) / cw < near:
                near, far, side = (100 - s["R"]) / cw, (100 - s["L"]) / cw, "right"
        else:
            near, far, side = s["T"] / chh, s["B"] / chh, "top"
            if (100 - s["B"]) / chh < near:
                near, far, side = (100 - s["B"]) / chh, (100 - s["T"]) / chh, "bottom"

        rec = {"sep": s, "side": side, "near": round(near, 2),
               "far": round(far, 2), "rim": round(rim[side], 2),
               "n_intruders": counts[side], "thin": round(min(w, h), 2),
               "vertical": vertical, "align": None}

        if min(w, h) > THIN_CELLS:
            rec["verdict"], rec["why"] = "keep", "not a sliver"
        elif far <= rim[side]:
            rec["verdict"], rec["why"] = "remove", "tier 1: wholly inside the rim"
        else:
            rec["align"] = _aligns(s, items, cw, chh, vertical)
            if near > RIM_CELLS:
                rec["verdict"], rec["why"] = "keep", "not at the rim"
            elif rec["align"] < MIN_ALIGN:
                rec["verdict"], rec["why"] = "remove", "tier 2: reaches past the rim, nothing aligns"
            else:
                rec["verdict"], rec["why"] = "keep", f"tier 2: {rec['align']} items align"
        out.append(rec)
    return out, rim, counts


def items_of(conn, page_id):
    """Every item BEFORE the pass -- for callers reporting what it removed."""
    return _items(conn, page_id)


def survivors(conn, page_id):
    """Every Tesseract item on the page with the rim slivers taken out.

    THE ENTRY POINT for later stages. Blocks pass through untouched; the
    separators and photos that this pass judged are filtered to those it
    kept. Callers get one list and never have to know the rules.
    """
    recs, _rim, _counts = classify(conn, page_id)
    gone = {(round(r["sep"]["L"], 4), round(r["sep"]["T"], 4),
             round(r["sep"]["R"], 4), round(r["sep"]["B"], 4),
             r["sep"]["kind"])
            for r in recs if r["verdict"] == "remove"}
    out = []
    for i in _items(conn, page_id):
        k = (round(i["L"], 4), round(i["T"], 4),
             round(i["R"], 4), round(i["B"], 4), i["kind"])
        if k not in gone:
            out.append(i)
    return out


def _cmd_page(conn, date, page):
    y, m, d = (int(v) for v in date.split("-"))
    row = conn.execute("SELECT id FROM pages WHERE year=? AND month=? AND day=? "
                       "AND page=?", (y, m, d, page)).fetchone()
    if not row:
        print("no such page")
        return
    recs, rim, counts = classify(conn, row["id"])
    print(f"{date} p{page}   rim per side (cells, nominal {RIM_CELLS}):")
    for k in ("left", "right", "top", "bottom"):
        note = f"  <- pulled in by {counts[k]} block(s)" if rim[k] < RIM_CELLS else ""
        print(f"     {k:7s} {rim[k]:5.2f}{note}")
    print(f"\n   {len(recs)} separators")
    for r in recs:
        s = r["sep"]
        a = "" if r["align"] is None else f" align {r['align']:2d}"
        print(f"     {r['verdict']:6s} x {s['L']:6.2f}-{s['R']:6.2f} "
              f"y {s['T']:6.2f}-{s['B']:6.2f}  {r['side']:6s} "
              f"{r['near']:5.1f}-{r['far']:5.1f} cells{a}   {r['why']}")


def _cmd_all(conn):
    from collections import Counter
    tally = Counter()
    per_page = []
    for row in conn.execute("SELECT id,year,month,day,page FROM pages "
                            "WHERE hocr_parsed_at IS NOT NULL "
                            "ORDER BY year,month,day,page"):
        recs, rim, counts = classify(conn, row["id"])
        rm = [r for r in recs if r["verdict"] == "remove"]
        for r in recs:
            tally[r["why"]] += 1
        narrow = [k for k, v in rim.items() if v < RIM_CELLS]
        per_page.append((f"{row['year']}-{row['month']:02d}-{row['day']:02d} "
                         f"p{row['page']}", len(recs), len(rm), narrow))
    print("VERDICTS across the corpus")
    for why, n in tally.most_common():
        print(f"   {n:5d}  {why}")
    tot = sum(p[1] for p in per_page)
    rem = sum(p[2] for p in per_page)
    print(f"\n   {rem} of {tot} separators removed ({rem/tot*100:.1f}%) "
          f"over {len(per_page)} pages")
    nar = [p for p in per_page if p[3]]
    print(f"   pages with a rim pulled in by intruding content: {len(nar)}")
    for p in nar[:8]:
        print(f"      {p[0]:20s} sides {p[3]}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("date", nargs="?")
    p.add_argument("--page", type=int)
    p.add_argument("--all", action="store_true")
    a = p.parse_args()
    conn = _sup.open_connection()
    try:
        if a.all:
            _cmd_all(conn)
        else:
            _cmd_page(conn, a.date, a.page)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
