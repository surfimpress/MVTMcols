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

entities.html's manual flagging UI produces the same top-level
{saved_at, approved, ignored} shape (always with an empty "ignored" --
there's no "ignore something I'm the one flagging" case) but its
"approved" entries reference an "id" with no matching terminology_reviews
row, since a human spotted it by browsing the full entity list, not
from an auto-raised review. Those entries carry entity_type/entity_id/
reason directly (self-sufficient) plus review_kind-specific fields --
other_entity_id for a merge, new_name for a rename, nothing extra for
a deletion; see _materialize_manual_review(), which raises a real
terminology_reviews row on the fly (provenance="human") before
applying it the same way as any other decision -- one code path for
every source after that point, including the one genuinely destructive
handler here (_apply_deletion): unlike merge, which preserves mention
data by moving it, deletion discards it.

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


def _materialize_manual_review(conn, review: dict) -> dict | None:
    """A decision whose id doesn't match any terminology_reviews row is
    either stale (the review was resolved by something else since the
    page was loaded -- a real failure) or was never a review to begin
    with: a human flagged it directly via entities.html, which has no
    pre-existing row to reference. Distinguish by whether the entry
    carries enough self-sufficient data to raise one now. Confidence is
    fixed at 1.0 throughout: a human explicitly chose this, unlike a
    heuristic guess or an LLM's self-reported score.

    Branches on review_kind -- entities.html can submit three shapes:
    duplicate_candidate (merge), name_too_specific (rename), deletion.
    """
    kind = review.get("review_kind")
    table = review.get("entity_type")
    if not table or not kind:
        return None

    if kind == "duplicate_candidate":
        if not (review.get("entity_id") and review.get("other_entity_id")):
            return None
        name_a = _entity_name(conn, table, review["entity_id"])
        name_b = _entity_name(conn, table, review["other_entity_id"])
        if not name_a or not name_b:
            return None  # ids don't resolve -- genuinely stale, not a manual entry
        reason = review.get("reason") or "flagged via entities.html"
        review_id = _db.raise_terminology_review(
            conn, entity_type=table, review_kind="duplicate_candidate",
            entity_id=review["entity_id"], other_entity_id=review["other_entity_id"],
            description=f"manual_match: {name_a!r} / {name_b!r} -- {reason}",
            raised_by="entities.html", provenance="human", confidence=1.0,
        )
    elif kind == "name_too_specific":
        new_name = review.get("new_name")
        if not (review.get("entity_id") and new_name):
            return None
        name = _entity_name(conn, table, review["entity_id"])
        if not name:
            return None
        reason = review.get("reason") or "renamed via entities.html"
        review_id = _db.raise_terminology_review(
            conn, entity_type=table, review_kind="name_too_specific",
            entity_id=review["entity_id"],
            description=f"manual_rename: {name!r} -> {new_name!r} -- {reason}",
            raised_by="entities.html", provenance="human", confidence=1.0,
            proposed_fix={"new_name": new_name},
        )
    elif kind == "deletion":
        if not review.get("entity_id"):
            return None
        name = _entity_name(conn, table, review["entity_id"])
        if not name:
            return None
        reason = review.get("reason") or "deleted via entities.html"
        review_id = _db.raise_terminology_review(
            conn, entity_type=table, review_kind="deletion",
            entity_id=review["entity_id"],
            description=f"manual_delete: {name!r} -- {reason}",
            raised_by="entities.html", provenance="human", confidence=1.0,
        )
    else:
        return None

    return dict(conn.execute(
        "SELECT * FROM terminology_reviews WHERE id=?", (review_id,)
    ).fetchone())


def _apply_duplicate(conn, review: dict, names: dict) -> str:
    table = review["entity_type"]
    keep, drop = names.get("entity_id"), names.get("other_entity_id")
    if keep is None or drop is None:
        # Not a failure -- the only way an id referenced by an open
        # review stops existing is merge_entity's own DELETE, so this
        # means an earlier decision in the same batch already merged
        # it away. The desired outcome (one row, not two) already
        # holds; resolve this review rather than bouncing it back to
        # 'open' for a human to look at again with nothing left to do.
        return "already resolved -- entity no longer exists (merged by an earlier decision in this batch)"
    result = _merge_entity.merge_entity(conn, table, keep, drop, alias=True)
    if result.get("already_merged"):
        return f"already resolved -- {result['kept']!r} and {result['dropped']!r} were already the same entity"
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
        # Same reasoning as _apply_duplicate's missing-id case -- the
        # only way this id stops existing is an earlier decision in
        # this batch already resolving it (a merge that consumed this
        # row). Resolve, don't bounce back to 'open'.
        return "already resolved -- entity no longer exists (renamed/merged by an earlier decision in this batch)"
    result = _cleanup.apply_genericize(conn, review["entity_type"], old_name, new_name)
    return f"genericized {result['dropped']!r} -> {result['kept']!r}"


def _apply_deletion(conn, review: dict, names: dict) -> str:
    """The one genuinely destructive handler here -- unlike merge,
    which preserves mention data by moving it, this discards it. Only
    reachable via a review a human explicitly raised (materialized
    on the fly, see _materialize_manual_review) or approved -- nothing
    auto-raises a 'deletion' review, so there's no automated path that
    could trigger this without a person having chosen it."""
    table = review["entity_type"]
    name = names.get("entity_id")
    if name is None:
        # Same reasoning as _apply_duplicate's missing-id case.
        return "already resolved -- entity no longer exists (merged/deleted by an earlier decision in this batch)"
    junction, fk, _namecol = _merge_entity.JUNCTIONS[table]
    n = conn.execute(f"DELETE FROM {junction} WHERE {fk}=?", (review["entity_id"],)).rowcount
    conn.execute(f"DELETE FROM {table} WHERE id=?", (review["entity_id"],))
    conn.commit()
    return f"deleted {name!r} ({n} mention(s) permanently removed)"


DISPATCH = {
    "duplicate_candidate": _apply_duplicate,
    "nomenclature_gap": _apply_nomenclature_gap,
    "name_too_specific": _apply_name_too_specific,
    "deletion": _apply_deletion,
}


def apply_decisions(conn, decisions: dict) -> dict:
    applied, failed, dismissed, rules_added = [], [], [], []

    for review in decisions.get("approved", []):
        live = conn.execute(
            "SELECT * FROM terminology_reviews WHERE id=?", (review["id"],)
        ).fetchone()
        if live is None:
            live = _materialize_manual_review(conn, review)
            if live is None:
                failed.append({"id": review["id"],
                               "error": "review id not found in DB and no self-sufficient "
                                        "entity_type/entity_id/other_entity_id to create one"})
                continue
        else:
            live = dict(live)
            # The decision file's entity_id/other_entity_id reflects
            # the human's actual keep/drop choice from
            # terminology_review.html's radio picker -- entity_id there
            # is NOT necessarily the same side as the DB row's own
            # entity_id (raised in whatever order the Python/LLM tier
            # happened to propose, see the 2026-08-10 Canadian-spelling
            # and genericization fixes for why that order is often
            # wrong). Confirmed live: ignoring this and blindly reusing
            # live's original order silently re-applied the wrong
            # direction even after the reviewer explicitly picked the
            # other side in the UI. Only ever REORDER the same known
            # pair here, never substitute a different id -- a decision
            # file can't smuggle in an id the DB review didn't already
            # have.
            file_pair = {review.get("entity_id"), review.get("other_entity_id")}
            live_pair = {live["entity_id"], live["other_entity_id"]}
            if (file_pair == live_pair and review.get("entity_id") != live["entity_id"]
                    and None not in file_pair):
                live["entity_id"], live["other_entity_id"] = (
                    review["entity_id"], review["other_entity_id"])
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
            # Mark by live["id"], not review["id"] -- for a materialized
            # manual review these differ (review["id"] is the client's
            # own bookkeeping id, live["id"] is the real row
            # raise_terminology_review() just created). Marking the
            # wrong one is a silent no-op in SQLite (UPDATE affecting 0
            # rows), which is exactly how this stayed 'open' forever
            # the first time -- confirmed live, not hypothesized.
            _mark(conn, live["id"], "applied", summary)
            applied.append({"id": review["id"], "db_id": live["id"], "summary": summary})
            if review.get("scope") == "always":
                key, fix = _rule_for(live, names, "approve")
                _cleanup.upsert_rule(conn, live["entity_type"], live["review_kind"], key,
                                     "approve", proposed_fix=fix,
                                     notes=f"created via terminology_review.html Save, review {review['id']}",
                                     provenance=live["provenance"])
                rules_added.append({"id": review["id"], "decision": "approve", "match_key": key})
        except Exception as e:
            _mark(conn, live["id"], "open", f"apply failed: {e}")
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
