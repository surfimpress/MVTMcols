"""Full-fidelity hOCR parse -- Stage 1b of the scaled pipeline.

`ocr_llm.parse_hocr()` keeps only `ocr_carea` blocks and, within them,
each word's bbox + `x_wconf`. Everything else Tesseract emits is
discarded. Measured on transcribe/work/ocr_llm/1990-10-10/p2/page.hocr
(one page):

  - 12 x `ocr_separator` -- siblings of ocr_carea, never selected by the
    existing XPath. One is bbox=[1947,171,1967,1849]: a 20px x 1678px
    *vertical rule*, i.e. an explicit column boundary. Others are
    horizontal rules.
  - 8 x `ocr_photo` -- including a real 520x524 photo region.
  - `x_size` on every line -- x-height in px, a direct font-size proxy.
    Body median 35, headlines up to 320 on that page.
  - `ocr_header` (10) and `ocr_textfloat` (26) line classes -- Tesseract's
    own heading/float classification. `ocr_caption` appears on other
    pages and maps straight onto item_ocr_block_spans.role='caption',
    which today is populated only by an LLM.
  - `ocr_par` paragraph structure, `baseline` slope, page `scan_res`.

That is exactly the signal `ocr-items.md` currently pays an LLM to
derive by *looking at the page image*. This module recovers it from the
.hocr files already on disk -- zero OCR, zero LLM.

Deliberately additive: this does NOT touch `ocr_llm.parse_hocr()` or any
existing table's existing columns. The current OCR+LLM route keeps
running unchanged while the scaled track is evaluated beside it.

Usage::

    python3 -m transcribe.scaled.hocr_parse backfill              # every page
    python3 -m transcribe.scaled.hocr_parse backfill --date 1990-10-10
    python3 -m transcribe.scaled.hocr_parse show 1990-10-10 --page 2
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import xml.etree.ElementTree as ET

from . import _support as _sup

HOCR_NS = {"x": "http://www.w3.org/1999/xhtml"}

_BBOX_RE = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_XSIZE_RE = re.compile(r"x_size ([\d.]+)")
_XASC_RE = re.compile(r"x_ascenders ([\d.]+)")
_XDESC_RE = re.compile(r"x_descenders ([\d.]+)")
_BASELINE_RE = re.compile(r"baseline (-?[\d.]+) (-?[\d.]+)")
_SCANRES_RE = re.compile(r"scan_res (\d+) (\d+)")

# Line-level classes Tesseract emits. ocr_line is ordinary body text;
# the other three are Tesseract's own layout judgements and are the
# interesting ones -- see module docstring.
LINE_CLASSES = ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat")

# Non-text block classes. These are siblings of ocr_carea and carry only
# a bbox (they are empty divs), which is precisely why the existing
# carea-only XPath misses them entirely.
REGION_CLASSES = ("ocr_separator", "ocr_photo")

# A separator whose bbox is at least this many times taller than it is
# wide counts as vertical. Rules are long and thin in one axis; the
# ratio test is orientation-agnostic and needs no page dimensions.
_ORIENT_RATIO = 2.0


def _f(rx, title, cast=float, default=None):
    m = rx.search(title or "")
    return cast(m.group(1)) if m else default


def _bbox(title: str | None) -> list[int]:
    m = _BBOX_RE.search(title or "")
    return [int(x) for x in m.groups()] if m else [0, 0, 0, 0]


def _orientation(x0: int, y0: int, x1: int, y1: int) -> str:
    """'vertical' | 'horizontal' | 'block'. A column rule is tall and
    thin, a horizontal divider is wide and thin; anything squarish is
    neither and gets 'block' so callers can ignore it rather than having
    to guess which axis a near-square region was meant to divide."""
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    if h >= w * _ORIENT_RATIO:
        return "vertical"
    if w >= h * _ORIENT_RATIO:
        return "horizontal"
    return "block"


def parse_hocr_full(hocr_path: str) -> dict:
    """Parse one .hocr into {page, blocks, lines, regions}.

    Block indices match `ocr_llm.parse_hocr()` exactly: both enumerate
    `ocr_carea` in document order, so `block_idx` joins cleanly onto the
    existing `page_ocr_blocks` rows. Non-carea regions are collected
    separately and do NOT consume a block_idx -- keeping that alignment
    is what makes this safe to run against already-ingested pages.
    """
    root = ET.parse(hocr_path).getroot()
    page_el = root.find(".//x:div[@class='ocr_page']", HOCR_NS)
    if page_el is None:
        raise ValueError(f"no ocr_page element in {hocr_path}")

    ptitle = page_el.get("title") or ""
    px0, py0, px1, py1 = _bbox(ptitle)
    sr = _SCANRES_RE.search(ptitle)
    page = {
        "width_px": px1 - px0,
        "height_px": py1 - py0,
        "scan_res_x": int(sr.group(1)) if sr else None,
        "scan_res_y": int(sr.group(2)) if sr else None,
    }
    pw, ph = max(1, page["width_px"]), max(1, page["height_px"])

    blocks, lines, regions = [], [], []
    block_idx = 0
    for child in page_el:
        cls = child.get("class") or ""
        bx0, by0, bx1, by1 = _bbox(child.get("title"))

        if cls in REGION_CLASSES:
            regions.append({
                "region_class": cls,
                "orientation": _orientation(bx0, by0, bx1, by1),
                "left_pct": _sup.px_to_pct(bx0, pw),
                "top_pct": _sup.px_to_pct(by0, ph),
                "right_pct": _sup.px_to_pct(bx1, pw),
                "bottom_pct": _sup.px_to_pct(by1, ph),
                "width_px": bx1 - bx0,
                "height_px": by1 - by0,
            })
            continue

        if cls != "ocr_carea":
            continue

        block_lines = []
        for par in child.findall(".//x:p[@class='ocr_par']", HOCR_NS):
            par_bbox = _bbox(par.get("title"))
            for span in par:
                scls = span.get("class") or ""
                if scls not in LINE_CLASSES:
                    continue
                t = span.get("title") or ""
                lx0, ly0, lx1, ly1 = _bbox(t)
                bl = _BASELINE_RE.search(t)
                words = span.findall(".//x:span[@class='ocrx_word']", HOCR_NS)
                text = " ".join(
                    (w.text or "").strip() for w in words if (w.text or "").strip()
                )
                block_lines.append({
                    "block_idx": block_idx,
                    "line_class": scls,
                    "left_pct": _sup.px_to_pct(lx0, pw),
                    "top_pct": _sup.px_to_pct(ly0, ph),
                    "right_pct": _sup.px_to_pct(lx1, pw),
                    "bottom_pct": _sup.px_to_pct(ly1, ph),
                    "x_size": _f(_XSIZE_RE, t),
                    "x_ascenders": _f(_XASC_RE, t),
                    "x_descenders": _f(_XDESC_RE, t),
                    "baseline_slope": float(bl.group(1)) if bl else None,
                    "par_top_pct": _sup.px_to_pct(par_bbox[1], ph),
                    "n_words": len(words),
                    "text": text,
                })

        sizes = [l["x_size"] for l in block_lines if l["x_size"]]
        blocks.append({
            "block_idx": block_idx,
            "block_class": cls,
            "x_size_median": round(statistics.median(sizes), 2) if sizes else None,
            "n_lines": len(block_lines),
        })
        lines.extend(block_lines)
        block_idx += 1

    return {"page": page, "blocks": blocks, "lines": lines, "regions": regions}


# --------------------------------------------------------------------
# DB
# --------------------------------------------------------------------

def ingest_page(conn, page_id: str, parsed: dict) -> dict:
    """Write lines + regions for one page and backfill the two new
    page_ocr_blocks columns. Idempotent -- deletes this page's own rows
    first, so a re-parse after a parser change is safe to just re-run."""
    conn.execute("DELETE FROM page_hocr_lines WHERE page_id=?", (page_id,))
    conn.execute("DELETE FROM page_hocr_regions WHERE page_id=?", (page_id,))

    block_ids = {
        r["block_idx"]: r["id"] for r in conn.execute(
            "SELECT block_idx, id FROM page_ocr_blocks WHERE page_id=?", (page_id,))
    }

    for b in parsed["blocks"]:
        bid = block_ids.get(b["block_idx"])
        if bid is None:
            continue  # block_idx not ingested by the existing route; skip, don't invent
        conn.execute(
            "UPDATE page_ocr_blocks SET block_class=?, x_size_median=? WHERE id=?",
            (b["block_class"], b["x_size_median"], bid),
        )

    n_lines = 0
    for l in parsed["lines"]:
        bid = block_ids.get(l["block_idx"])
        conn.execute(
            """INSERT INTO page_hocr_lines
               (id, page_id, page_ocr_block_id, block_idx, line_class,
                left_pct, top_pct, right_pct, bottom_pct,
                x_size, x_ascenders, x_descenders, baseline_slope,
                par_top_pct, n_words, text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, bid, l["block_idx"], l["line_class"],
             l["left_pct"], l["top_pct"], l["right_pct"], l["bottom_pct"],
             l["x_size"], l["x_ascenders"], l["x_descenders"], l["baseline_slope"],
             l["par_top_pct"], l["n_words"], l["text"]),
        )
        n_lines += 1

    for r in parsed["regions"]:
        conn.execute(
            """INSERT INTO page_hocr_regions
               (id, page_id, region_class, orientation,
                left_pct, top_pct, right_pct, bottom_pct, width_px, height_px)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_sup.new_uuid(), page_id, r["region_class"], r["orientation"],
             r["left_pct"], r["top_pct"], r["right_pct"], r["bottom_pct"],
             r["width_px"], r["height_px"]),
        )

    conn.execute(
        "UPDATE pages SET hocr_parsed_at=?, scan_res_x=?, scan_res_y=? WHERE id=?",
        (_sup.now_iso(), parsed["page"]["scan_res_x"], parsed["page"]["scan_res_y"],
         page_id),
    )
    conn.commit()

    seps = [r for r in parsed["regions"] if r["region_class"] == "ocr_separator"]
    return {
        "lines": n_lines,
        "blocks": len(parsed["blocks"]),
        "regions": len(parsed["regions"]),
        "vertical_rules": sum(1 for r in seps if r["orientation"] == "vertical"),
        "photos": sum(1 for r in parsed["regions"] if r["region_class"] == "ocr_photo"),
        "headers": sum(1 for l in parsed["lines"] if l["line_class"] == "ocr_header"),
        "captions": sum(1 for l in parsed["lines"] if l["line_class"] == "ocr_caption"),
    }


def pending_pages(conn, date: str | None = None, force: bool = False) -> list[dict]:
    sql = ("SELECT id, year, month, day, page, hocr_path FROM pages "
           "WHERE hocr_path IS NOT NULL")
    params: list = []
    if not force:
        sql += " AND hocr_parsed_at IS NULL"
    if date:
        y, m, d = (int(x) for x in date.split("-"))
        sql += " AND year=? AND month=? AND day=?"
        params += [y, m, d]
    sql += " ORDER BY year, month, day, page"
    return [dict(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _cmd_backfill(args):
    conn = _sup.open_connection()
    try:
        rows = pending_pages(conn, args.date, args.force)
        if not rows:
            print("Nothing to parse (use --force to re-parse already-parsed pages).")
            return
        totals = {k: 0 for k in
                  ("lines", "blocks", "regions", "vertical_rules", "photos",
                   "headers", "captions")}
        skipped = []
        for r in rows:
            if not os.path.isfile(r["hocr_path"]):
                skipped.append((r["page"], r["hocr_path"]))
                continue
            parsed = parse_hocr_full(r["hocr_path"])
            s = ingest_page(conn, r["id"], parsed)
            for k in totals:
                totals[k] += s[k]
            print(f"  {r['year']}-{r['month']:02d}-{r['day']:02d} p{r['page']}: "
                  f"{s['blocks']} blocks, {s['lines']} lines, "
                  f"{s['vertical_rules']} vertical rules, {s['photos']} photos")
        print(f"\n{len(rows) - len(skipped)} page(s) parsed.")
        print(f"  recovered: {totals['regions']} regions "
              f"({totals['vertical_rules']} vertical rules, {totals['photos']} photos), "
              f"{totals['headers']} ocr_header lines, {totals['captions']} ocr_caption lines")
        if skipped:
            print(f"  SKIPPED {len(skipped)} page(s) -- hocr file missing on disk:")
            for pg, path in skipped:
                print(f"    p{pg}: {path}")
    finally:
        conn.close()


def _cmd_show(args):
    conn = _sup.open_connection()
    try:
        y, m, d = (int(x) for x in args.date.split("-"))
        row = conn.execute(
            "SELECT id, hocr_path FROM pages WHERE year=? AND month=? AND day=? AND page=?",
            (y, m, d, args.page),
        ).fetchone()
        if row is None:
            print(f"No page row for {args.date} p{args.page}")
            return
        parsed = parse_hocr_full(row["hocr_path"])
        print(f"page: {parsed['page']}")
        print(f"blocks: {len(parsed['blocks'])}  lines: {len(parsed['lines'])}  "
              f"regions: {len(parsed['regions'])}")
        print("\nregions:")
        for r in parsed["regions"]:
            print(f"  {r['region_class']:15s} {r['orientation']:10s} "
                  f"{r['width_px']}x{r['height_px']}px  "
                  f"left={r['left_pct']}% top={r['top_pct']}%")
        by_class: dict[str, list[float]] = {}
        for l in parsed["lines"]:
            if l["x_size"]:
                by_class.setdefault(l["line_class"], []).append(l["x_size"])
        print("\nx_size by line class:")
        for cls, sizes in sorted(by_class.items()):
            sizes.sort()
            print(f"  {cls:15s} n={len(sizes):4d} min={sizes[0]:6.1f} "
                  f"median={sizes[len(sizes)//2]:6.1f} max={sizes[-1]:6.1f}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    pb = sub.add_parser("backfill", help="Parse .hocr files into the new tables")
    pb.add_argument("--date", help="YYYY-MM-DD (default: every unparsed page)")
    pb.add_argument("--force", action="store_true",
                    help="Re-parse pages already parsed (idempotent)")
    pb.set_defaults(func=_cmd_backfill)

    ps = sub.add_parser("show", help="Dump one page's parse without writing")
    ps.add_argument("date", help="YYYY-MM-DD")
    ps.add_argument("--page", type=int, required=True)
    ps.set_defaults(func=_cmd_show)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
