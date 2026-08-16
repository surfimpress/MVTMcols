"""Separator rules from Tesseract, cleaned. NOT IN THE LIVE PATH.

**This module is OFF by default and the live pipeline does not use it.**
`separator_grid.build()` defaults to `clean=False` and reads raw
`ocr_separator` rows; this cleaning runs only under `--clean`, as a
comparison.

MEASURED, 90 pages, corner-derived zones:

    raw separators (live)   273 zones
    cleaned                 256 zones      worse on 15 pages, better on 10

On 1980-04-06 p13 cleaning drops the count 8 -> 7, and the box it loses is
the Sidewalk Sale -- the very box `_merge_fragments` was written to
rescue.

Why it reverses: this cleaning was built for the rule-PAIRING detector
(`archive/detect_boxes_pairing.py`), where a fragmented rule broke the
pair and a conjoined region invented one. The corner derivation wants rule
ENDS -- they are what become corners once near-misses are resolved to
their axis crossing -- and merging fragments removes ends.

Kept, not archived, because `--clean` is a genuinely useful diagnostic and
because the observation below is durable even though the remedy is not.

---

Tesseract reports the printed rules on a page as `ocr_separator` regions,
but it reports them imperfectly in two opposite ways, and BOTH have to be
undone before the rules can be trusted:

  CONJOINED   it emits the individual rules AND a single region covering
              them. On 1980-04-06 p13 the left edge appears three times --
              a 17px upper rule, a 29px lower rule, and a 50px region
              spanning both. The merged region bridges the gap between two
              genuinely separate rules and manufactures structure that is
              not there.

  FRAGMENTED  the opposite: one printed rule split into collinear pieces
              where something was pasted over it or the scan lost a
              stretch. The Sidewalk Sale box on p13 has its foot in two
              pieces, so the largest box on the page was invisible.

Both are handled here, once, so no consumer has to think about them.

NOT to be confused with a rule being genuinely long: a column rule is ONE
continuous strip with ads butting against it, and splitting it into
per-box segments invents gaps that were never in the ink. See
`instructions/typesetting_practice.md`.
"""

from __future__ import annotations

# Slack when deciding whether one rule's run sits inside another's.
CONJOIN_TOL_PCT = 0.3

# Two collinear pieces are the same printed rule when they sit this close
# across the rule, and the gap along it is no wider than the second value.
# The gap allowance is generous because what interrupts a rule (a page
# number, scan damage) can be several percent wide.
FRAGMENT_POS_PCT = 0.7
FRAGMENT_GAP_PCT = 8.0






def _drop_conjoined(rows: list[dict], orientation: str) -> list[dict]:
    """Remove separator regions that are several rules merged into one.

    Tesseract sometimes reports BOTH the individual rules AND a single
    region covering them. On 1980-04-06 p13 the left edge appears three
    times:

        V  x 4.29-4.69  y 25.82-47.79  (17px)   the real upper rule
        V  x 4.57-5.27  y 49.51-95.80  (29px)   the real lower rule
        V  x 3.76-4.96  y 25.82-95.88  (50px)   both, conjoined

    The merged region is thicker (roughly the sum) and spans the gap
    between the real rules, so it manufactures boxes across a boundary
    that is not there and hides the true ones.

    A region is conjoined when at least TWO others of the same
    orientation lie within its RUN and overlap it on the thickness axis.
    Containment of the full bbox is NOT the test -- the merged region is
    typically slightly WIDER than its own parts (3.76-4.96 against a part
    at 4.57-5.27), so a bbox test misses it.
    """
    keep = []
    for i, a in enumerate(rows):
        inner = 0
        for j, b in enumerate(rows):
            if i == j:
                continue
            if orientation == "vertical":
                within = (b["T"] >= a["T"] - CONJOIN_TOL_PCT
                          and b["B"] <= a["B"] + CONJOIN_TOL_PCT
                          and (b["B"] - b["T"]) < (a["B"] - a["T"]) * 0.9)
                overlaps = min(a["R"], b["R"]) - max(a["L"], b["L"]) > 0
            else:
                within = (b["L"] >= a["L"] - CONJOIN_TOL_PCT
                          and b["R"] <= a["R"] + CONJOIN_TOL_PCT
                          and (b["R"] - b["L"]) < (a["R"] - a["L"]) * 0.9)
                overlaps = min(a["B"], b["B"]) - max(a["T"], b["T"]) > 0
            if within and overlaps:
                inner += 1
        if inner < 2:
            keep.append(a)
    return keep


def _merge_fragments(rows: list[dict], orientation: str) -> list[dict]:
    """Join collinear pieces of one printed rule back together.

    The mirror of `_drop_conjoined`: Tesseract also SPLITS a single rule
    into segments, typically where something interrupts it. On
    1980-04-06 p13 the Sidewalk Sale box -- which occupies the whole
    lower half of the page -- has left, right and top rules but its foot
    arrives in pieces:

        H  x  4.43-75.05  y 95.12-96.02
        H  x 80.97-95.24  y 95.75-96.27

    Neither piece bridges both verticals, so the largest box on the page
    was missed entirely.

    Pieces are merged when they sit at the same position across the rule
    (within FRAGMENT_POS_PCT) and the gap along it is no wider than
    FRAGMENT_GAP_PCT. The merged rule spans the full extent and takes the
    heaviest thickness of its parts.
    """
    pos = (lambda r: (r["L"] + r["R"]) / 2) if orientation == "vertical" \
        else (lambda r: (r["T"] + r["B"]) / 2)
    lo = (lambda r: r["T"]) if orientation == "vertical" else (lambda r: r["L"])
    hi = (lambda r: r["B"]) if orientation == "vertical" else (lambda r: r["R"])

    out: list[dict] = []
    for r in sorted(rows, key=lambda r: (pos(r), lo(r))):
        merged = False
        for o in out:
            if abs(pos(o) - pos(r)) > FRAGMENT_POS_PCT:
                continue
            gap = max(lo(r) - hi(o), lo(o) - hi(r))
            if gap > FRAGMENT_GAP_PCT:
                continue
            o["L"], o["R"] = min(o["L"], r["L"]), max(o["R"], r["R"])
            o["T"], o["B"] = min(o["T"], r["T"]), max(o["B"], r["B"])
            for k in ("wd", "ht"):
                if r.get(k) and (o.get(k) or 0) < r[k]:
                    o[k] = r[k]
            merged = True
            break
        if not merged:
            out.append(dict(r))
    return out


def rules_of(conn, page_id: str, orientation: str) -> list[dict]:
    return _merge_fragments(_drop_conjoined([dict(r) for r in conn.execute(
        "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
        "width_px wd, height_px ht "
        "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_separator' "
        "AND orientation=?", (page_id, orientation))], orientation), orientation)
