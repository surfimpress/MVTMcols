"""Apply a decisions file downloaded from terminology_review.html.

The review page never talks to the database directly (see its own
footer note) -- Approve/Ignore clicks live in browser localStorage
until "Save decisions" downloads one JSON file:

    {"saved_at": "...", "approved": [{"id", "review_kind",
     "entity_type", "entity_id", "other_entity_id", "description",
     "suggested_cli", "scope": "once"|"always"}, ...], "ignored": [...]}

This script is the other half: read that file, dispatch each
approved review to the right apply_* function by review_kind (using
its structured fields -- entity_id, other_entity_id, a fresh DB
lookup for current names -- not by re-parsing suggested_cli as a
shell command, which is a display string for humans, not something
this script should be shlex-parsing), mark it applied or leave it
open with a note if it fails, and mark every ignored review
dismissed. Nothing here is destructive beyond what the review already
proposed and a human already clicked Approve on.

`scope: "always"` additionally writes a permanent, name-keyed rule to
terminology_rules (see schema.sql's comment there) -- future runs of
terminology_cleanup.py will auto-apply (or auto-skip) matching cases
without raising a review at all. The review page gates creating one
of these behind its own confirm() dialog before it's ever included
in a decisions file, since the blast radius is different: a "once"
decision affects the two entities in front of you; an "always" rule
affects every future match, unattended.

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


def _entity_name(conn, table: str, entity_id: str) -> str | None:
    namecol = _cleanup.NAME_COL[table]
    row = conn.execute(f"SELECT {namecol} AS name FROM {table} WHERE id=?", (entity_id,)).fetchone()
    return row["name"] if row else None


def _apply_duplicate(conn, review: dict, names: dict) -> str:
    table = review["entity_type"]
    keep, drop = names.get("entity_id"), names.get("other_entity_id")
    if keep is None or drop is None:
        raise ValueError("one or both entities no longer exist -- already merged "
                         "by an earlier decision in this batch?")
    result = _merge_entity.merge_entity(conn, table, keep, drop, alias=True)
    return (f"merged {result['dropped']!r} into {result['kept']!r} "
            f"({result['moved']} mentions moved)")


def _apply_nomenclature_gap(conn, review: dict, names: dict) -> str:
    fix = json.loads(review["proposed_fix_json"] or "{}")
    uri = fix.get("external_uri")
    if not uri:
        raise ValueError("no external_uri in proposed_fix_json")
    result = _cleanup.apply_nomenclature(conn, review["entity_id"], uri)
    return f"{result['product']!r} -> product_type={result['product_type']!r}"


def _apply_name_too_specific(conn, review: dict, names: dict) -> str:
    fix = json.loads(review["proposed_fix_json"] or "{}")
    new_name = fix.get("new_name")
    if not new_name:
        raise ValueError("no new_name in proposed_fix_json")
    old_name = names.get("entity_id")
    if old_name is None:
        raise ValueError("entity no longer exists -- already renamed by an "
                         "earlier decision in this batch?")
    result = _cleanup.apply_genericize(conn, review["entity_type"], old_name, new_name)
    return f"genericized {result['dropped']!r} -> {result['kept']!r}"


DISPATCH = {
    "duplicate_candidate": _apply_duplicate,
    "nomenclature_gap": _apply_nomenclature_gap,
    "name_too_specific": _apply_name_too_specific,
}


def apply_decisions(conn, decisions: dict) -> dict:
    applied, failed, dismissed, rules_added = [], [], [], []

    for review in decisions.get("approved", []):
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
        # Capture names BEFORE applying -- a duplicate merge deletes the
        # 'drop' row, so its name is unrecoverable afterward.
        names = {
            "entity_id": _entity_name(conn, live["entity_type"], live["entity_id"]) if live["entity_id"] else None,
            "other_entity_id": _entity_name(conn, live["entity_type"], live["other_entity_id"]) if live["other_entity_id"] else None,
        }
        try:
            summary = handler(conn, live, names)
            _mark(conn, review["id"], "applied", summary)
            applied.append({"id": review["id"], "summary": summary})
            if review.get("scope") == "always":
                key, fix = _rule_for(live, names, "approve")
                _cleanup.upsert_rule(conn, live["entity_type"], live["review_kind"], key,
                                     "approve", proposed_fix=fix,
                                     notes=f"created via terminology_review.html Save, review {review['id']}",
                                     provenance=live["provenance"])
                rules_added.append({"id": review["id"], "decision": "approve", "match_key": key})
        except Exception as e:
            _mark(conn, review["id"], "open", f"apply failed: {e}")
            failed.append({"id": review["id"], "error": str(e)})

    for review in decisions.get("ignored", []):
        live = conn.execute(
            "SELECT * FROM terminology_reviews WHERE id=?", (review["id"],)
        ).fetchone()
        _mark(conn, review["id"], "dismissed")
        dismissed.append({"id": review["id"]})
        if live is not None and review.get("scope") == "always":
            live = dict(live)
            names = {
                "entity_id": _entity_name(conn, live["entity_type"], live["entity_id"]) if live["entity_id"] else None,
                "other_entity_id": _entity_name(conn, live["entity_type"], live["other_entity_id"]) if live["other_entity_id"] else None,
            }
            key, _fix = _rule_for(live, names, "ignore")
            if key:
                _cleanup.upsert_rule(conn, live["entity_type"], live["review_kind"], key,
                                     "ignore", proposed_fix=None,
                                     notes=f"created via terminology_review.html Save, review {review['id']}",
                                     provenance=live["provenance"])
                rules_added.append({"id": review["id"], "decision": "ignore", "match_key": key})

    return {"applied": applied, "failed": failed, "dismissed": dismissed, "rules_added": rules_added}


def _rule_for(live: dict, names: dict, decision: str) -> tuple[str | None, dict | None]:
    """Build the (match_key, proposed_fix) pair for an 'always' rule
    from an already-resolved review + its pre-captured entity names."""
    review_kind = live["review_kind"]
    if review_kind == "duplicate_candidate":
        name_a, name_b = names.get("entity_id"), names.get("other_entity_id")
        if not name_a or not name_b:
            return None, None
        key = _cleanup.rule_match_key(review_kind, name_a, name_b)
        fix = {"keep": name_a, "drop": name_b} if decision == "approve" else None
        return key, fix
    name = names.get("entity_id")
    if not name:
        return None, None
    key = _cleanup.rule_match_key(review_kind, name)
    fix = json.loads(live["proposed_fix_json"] or "{}") if decision == "approve" else None
    return key, fix


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
    for r in result["rules_added"]:
        print(f"  rule added ({r['decision']}, permanent): {r['match_key']}")
    print(f"\n{len(result['applied'])} applied, {len(result['failed'])} failed, "
          f"{len(result['dismissed'])} dismissed, {len(result['rules_added'])} rule(s) added")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
