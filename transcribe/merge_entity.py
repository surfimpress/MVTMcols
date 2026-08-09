"""Merge two entity rows of the same type -- one canonical, one dropped.

Repoints every mention junction row from the dropped entity to the
kept one (de-duplicating any (item_id, entity_id, span_start)
collision rather than crashing on the PK), widens first_seen_date/
last_seen_date via MIN/MAX across both, optionally records the
dropped name as a noted alias, then deletes the dropped row.

Built after doing this by hand five times in one session (Almonte
Gazette/The Almonte Gazette, Bell/Bell Canada, C.P.R./Canadian
Pacific Railway, G. L. Comba School/G.L. Comba Public School,
Heritage IDA/Heritage IDA Drugs) -- same script rewritten each time.

Usage::

    python3 -m transcribe.merge_entity organizations \
        "Canadian Pacific Railway" "C.P.R." --alias
"""

from __future__ import annotations

import argparse
import sys

from . import db as _db

JUNCTIONS = {
    "people": ("item_people_mentions", "person_id", "full_name"),
    "organizations": ("item_organizations_mentions", "organization_id", "name"),
    "places": ("item_places_mentions", "place_id", "name"),
    "products": ("item_products_mentions", "product_id", "name"),
    "events": ("item_events_mentions", "event_id", "name"),
}


def merge_entity(conn, table: str, keep_name: str, drop_name: str,
                  alias: bool = False) -> dict:
    if table not in JUNCTIONS:
        raise ValueError(f"unknown entity type {table!r}, must be one of {list(JUNCTIONS)}")
    junction, fk, namecol = JUNCTIONS[table]

    keep = conn.execute(f"SELECT * FROM {table} WHERE {namecol}=?", (keep_name,)).fetchone()
    drop = conn.execute(f"SELECT * FROM {table} WHERE {namecol}=?", (drop_name,)).fetchone()
    if keep is None:
        raise ValueError(f"no {table} row named {keep_name!r} -- check exact spelling on entities.html")
    if drop is None:
        raise ValueError(f"no {table} row named {drop_name!r} -- check exact spelling on entities.html")
    if keep["id"] == drop["id"]:
        raise ValueError(f"{keep_name!r} and {drop_name!r} are already the same row")

    fs = [d for d in (keep["first_seen_date"], drop["first_seen_date"]) if d]
    ls = [d for d in (keep["last_seen_date"], drop["last_seen_date"]) if d]
    notes = (keep["notes"] or "").strip()
    if alias:
        alias_note = f"alias: {drop_name}"
        if alias_note not in notes:
            notes = (notes + "; " + alias_note).strip("; ")
    conn.execute(
        f"UPDATE {table} SET first_seen_date=?, last_seen_date=?, notes=? WHERE id=?",
        (min(fs) if fs else None, max(ls) if ls else None, notes, keep["id"]),
    )

    rows = conn.execute(f"SELECT * FROM {junction} WHERE {fk}=?", (drop["id"],)).fetchall()
    moved, collided = 0, 0
    for r in rows:
        exists = conn.execute(
            f"SELECT 1 FROM {junction} WHERE item_id=? AND {fk}=? AND span_start=?",
            (r["item_id"], keep["id"], r["span_start"]),
        ).fetchone()
        if exists:
            conn.execute(
                f"DELETE FROM {junction} WHERE item_id=? AND {fk}=? AND span_start=?",
                (r["item_id"], drop["id"], r["span_start"]),
            )
            collided += 1
        else:
            conn.execute(
                f"UPDATE {junction} SET {fk}=? WHERE item_id=? AND {fk}=? AND span_start=?",
                (keep["id"], r["item_id"], drop["id"], r["span_start"]),
            )
            moved += 1

    conn.execute(f"DELETE FROM {table} WHERE id=?", (drop["id"],))
    conn.commit()
    return {"kept": keep_name, "kept_id": keep["id"], "dropped": drop_name,
            "moved": moved, "collided": collided, "notes": notes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("type", choices=list(JUNCTIONS))
    parser.add_argument("keep_name", help="Canonical name to keep")
    parser.add_argument("drop_name", help="Duplicate name to merge in and delete")
    parser.add_argument("--alias", action="store_true",
                        help="Record drop_name as a noted alias on the kept row")
    args = parser.parse_args()

    conn = _db.open_connection()
    try:
        result = merge_entity(conn, args.type, args.keep_name, args.drop_name, args.alias)
    finally:
        conn.close()

    print(f"kept {result['kept']!r} ({result['kept_id']}), dropped {result['dropped']!r} -- "
          f"moved {result['moved']} mentions, {result['collided']} collisions deduped")
    if result["notes"]:
        print(f"notes: {result['notes']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
