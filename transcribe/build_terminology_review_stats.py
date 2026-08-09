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

OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "terminology_review.json")


def build_stats(conn) -> dict:
    rows = conn.execute(
        """SELECT id, entity_type, entity_id, other_entity_id, review_kind,
                  description, confidence, suggested_cli, status, raised_by,
                  raised_at, resolved_at, notes
           FROM terminology_reviews ORDER BY raised_at DESC"""
    ).fetchall()
    reviews = [dict(r) for r in rows]

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
