"""Write transcribe/ocr_llm_stats.json for the OCR+LLM route's monitor.

Own compiled-stats store, deliberately separate from repair_stats.json
(that one belongs to the column-cut pipeline; this route has different
shape data -- OCR blocks/confidence, item/entity counts, no column
concept at all). transcribe_ocr_llm_monitor.html only ever reads this
JSON file, never queries transcribe.db directly, so viewing the
monitor never touches the database. The only writer is this script,
invoked on a slow, fixed interval by a LaunchAgent (see
tools/refresh_ocr_llm_stats.py) -- not by page-completion events, and
not by the monitor page itself. This mirrors build_repair_stats.py's
own documented lesson (its docstring: an earlier version of that
monitor was invoked on every single page-completion event by a live
loop, which is exactly the "drag on database calls" failure mode this
route's monitor is deliberately built to avoid from the start.

A handful of GROUP BY / count queries against transcribe.db; cheap at
this corpus's current scale (low thousands of rows). If per-issue rows
grow into the tens of thousands, revisit whether any of this needs the
corpus_totals_cache.json-style hour-long caching build_repair_stats.py
uses -- not needed yet, so not added yet.

Run standalone:
    python3 -m transcribe.build_ocr_llm_stats
"""

from __future__ import annotations

import json
import os

from . import db as _db

OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "ocr_llm_stats.json")


def _rows_to_dicts(rows):
    return [dict(r) for r in rows]


def build_stats(conn) -> dict:
    issues = conn.execute(
        "SELECT year, month, day, count(*) AS pages, "
        "min(created_at) AS first_rendered, max(created_at) AS last_rendered "
        "FROM pages GROUP BY year, month, day ORDER BY year, month, day"
    ).fetchall()

    issue_rows = []
    for iss in issues:
        y, m, d = iss["year"], iss["month"], iss["day"]
        items_n = conn.execute(
            "SELECT count(*) AS n FROM items WHERE year=? AND month=? AND day=? "
            "AND id IN (SELECT item_id FROM items_ocr_ext)",
            (y, m, d),
        ).fetchone()["n"]
        blocks = conn.execute(
            "SELECT count(*) AS n, "
            "sum(triaged) AS triaged, "
            "sum(CASE WHEN cleanup_status='noise' THEN 1 ELSE 0 END) AS noise "
            "FROM page_ocr_blocks b JOIN pages p ON b.page_id=p.id "
            "WHERE p.year=? AND p.month=? AND p.day=?",
            (y, m, d),
        ).fetchone()
        # Coverage: blocks with no item_ocr_block_spans row at all.
        uncovered = conn.execute(
            "SELECT count(*) AS n FROM page_ocr_blocks b "
            "JOIN pages p ON b.page_id=p.id "
            "WHERE p.year=? AND p.month=? AND p.day=? AND b.id NOT IN "
            "(SELECT page_ocr_block_id FROM item_ocr_block_spans)",
            (y, m, d),
        ).fetchone()["n"]

        issue_rows.append({
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "pages": iss["pages"],
            "items": items_n,
            "blocks_total": blocks["n"] or 0,
            "blocks_triaged": blocks["triaged"] or 0,
            "blocks_noise": blocks["noise"] or 0,
            "blocks_uncovered": uncovered,
            "first_rendered": iss["first_rendered"],
            "last_rendered": iss["last_rendered"],
        })

    mention_counts = {}
    for table, junction in (
        ("people", "item_people_mentions"),
        ("organizations", "item_organizations_mentions"),
        ("places", "item_places_mentions"),
        ("products", "item_products_mentions"),
        ("events", "item_events_mentions"),
    ):
        row = conn.execute(f"SELECT count(*) AS n FROM {junction}").fetchone()
        mention_counts[table] = row["n"]

    recent_pages = conn.execute(
        "SELECT year, month, day, page, created_at, hocr_word_count, "
        "hocr_mean_confidence FROM pages ORDER BY created_at DESC LIMIT 20"
    ).fetchall()

    totals = conn.execute(
        "SELECT (SELECT count(*) FROM pages) AS pages, "
        "(SELECT count(*) FROM page_ocr_blocks) AS blocks, "
        "(SELECT count(*) FROM items WHERE id IN (SELECT item_id FROM items_ocr_ext)) AS items"
    ).fetchone()

    return {
        "generated_at": _db.now_iso(),
        "totals": dict(totals),
        "mention_counts": mention_counts,
        "issues": issue_rows,
        "recent_pages": [
            {
                "date": f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}",
                "page": r["page"],
                "created_at": r["created_at"],
                "hocr_word_count": r["hocr_word_count"],
                "hocr_mean_confidence": r["hocr_mean_confidence"],
            }
            for r in recent_pages
        ],
    }


def main() -> int:
    conn = _db.open_connection()
    try:
        stats = build_stats(conn)
    finally:
        conn.close()

    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(stats, f, indent=2)
    os.replace(tmp_path, OUT_PATH)  # atomic -- monitor never sees a partial write
    print(f"wrote {OUT_PATH} ({stats['totals']['pages']} pages, "
          f"{stats['totals']['items']} items)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
