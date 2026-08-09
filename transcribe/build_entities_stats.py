"""Write transcribe/entities.json for the entity table-view page.

Own compiled-stats store (like ocr_llm_stats.json), separate because
entities span both pipelines (pre-1980 items-classifier and the
OCR+LLM route) -- not scoped to either one. transcribe/entities.html
only ever reads this JSON, never queries transcribe.db directly.
Refreshed by the same LaunchAgent as the OCR+LLM stats (see
tools/refresh_ocr_llm_stats.py) -- cheap at this corpus's current
scale (a few thousand rows total across 5 tables).

Run standalone:
    python3 -m transcribe.build_entities_stats
"""

from __future__ import annotations

import json
import os

from . import db as _db
from . import entity_candidates as _entity_candidates

OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "entities.json")

TABLES = [
    ("people", "full_name", "item_people_mentions", "person_id"),
    ("organizations", "name", "item_organizations_mentions", "organization_id"),
    ("places", "name", "item_places_mentions", "place_id"),
    ("products", "name", "item_products_mentions", "product_id"),
    ("events", "name", "item_events_mentions", "event_id"),
]


def build_stats(conn) -> dict:
    entities = []
    for table, namecol, junction, fk in TABLES:
        # Same base row-fetch the item-markup pass's candidate-list
        # prefetch uses (entity_candidates.all_rows) -- one query per
        # table, uncapped here since this view wants everything, not
        # a prompt-sized sample.
        rows = _entity_candidates.all_rows(conn, table, namecol)
        mention_counts = {
            r["id"]: r["n"] for r in conn.execute(
                f"SELECT {fk} AS id, count(*) AS n FROM {junction} GROUP BY {fk}")
        }
        for r in rows:
            entities.append({
                "id": r["id"], "type": table, "name": r["name"],
                "first_seen": r["first_seen_date"], "last_seen": r["last_seen_date"],
                "mentions": mention_counts.get(r["id"], 0),
            })

    counts = {}
    for table, *_ in TABLES:
        counts[table] = sum(1 for e in entities if e["type"] == table)

    return {
        "generated_at": _db.now_iso(),
        "counts": counts,
        "entities": entities,
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
    print(f"wrote {OUT_PATH} ({len(stats['entities'])} entities)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
