"""Write transcribe/terminology_review.json for the review-queue page.

Own compiled-stats store (same pattern as build_entities_stats.py) --
terminology_review.html only ever fetches this JSON, never queries
transcribe.db directly. Refreshed by the same LaunchAgent as the other
stats builds (see tools/refresh_ocr_llm_stats.py).

Run standalone:
    python3 -m transcribe.build_terminology_review_stats
"""

from __future__ import annotations

import json
import os

from . import db as _db
from . import merge_entity as _merge_entity

OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "terminology_review.json")

CONTEXT_LIMIT = 3  # mentions per entity -- enough to judge, not a full dump


def entity_context(conn, table: str, entity_id: str) -> list[dict]:
    """A few real mentions of one entity -- headline, date, and a
    text excerpt -- so a human reviewing a duplicate candidate on
    terminology_review.html can judge from actual context instead of
    two bare name strings (e.g. "Big Brothers/Big Sisters" vs "Big
    Brothers/Big Sisters of Lanark County" is unjudgeable without
    seeing what each mention is actually about)."""
    junction, fk, _namecol = _merge_entity.JUNCTIONS[table]
    rows = conn.execute(
        f"""SELECT m.mention_text, m.role, i.headline, i.year, i.month, i.day, i.page,
                   substr(i.full_text, 1, 400) AS excerpt
              FROM {junction} m JOIN items i ON i.id = m.item_id
             WHERE m.{fk}=?
          ORDER BY i.year, i.month, i.day
             LIMIT {CONTEXT_LIMIT}""",
        (entity_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_stats(conn) -> dict:
    rows = conn.execute(
        """SELECT id, entity_type, entity_id, other_entity_id, review_kind,
                  description, confidence, suggested_cli, status, raised_by,
                  raised_at, resolved_at, notes
           FROM terminology_reviews ORDER BY raised_at DESC"""
    ).fetchall()
    reviews = [dict(r) for r in rows]

    # Context is only useful for open reviews -- skip it for resolved
    # history to keep the JSON lean and avoid wasted queries.
    for r in reviews:
        if r["status"] != "open" or r["entity_type"] not in _merge_entity.JUNCTIONS:
            continue
        r["context_a"] = entity_context(conn, r["entity_type"], r["entity_id"]) if r["entity_id"] else []
        r["context_b"] = (entity_context(conn, r["entity_type"], r["other_entity_id"])
                          if r["other_entity_id"] else [])

    counts_by_kind = {}
    counts_by_status = {}
    for r in reviews:
        counts_by_kind[r["review_kind"]] = counts_by_kind.get(r["review_kind"], 0) + 1
        counts_by_status[r["status"]] = counts_by_status.get(r["status"], 0) + 1

    return {
        "generated_at": _db.now_iso(),
        "counts_by_kind": counts_by_kind,
        "counts_by_status": counts_by_status,
        "reviews": reviews,
    }


def main() -> int:
    conn = _db.open_connection()
    try:
        stats = build_stats(conn)
    finally:
        conn.close()

    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(stats, f)
    os.replace(tmp_path, OUT_PATH)
    print(f"wrote {OUT_PATH} ({len(stats['reviews'])} reviews)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
