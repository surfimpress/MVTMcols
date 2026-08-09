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
from . import workflow_usage as _wf_usage

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

        # Trusted token/time totals: sum of Workflow runs' own harness-
        # reported aggregates, plus manual single-agent dispatches
        # (run_id IS NULL) added on top since they aren't part of any
        # run. See schema.sql's comment on page_llm_calls -- this is
        # deliberately NOT a sum of page_llm_calls for run-covered
        # pages, because that reconstruction doesn't reconcile exactly
        # to the runs' own reported totals.
        run_usage = conn.execute(
            "SELECT coalesce(sum(total_tokens),0) AS tokens, "
            "coalesce(sum(duration_ms),0) AS ms FROM ocr_llm_runs "
            "WHERE year=? AND month=? AND day=?", (y, m, d),
        ).fetchone()
        manual_usage = conn.execute(
            "SELECT coalesce(sum(coalesce(tokens_in,0)+coalesce(tokens_out,0)),0) AS tokens, "
            "coalesce(sum(duration_ms),0) AS ms FROM page_llm_calls c "
            "JOIN pages p ON c.page_id=p.id "
            "WHERE c.run_id IS NULL AND p.year=? AND p.month=? AND p.day=?",
            (y, m, d),
        ).fetchone()

        issue_rows.append({
            "date": f"{y:04d}-{m:02d}-{d:02d}",
            "pages": iss["pages"],
            "items": items_n,
            "blocks_total": blocks["n"] or 0,
            "blocks_triaged": blocks["triaged"] or 0,
            "blocks_noise": blocks["noise"] or 0,
            "blocks_uncovered": uncovered,
            "llm_tokens": run_usage["tokens"] + manual_usage["tokens"],
            "llm_duration_ms": run_usage["ms"] + manual_usage["ms"],
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

    recent_llm_calls = conn.execute(
        "SELECT p.year, p.month, p.day, p.page, c.kind, c.tokens_in, c.tokens_out, "
        "c.tool_calls, c.duration_ms, c.created_at FROM page_llm_calls c "
        "JOIN pages p ON c.page_id=p.id ORDER BY c.created_at DESC LIMIT 20"
    ).fetchall()

    totals = conn.execute(
        "SELECT (SELECT count(*) FROM pages) AS pages, "
        "(SELECT count(*) FROM page_ocr_blocks) AS blocks, "
        "(SELECT count(*) FROM items WHERE id IN (SELECT item_id FROM items_ocr_ext)) AS items"
    ).fetchone()
    llm_totals = conn.execute(
        "SELECT (SELECT coalesce(sum(total_tokens),0) FROM ocr_llm_runs) "
        "+ (SELECT coalesce(sum(coalesce(tokens_in,0)+coalesce(tokens_out,0)),0) "
        "   FROM page_llm_calls WHERE run_id IS NULL) AS tokens, "
        "(SELECT coalesce(sum(duration_ms),0) FROM ocr_llm_runs) "
        "+ (SELECT coalesce(sum(duration_ms),0) FROM page_llm_calls WHERE run_id IS NULL) AS duration_ms"
    ).fetchone()

    return {
        "generated_at": _db.now_iso(),
        "totals": {**dict(totals), **dict(llm_totals)},
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
        # Per-page/per-kind breakdown, reconstructed from agent
        # transcripts -- useful for relative page-to-page comparison
        # but does NOT sum exactly to `totals.tokens` (confirmed
        # 2026-08-09: reconstructed sums ran ~70-80% of the harness's
        # own reported run totals across two checked runs; formula
        # not fully understood). The monitor must caption this, not
        # present it as the authoritative total.
        "recent_llm_calls": [
            {
                "date": f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}",
                "page": r["page"], "kind": r["kind"],
                "tokens": (r["tokens_in"] or 0) + (r["tokens_out"] or 0),
                "tool_calls": r["tool_calls"],
                "duration_ms": r["duration_ms"],
            }
            for r in recent_llm_calls
        ],
        # Live progress for any in-flight run -- reconstructed from
        # journal.jsonl + agent transcripts, not agent self-reporting
        # (ocr-cleanup/ocr-items are Read-only by design). Empty list
        # when nothing is currently running.
        "active_runs": _wf_usage.find_active_runs(),
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
