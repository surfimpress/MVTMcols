"""Write transcribe/entities.json and transcribe/entities_context.json
for the entity table-view page.

Own compiled-stats store (like monitor.json), separate because
entities span both pipelines (pre-1980 items-classifier and the
OCR+LLM route) -- not scoped to either one. transcribe/entities.html
only ever reads these JSON files, never queries transcribe.db
directly. Refreshed by the same LaunchAgent as the OCR+LLM stats (see
tools/refresh_ocr_llm_stats.py) -- cheap at this corpus's current
scale (a few thousand rows total across 5 tables).

entities.json stays small and is re-fetched by the page every 30s
(POLL_MS) -- it's just the flat table data. entities_context.json is
the much heavier per-entity mention detail (up to 3 mentions with
excerpts per entity, reusing build_terminology_review_stats.entity_context
-- already fully generic, no review-specific coupling), written
separately so the frequently-polled file never carries it; the page
fetches this second file lazily, once per session, only when a detail
modal is actually opened.

Run standalone:
    python3 -m transcribe.build_entities_stats
"""

from __future__ import annotations

import json
import os

from . import build_terminology_review_stats as _review_stats
from . import db as _db
from . import entity_candidates as _entity_candidates

OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "entities.json")
CONTEXT_OUT_PATH = os.path.join(_db.REPO_ROOT, "transcribe", "entities_context.json")

TABLES = [
    ("people", "full_name", "item_people_mentions", "person_id"),
    ("organizations", "name", "item_organizations_mentions", "organization_id"),
    ("places", "name", "item_places_mentions", "place_id"),
    ("products", "name", "item_products_mentions", "product_id"),
    ("events", "name", "item_events_mentions", "event_id"),
]

# Extra per-table DB columns for the detail modal, beyond the universal
# notes/created_at every table carries -- deliberately NOT folded into
# entity_candidates.all_rows, which stays minimal on purpose (it also
# feeds the LLM-facing candidate lists trimmed down earlier this
# session to cut per-issue token cost). This is a browsing-UI-only
# fetch, so it can afford the full row.
EXTRA_COLS = {
    "people": ["first_name", "last_name", "title", "suffix"],
    "organizations": ["org_type"],
    "places": ["place_type", "parent_place_id"],
    "products": ["manufacturer", "product_type", "external_terminology",
                 "external_category", "external_uri", "external_reference"],
    "events": ["event_type", "year_known", "date_known"],
}


def build_stats(conn) -> tuple[dict, dict]:
    """Returns (entities_stats, context_stats) -- the two JSON payloads."""
    entities = []
    context = {}
    place_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM places")}
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
        # Every distinct (year, month, day, page) this entity is
        # mentioned on -- uncapped, integers only, no headline/excerpt
        # text. One JOIN query per table (same shape as mention_counts
        # above), not one query per entity. Cheap enough to live
        # directly in entities.json (unlike entity_context()'s text,
        # which stays in the separate lazy-loaded file) because the
        # year/month/day/page filter dropdowns need to work immediately
        # against the whole loaded list, not one entity at a time.
        dates_by_id: dict[str, set] = {}
        for r in conn.execute(
            f"SELECT m.{fk} AS id, i.year, i.month, i.day, i.page "
            f"FROM {junction} m JOIN items i ON i.id = m.item_id"
        ):
            dates_by_id.setdefault(r["id"], set()).add(
                (r["year"], r["month"], r["day"], r["page"]))

        meta_cols = ["notes", "created_at"] + EXTRA_COLS[table]
        meta_by_id = {
            row["id"]: {c: row[c] for c in meta_cols}
            for row in conn.execute(f"SELECT id, {', '.join(meta_cols)} FROM {table}")
        }
        if table == "places":
            for m in meta_by_id.values():
                m["parent_place_name"] = place_names.get(m.get("parent_place_id"))

        for r in rows:
            n = mention_counts.get(r["id"], 0)
            entities.append({
                "id": r["id"], "type": table, "name": r["name"],
                "first_seen": r["first_seen_date"], "last_seen": r["last_seen_date"],
                "mentions": n,
                "dates": sorted(dates_by_id.get(r["id"], set())),
                "meta": meta_by_id.get(r["id"], {}),
            })
            if n > 0:  # nothing to show for a zero-mention entity
                context[r["id"]] = {"context": _review_stats.entity_context(conn, table, r["id"])}

    counts = {}
    for table, *_ in TABLES:
        counts[table] = sum(1 for e in entities if e["type"] == table)

    entities_stats = {
        "generated_at": _db.now_iso(),
        "counts": counts,
        "entities": entities,
    }
    context_stats = {
        "generated_at": _db.now_iso(),
        "context": context,
    }
    return entities_stats, context_stats


def _atomic_write(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)


def main() -> int:
    conn = _db.open_connection()
    try:
        entities_stats, context_stats = build_stats(conn)
    finally:
        conn.close()

    _atomic_write(OUT_PATH, entities_stats)
    _atomic_write(CONTEXT_OUT_PATH, context_stats)
    print(f"wrote {OUT_PATH} ({len(entities_stats['entities'])} entities), "
          f"{CONTEXT_OUT_PATH} ({len(context_stats['context'])} with context)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
