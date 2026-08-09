"""Apply a decisions file downloaded from terminology_review.html.

The review page never talks to the database directly (see its own
footer note) -- Approve/Ignore clicks live in browser localStorage
until "Save decisions" downloads one JSON file:

    {"saved_at": "...", "approved": [{"id", "review_kind",
     "entity_type", "entity_id", "other_entity_id", "description",
     "suggested_cli"}, ...], "ignored": [...]}

This script is the other half: read that file, dispatch each
approved review to the right apply_* function by review_kind (using
its structured fields -- entity_id, other_entity_id, a fresh DB
lookup for current names -- not by re-parsing suggested_cli as a
shell command, which is a display string for humans, not something
this script should be shlex-parsing), mark it applied or leave it
open with a note if it fails, and mark every ignored review
dismissed. Nothing here is destructive beyond what the review already
proposed and a human already clicked Approve on.

Usage::

    python3 -m transcribe.apply_terminology_decisions \
        ~/Downloads/terminology_decisions_2026-08-09T....json
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db as _db
from . import merge_entity as _merge_entity
from . import terminology_cleanup as _cleanup


def _mark(conn, review_id: str, status: str, note: str | None = None) -> None:
    conn.execute(
        "UPDATE terminology_reviews SET status=?, resolved_at=?, "
        "notes=COALESCE(notes || '; ', '') || ? WHERE id=?",
        (status, _db.now_iso(), note or status, review_id),
    )
    conn.commit()


def _apply_duplicate(conn, review: dict) -> str:
    table = review["entity_type"]
    namecol = _cleanup.NAME_COL[table]
    keep = conn.execute(
        f"SELECT {namecol} AS name FROM {table} WHERE id=?", (review["entity_id"],)
    ).fetchone()
    drop = conn.execute(
        f"SELECT {namecol} AS name FROM {table} WHERE id=?", (review["other_entity_id"],)
    ).fetchone()
    if keep is None or drop is None:
        raise ValueError("one or both entities no longer exist -- already merged "
                         "by an earlier decision in this batch?")
    result = _merge_entity.merge_entity(conn, table, keep["name"], drop["name"], alias=True)
    return (f"merged {result['dropped']!r} into {result['kept']!r} "
            f"({result['moved']} mentions moved)")


def _apply_nomenclature_gap(conn, review: dict) -> str:
    fix = json.loads(review["proposed_fix_json"] or "{}")
    uri = fix.get("external_uri")
    if not uri:
        raise ValueError("no external_uri in proposed_fix_json")
    result = _cleanup.apply_nomenclature(conn, review["entity_id"], uri)
    return f"{result['product']!r} -> product_type={result['product_type']!r}"


def _apply_name_too_specific(conn, review: dict) -> str:
    fix = json.loads(review["proposed_fix_json"] or "{}")
    new_name = fix.get("new_name")
    if not new_name:
        raise ValueError("no new_name in proposed_fix_json")
    table = review["entity_type"]
    namecol = _cleanup.NAME_COL[table]
    row = conn.execute(
        f"SELECT {namecol} AS name FROM {table} WHERE id=?", (review["entity_id"],)
    ).fetchone()
    if row is None:
        raise ValueError("entity no longer exists -- already renamed by an "
                         "earlier decision in this batch?")
    result = _cleanup.apply_genericize(conn, table, row["name"], new_name)
    return f"genericized {result['dropped']!r} -> {result['kept']!r}"


DISPATCH = {
    "duplicate_candidate": _apply_duplicate,
    "nomenclature_gap": _apply_nomenclature_gap,
    "name_too_specific": _apply_name_too_specific,
}


def apply_decisions(conn, decisions: dict) -> dict:
    applied, failed, dismissed = [], [], []

    for review in decisions.get("approved", []):
        # Re-fetch the live row -- review_kind here is trusted metadata
        # the page copied from terminology_review.json, but proposed_fix_json
        # wasn't included in that copy (see the page's Save handler), so
        # pull the authoritative row from the DB by id.
        live = conn.execute(
            "SELECT * FROM terminology_reviews WHERE id=?", (review["id"],)
        ).fetchone()
        if live is None:
            failed.append({"id": review["id"], "error": "review id not found in DB"})
            continue
        live = dict(live)
        if live["status"] != "open":
            failed.append({"id": review["id"], "error": f"already {live['status']}, skipped"})
            continue
        handler = DISPATCH.get(live["review_kind"])
        if handler is None:
            failed.append({"id": review["id"],
                          "error": f"no apply handler for review_kind={live['review_kind']!r}"})
            continue
        try:
            summary = handler(conn, live)
            _mark(conn, review["id"], "applied", summary)
            applied.append({"id": review["id"], "summary": summary})
        except Exception as e:
            _mark(conn, review["id"], "open", f"apply failed: {e}")
            failed.append({"id": review["id"], "error": str(e)})

    for review in decisions.get("ignored", []):
        _mark(conn, review["id"], "dismissed")
        dismissed.append({"id": review["id"]})

    return {"applied": applied, "failed": failed, "dismissed": dismissed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions_path")
    args = parser.parse_args()

    with open(args.decisions_path) as f:
        decisions = json.load(f)

    conn = _db.open_connection()
    try:
        result = apply_decisions(conn, decisions)
    finally:
        conn.close()

    for a in result["applied"]:
        print(f"  applied: {a['summary']}")
    for f in result["failed"]:
        print(f"  FAILED {f['id']}: {f['error']}")
    print(f"\n{len(result['applied'])} applied, {len(result['failed'])} failed, "
          f"{len(result['dismissed'])} dismissed")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
