"""Terminology cleanup: an independent, repeatable pass over the
entity registry (people/organizations/places/products/events) --
finds duplicate candidates, fills Nomenclature gaps for products, and
flags names that are too specific to be reusable across issues (see
items-classifier.md's "Prefer names that will recur" /  "Picking the
right altitude for `name`" sections -- this module is the automated,
repeatable version of the manual cleanup pass done 2026-08-09 that
those sections document).

Same non-mutating-by-default philosophy as `repairs`
(transcribe/CLAUDE.md), but in its own table -- terminology_reviews,
not repairs -- because this is the entity-registry domain, not the
transcript/cutting domain. Two kinds of finding:

  - Safe, mechanical, auto-applied: filling in Nomenclature fields for
    a product when the match's category already equals the existing
    product_type (pure enrichment, doesn't change any classification).
  - Everything else: written to terminology_reviews as an open review
    with a suggested_cli to run if a human confirms it -- duplicate
    merges and name genericization both restructure real data (moving
    mentions, deleting rows), so per this project's standing "never
    delete before the replacement is confirmed" rule, nothing here
    auto-merges or auto-renames.

Usage::

    python3 -m transcribe.terminology_cleanup run-all
    python3 -m transcribe.terminology_cleanup duplicates organizations
    python3 -m transcribe.terminology_cleanup nomenclature-gaps
    python3 -m transcribe.terminology_cleanup generic-names
    python3 -m transcribe.terminology_cleanup apply-genericize products \
        "White Swan Baking Powder" "Baking Powder"

Intended to run ad hoc today; a LaunchAgent (mirroring
com.mvtm.db_backup's daily pattern) can call `run-all` on a schedule
once the review queue's signal-to-noise has been checked over a few
ad hoc runs -- not wired up yet, see the bottom of this docstring's
sibling note in CLAUDE.md.
"""

from __future__ import annotations

import argparse
import re

from . import db as _db
from . import merge_entity as _merge_entity
from . import nomenclature as _nomenclature

RAISED_BY = "terminology_cleanup"

NAME_COL = {
    "people": "full_name",
    "organizations": "name",
    "places": "name",
    "products": "name",
    "events": "name",
}

_STOPWORDS = {"the", "ltd", "inc", "co", "company", "corp", "corporation", "limited"}


def _normalize_for_dedup(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[.,'\"()]", "", s)
    words = [w for w in s.split() if w not in _STOPWORDS]
    return " ".join(words).strip()


# --------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------

def _existing_open_pair(conn, entity_type: str, id_a: str, id_b: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM terminology_reviews
           WHERE entity_type=? AND review_kind='duplicate_candidate' AND status='open'
             AND ((entity_id=? AND other_entity_id=?) OR (entity_id=? AND other_entity_id=?))
           LIMIT 1""",
        (entity_type, id_a, id_b, id_b, id_a),
    ).fetchone()
    return row is not None


def find_duplicates(conn, table: str) -> list[dict]:
    """Bucketed by normalized-name first character to keep this well
    under O(n^2) at this corpus's scale -- true duplicates almost
    always share a first character after stopword-stripping."""
    namecol = NAME_COL[table]
    rows = conn.execute(f"SELECT id, {namecol} AS name FROM {table}").fetchall()
    normed = [(r["id"], r["name"], _normalize_for_dedup(r["name"])) for r in rows]
    normed = [t for t in normed if t[2]]

    buckets: dict[str, list] = {}
    for entry in normed:
        buckets.setdefault(entry[2][0], []).append(entry)

    raised = []
    for bucket in buckets.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                id_a, name_a, norm_a = bucket[i]
                id_b, name_b, norm_b = bucket[j]
                if norm_a == norm_b:
                    kind, confidence = "exact_normalized_match", 0.9
                elif len(norm_a) >= 4 and len(norm_b) >= 4 and (
                        norm_a in norm_b or norm_b in norm_a):
                    # Genuinely low-precision: tested against the live
                    # corpus 2026-08-09 and confirmed noisy -- a bare
                    # given name, surname, or place-name prefix ("Elizabeth",
                    # "Naismith", "Lanark") is a substring of many real,
                    # unrelated longer entities, not a truncated alias of
                    # any one of them. No cheap mechanical filter found
                    # that reliably separates that from real cases (Bell/
                    # Bell Canada). Low confidence is the honest signal;
                    # this tier needs real human judgment, not a rubber
                    # stamp -- never auto-apply from this kind alone.
                    kind, confidence = "substring_containment", 0.3
                else:
                    continue
                if _existing_open_pair(conn, table, id_a, id_b):
                    continue
                review_id = _db.raise_terminology_review(
                    conn, entity_type=table, review_kind="duplicate_candidate",
                    entity_id=id_a, other_entity_id=id_b, confidence=confidence,
                    description=f"{kind}: {name_a!r} / {name_b!r}",
                    raised_by=RAISED_BY,
                    suggested_cli=(
                        f"python3 -m transcribe.merge_entity {table} "
                        f"{name_a!r} {name_b!r} --alias"),
                )
                raised.append({"id": review_id, "a": name_a, "b": name_b, "kind": kind})
    return raised


# --------------------------------------------------------------------
# Nomenclature gaps (products only)
# --------------------------------------------------------------------

def _snake(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return re.sub(r"_+", "_", s)


def nomenclature_gaps(conn) -> dict:
    """For every product without an external_uri yet, search
    Nomenclature. A match whose category already equals the current
    product_type is pure enrichment -- apply directly. A match that
    would change product_type is a real classification change --
    raise a review instead of applying it silently."""
    rows = conn.execute(
        "SELECT id, name, product_type FROM products WHERE external_uri IS NULL"
    ).fetchall()
    applied, reviewed, checked = 0, 0, 0
    for r in rows:
        checked += 1
        try:
            candidates = _nomenclature.search_terms(r["name"])
        except Exception as e:
            print(f"terminology_cleanup: nomenclature lookup failed for "
                  f"{r['name']!r}: {e}")
            continue
        natives = [c for c in candidates if c["top_category"]]
        if not natives:
            continue
        match = natives[0]
        category_label = match["path"][1] if len(match["path"]) > 1 else match["path"][0]
        category_value = _snake(category_label)
        reference = match["uri"].rstrip("/").rsplit("/", 1)[-1]

        if r["product_type"] == category_value or r["product_type"] is None:
            conn.execute(
                "UPDATE products SET product_type=?, external_category=?, "
                "external_uri=?, external_reference=?, external_terminology=? "
                "WHERE id=?",
                (category_value, category_label, match["uri"], reference,
                 _nomenclature.TERMINOLOGY_NAME, r["id"]),
            )
            conn.commit()
            applied += 1
        else:
            _db.raise_terminology_review(
                conn, entity_type="products", review_kind="nomenclature_gap",
                entity_id=r["id"], confidence=0.5,
                description=(
                    f"{r['name']!r} currently product_type={r['product_type']!r}; "
                    f"Nomenclature suggests {category_value!r} "
                    f"({match['label']!r}, {match['uri']})"),
                raised_by=RAISED_BY,
                proposed_fix={
                    "product_type": category_value,
                    "external_category": category_label,
                    "external_uri": match["uri"],
                    "external_reference": reference,
                    "external_terminology": _nomenclature.TERMINOLOGY_NAME,
                },
                suggested_cli=(
                    f"python3 -m transcribe.terminology_cleanup apply-nomenclature "
                    f"{r['id']} {match['uri']!r}"),
            )
            reviewed += 1
    return {"checked": checked, "applied": applied, "reviewed": reviewed}


def apply_nomenclature(conn, product_id: str, uri: str) -> dict:
    """Apply a specific Nomenclature match to one product -- looks up
    the match's own path fresh (doesn't trust a stale review row) so
    this stays correct even if run well after the review was raised."""
    row = conn.execute(
        "SELECT name, product_type FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no product with id {product_id!r}")
    candidates = _nomenclature.search_terms(row["name"])
    match = next((c for c in candidates if c["uri"] == uri), None)
    if match is None or not match["top_category"]:
        raise ValueError(f"{uri!r} not found among current Nomenclature "
                          f"candidates for {row['name']!r} -- re-run nomenclature-gaps")
    category_label = match["path"][1] if len(match["path"]) > 1 else match["path"][0]
    category_value = _snake(category_label)
    reference = uri.rstrip("/").rsplit("/", 1)[-1]
    conn.execute(
        "UPDATE products SET product_type=?, external_category=?, external_uri=?, "
        "external_reference=?, external_terminology=? WHERE id=?",
        (category_value, category_label, uri, reference,
         _nomenclature.TERMINOLOGY_NAME, product_id),
    )
    conn.commit()
    return {"product": row["name"], "product_type": category_value}


# --------------------------------------------------------------------
# Names that are too specific (mechanical case: manufacturer still
# embedded in name -- the pre-genericization pattern this corpus had
# for White Swan Baking Powder etc. See items-classifier.md's altitude
# guidance for the broader, judgment-heavy version of this check that
# this mechanical pass doesn't attempt.)
# --------------------------------------------------------------------

def check_generic_names(conn, table: str = "products") -> list[dict]:
    if table != "products":
        raise ValueError("only products has a manufacturer column to check against")
    rows = conn.execute(
        "SELECT id, name, manufacturer FROM products WHERE manufacturer IS NOT NULL"
    ).fetchall()
    raised = []
    for r in rows:
        mfr = r["manufacturer"]
        name = r["name"]
        lname, lmfr = name.lower(), mfr.lower()
        # Word-boundary prefix/suffix only -- not a bare substring check.
        # Tested against the live corpus 2026-08-09: a bare-substring
        # version flagged "Dr. Pierce's Favorite Prescription" (manufacturer
        # "Dr. Pierce") and produced "'s Favorite Prescription" by blindly
        # stripping mid-word -- both semantically wrong (patent medicines
        # are a deliberate exception, the brand IS the product's identity,
        # see items-classifier.md) and grammatically broken. Every other
        # already-genericized product in this corpus (Baking Powder/White
        # Swan, Chocolate Bars/Neilson, etc.) is a clean word-boundary
        # case, so this tightening costs nothing on real positives.
        if lname.startswith(lmfr + " "):
            stripped = name[len(mfr):].strip(" -:")
        elif lname.endswith(" " + lmfr):
            stripped = name[:-len(mfr)].strip(" -:")
        else:
            continue
        stripped = re.sub(r"\s+", " ", stripped)
        if not stripped or stripped.lower() == name.lower():
            continue
        existing = conn.execute(
            """SELECT 1 FROM terminology_reviews
               WHERE entity_type='products' AND review_kind='name_too_specific'
                 AND status='open' AND entity_id=?""",
            (r["id"],),
        ).fetchone()
        if existing:
            continue
        review_id = _db.raise_terminology_review(
            conn, entity_type="products", review_kind="name_too_specific",
            entity_id=r["id"], confidence=0.7,
            description=(f"{name!r} still embeds manufacturer {mfr!r} in its own "
                         f"name -- propose genericizing to {stripped!r}"),
            raised_by=RAISED_BY,
            proposed_fix={"new_name": stripped},
            suggested_cli=(
                f"python3 -m transcribe.terminology_cleanup apply-genericize "
                f"products {name!r} {stripped!r}"),
        )
        raised.append({"id": review_id, "name": name, "proposed": stripped})
    return raised


def apply_genericize(conn, table: str, old_name: str, new_name: str) -> dict:
    """Genericize one entity: backfill mention_text on its existing
    mentions with the old (specific) name wherever mention_text is
    still null -- otherwise that specificity is lost the moment the
    old name disappears into the merge -- then merge old into new
    (creating new if it doesn't exist yet)."""
    junction, fk, namecol = _merge_entity.JUNCTIONS[table]
    old = conn.execute(f"SELECT * FROM {table} WHERE {namecol}=?", (old_name,)).fetchone()
    if old is None:
        raise ValueError(f"no {table} row named {old_name!r}")

    conn.execute(
        f"UPDATE {junction} SET mention_text=? WHERE {fk}=? AND mention_text IS NULL",
        (old_name, old["id"]),
    )
    conn.commit()

    new = conn.execute(f"SELECT id FROM {table} WHERE {namecol}=?", (new_name,)).fetchone()
    if new is None:
        new_id = _db.new_uuid()
        conn.execute(
            f"INSERT INTO {table} (id, {namecol}, normalised_key, created_at) "
            f"VALUES (?,?,?,?)",
            (new_id, new_name, new_name.lower(), _db.now_iso()),
        )
        conn.commit()

    return _merge_entity.merge_entity(conn, table, keep_name=new_name,
                                       drop_name=old_name, alias=False)


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _cmd_duplicates(args):
    conn = _db.open_connection()
    try:
        tables = [args.table] if args.table else list(NAME_COL.keys())
        for table in tables:
            raised = find_duplicates(conn, table)
            print(f"{table}: {len(raised)} duplicate candidate(s) raised")
    finally:
        conn.close()


def _cmd_nomenclature_gaps(args):
    conn = _db.open_connection()
    try:
        result = nomenclature_gaps(conn)
        print(f"checked {result['checked']}, applied {result['applied']} "
              f"(enrichment only), raised {result['reviewed']} review(s)")
    finally:
        conn.close()


def _cmd_apply_nomenclature(args):
    conn = _db.open_connection()
    try:
        result = apply_nomenclature(conn, args.product_id, args.uri)
        print(f"{result['product']!r} -> product_type={result['product_type']!r}")
    finally:
        conn.close()


def _cmd_generic_names(args):
    conn = _db.open_connection()
    try:
        raised = check_generic_names(conn, args.table)
        print(f"{args.table}: {len(raised)} name_too_specific candidate(s) raised")
    finally:
        conn.close()


def _cmd_apply_genericize(args):
    conn = _db.open_connection()
    try:
        result = apply_genericize(conn, args.table, args.old_name, args.new_name)
        print(f"kept {result['kept']!r}, dropped {result['dropped']!r} -- "
              f"moved {result['moved']} mentions, {result['collided']} collisions deduped")
    finally:
        conn.close()


def _cmd_run_all(args):
    conn = _db.open_connection()
    try:
        for table in NAME_COL:
            raised = find_duplicates(conn, table)
            print(f"duplicates/{table}: {len(raised)} raised")
        result = nomenclature_gaps(conn)
        print(f"nomenclature-gaps: checked {result['checked']}, "
              f"applied {result['applied']}, raised {result['reviewed']}")
        raised = check_generic_names(conn, "products")
        print(f"generic-names/products: {len(raised)} raised")
        n_open = conn.execute(
            "SELECT count(*) FROM terminology_reviews WHERE status='open'"
        ).fetchone()[0]
        print(f"\n{n_open} open review(s) total. See terminology_review.html "
              f"or: SELECT * FROM terminology_reviews WHERE status='open';")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_dup = sub.add_parser("duplicates", help="Find duplicate-candidate pairs")
    p_dup.add_argument("table", nargs="?", choices=list(NAME_COL.keys()),
                        help="Restrict to one entity table (default: all five)")
    p_dup.set_defaults(func=_cmd_duplicates)

    p_gap = sub.add_parser("nomenclature-gaps",
                            help="Fill/flag Nomenclature grounding gaps for products")
    p_gap.set_defaults(func=_cmd_nomenclature_gaps)

    p_apply_nom = sub.add_parser("apply-nomenclature",
                                  help="Apply a specific Nomenclature match to one product")
    p_apply_nom.add_argument("product_id")
    p_apply_nom.add_argument("uri")
    p_apply_nom.set_defaults(func=_cmd_apply_nomenclature)

    p_generic = sub.add_parser("generic-names",
                                help="Find products whose name still embeds their manufacturer")
    p_generic.add_argument("table", nargs="?", default="products")
    p_generic.set_defaults(func=_cmd_generic_names)

    p_apply_gen = sub.add_parser("apply-genericize",
                                  help="Genericize one entity's name, preserving the old form in mention_text")
    p_apply_gen.add_argument("table", choices=list(NAME_COL.keys()))
    p_apply_gen.add_argument("old_name")
    p_apply_gen.add_argument("new_name")
    p_apply_gen.set_defaults(func=_cmd_apply_genericize)

    p_all = sub.add_parser("run-all", help="Run every pass in sequence")
    p_all.set_defaults(func=_cmd_run_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
