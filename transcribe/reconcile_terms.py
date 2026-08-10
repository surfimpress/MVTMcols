"""Unit 4b: independent, LLM-based entity-matching tier.

Runs after transcribe/terminology_cleanup.py's Python heuristics (first-
character bucketing, exact-normalized-match and substring-containment),
not instead of them -- those stay the cheap first pass. This module adds
a second tier using real language understanding for the cases the string
heuristics structurally can't reach: two names that don't share a first
character or a substring relationship after normalization (e.g. "Wm.
Garvin" vs "William Garvin" -- normalise_key gives "wmgarvin" and
"williamgarvin", same first letter, but neither substring-contains the
other, so terminology_cleanup.py never even generates that pair).

Deliberately scoped to review_kind="duplicate_candidate" only --
nomenclature_gap/name_too_specific aren't matching problems, they stay
Python-only.

Incremental, not a full rescan. schema_meta key
'reconcile_terms_last_run' is the checkpoint: each run's candidate set is
only entities created since that checkpoint (small, after the first run).
The full existing name list per type ("the dictionary") and the set of
previously-confirmed llm-provenance matches ("confirmed examples") are
each fetched once per run as context, not per-candidate -- one lookup,
same spirit as classify_terms.py's known_values(). First run is
necessarily a full-corpus bootstrap since nothing has a checkpoint yet.

Reuses terminology_cleanup.py's own dedup-before-raise machinery
(_already_reviewed_pair, find_rule, rule_match_key) rather than
reimplementing it -- a pair this module proposes that's already been
decided (either provenance) is skipped exactly the same way the Python
passes already skip one.

Usage::

    python3 -m transcribe.reconcile_terms build
    # -> writes ticket(s) under transcribe/work/reconcile_terms/,
    #    a workflow_args.json, and prints next steps
    # dispatch via Workflow (transcribe/workflows/reconcile_terms.js),
    # save its result, then:
    python3 -m transcribe.reconcile_terms ingest-workflow-result <result.json>
"""

from __future__ import annotations

import argparse
import json
import os

from . import db as _db
from . import entity_candidates as _entity_candidates
from . import terminology_cleanup as _cleanup

WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "reconcile_terms")
CHECKPOINT_KEY = "reconcile_terms_last_run"
CONFIRMED_EXAMPLES_LIMIT = 50  # soft cap; truncation is logged, never silent

ENTITY_TABLES = ("people", "organizations", "places", "products", "events")

PROMPT_TEMPLATE = """Find likely-duplicate entities for {entity_type} ({n} candidates).
Read the batch file at {path} -- it has the candidates to check, the
existing dictionary of known names, and any confirmed examples, in the
shape described in your agent instructions.

Write your answer as a JSON array only: [{{"id_a": "...", "id_b": "...", "confidence": 0.0, "rationale": "..."}}, ...]
"""


def _get_checkpoint(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key=?", (CHECKPOINT_KEY,)
    ).fetchone()
    return row["value"] if row else None


def _set_checkpoint(conn, when: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (CHECKPOINT_KEY, when),
    )
    conn.commit()


def new_entities(conn, table: str, since: str | None) -> list[dict]:
    name_col = _cleanup.NAME_COL[table]
    if since is None:
        rows = conn.execute(
            f"SELECT id, {name_col} AS name FROM {table} ORDER BY {name_col}"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, {name_col} AS name FROM {table} "
            f"WHERE created_at > ? ORDER BY {name_col}",
            (since,),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def dictionary(conn, table: str) -> list[dict]:
    """The full current {id, name} list for a type -- reuses
    entity_candidates.all_rows(), the same uncapped helper
    build_entities_stats.py uses. Carries id (unlike Unit 3's candidate
    lists) because this tier's whole job is identity matching -- a
    candidate found to duplicate an existing dictionary entry needs a
    real id to report, not just a name."""
    name_col = _cleanup.NAME_COL[table]
    return [{"id": r["id"], "name": r["name"]}
            for r in _entity_candidates.all_rows(conn, table, name_col)]


def confirmed_examples(conn, table: str, limit: int = CONFIRMED_EXAMPLES_LIMIT) -> list[dict]:
    """Approved llm-provenance duplicate_candidate rules for this type --
    the whole feed-forward mechanism. No new storage: this reads exactly
    what apply_terminology_decisions.py already writes to terminology_rules
    when a human clicks "approve always" on an llm-sourced review. A
    python-provenance "approve always" never shows up here -- that
    decision stays a simple mechanical rule with no further effect,
    per the plan's design.
    """
    rows = conn.execute(
        "SELECT proposed_fix_json FROM terminology_rules "
        "WHERE entity_type=? AND review_kind='duplicate_candidate' "
        "AND decision='approve' AND provenance='llm' "
        "ORDER BY created_at DESC LIMIT ?",
        (table, limit),
    ).fetchall()
    out = []
    for r in rows:
        if not r["proposed_fix_json"]:
            continue
        fix = json.loads(r["proposed_fix_json"])
        if fix.get("keep") and fix.get("drop"):
            out.append({"a": fix["keep"], "b": fix["drop"]})
    return out


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_tickets(conn, tables: list[str] | None = None) -> list[dict]:
    """One ticket per entity type that has any new-since-checkpoint
    candidates. Returns the list of {entity_type, candidates_path, n,
    prompt} dicts (the workflow_args shape)."""
    tables = tables or list(ENTITY_TABLES)
    since = _get_checkpoint(conn)
    os.makedirs(WORK_DIR, exist_ok=True)
    tickets = []
    for table in tables:
        candidates = new_entities(conn, table, since)
        if not candidates:
            continue
        payload = {
            "entity_type": table,
            "candidates": candidates,
            "dictionary": dictionary(conn, table),
            "confirmed_examples": confirmed_examples(conn, table),
        }
        candidates_path = os.path.join(WORK_DIR, f"{table}.json")
        with open(candidates_path, "w") as f:
            json.dump(payload, f)
        prompt = PROMPT_TEMPLATE.format(
            entity_type=table, n=len(candidates), path=candidates_path)
        ticket_path = os.path.join(WORK_DIR, f"{table}_ticket.json")
        ticket = {"entity_type": table, "candidates_path": candidates_path,
                   "n": len(candidates), "prompt": prompt}
        with open(ticket_path, "w") as f:
            json.dump(ticket, f, indent=2)
        tickets.append(ticket)
    return tickets


def ingest_matches(conn, entity_type: str, matches: list[dict]) -> dict:
    """Apply a list of {id_a, id_b, confidence, rationale} proposed
    matches for one entity type -- raises a terminology_review for each
    one not already covered by a prior review or rule (either
    provenance), reusing terminology_cleanup.py's own dedup-before-raise
    helpers rather than reimplementing them."""
    name_col = _cleanup.NAME_COL[entity_type]
    raised, skipped = [], 0
    for m in matches:
        id_a, id_b = m.get("id_a"), m.get("id_b")
        if not id_a or not id_b or id_a == id_b:
            continue
        rows = conn.execute(
            f"SELECT id, {name_col} AS name FROM {entity_type} WHERE id IN (?, ?)",
            (id_a, id_b),
        ).fetchall()
        names = {r["id"]: r["name"] for r in rows}
        if id_a not in names or id_b not in names:
            continue  # unknown/stale id -- skip rather than error
        name_a, name_b = names[id_a], names[id_b]

        if _cleanup._already_reviewed_pair(conn, entity_type, id_a, id_b):
            skipped += 1
            continue
        key = _cleanup.rule_match_key("duplicate_candidate", name_a, name_b)
        rule = _cleanup.find_rule(conn, entity_type, "duplicate_candidate", key)
        if rule:
            # Already decided (approve or ignore) -- nothing new to raise.
            # Auto-applying here would duplicate terminology_cleanup.py's
            # own auto-apply path; this pass only proposes, on the next
            # terminology_cleanup run-all the rule will be picked up
            # there for any pair that also matches its heuristics, and
            # regardless _already_reviewed_pair will keep catching it.
            skipped += 1
            continue

        review_id = _db.raise_terminology_review(
            conn, entity_type=entity_type, review_kind="duplicate_candidate",
            entity_id=id_a, other_entity_id=id_b,
            confidence=m.get("confidence"),
            description=f"llm_match: {name_a!r} / {name_b!r} -- {m.get('rationale', '')}",
            raised_by="term-reconciler", provenance="llm",
            suggested_cli=(
                f"python3 -m transcribe.merge_entity {entity_type} "
                f"{name_a!r} {name_b!r} --alias"),
        )
        raised.append({"id": review_id, "a": name_a, "b": name_b})
    return {"entity_type": entity_type, "raised": len(raised), "skipped": skipped}


def ingest_workflow_result(conn, result_path: str) -> list[dict]:
    """result_path: JSON array of {entity_type, matches} (one entry per
    ticket the workflow processed), matching what
    transcribe/workflows/reconcile_terms.js returns. Advances the
    checkpoint to now after a successful ingest."""
    with open(result_path) as f:
        results = json.load(f)
    summaries = []
    for r in results:
        if not r:
            continue
        summaries.append(ingest_matches(conn, r["entity_type"], r.get("matches") or []))
    _set_checkpoint(conn, _db.now_iso())
    return summaries


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _cmd_build(args):
    conn = _db.open_connection()
    try:
        tables = args.tables or None
        tickets = build_tickets(conn, tables)
        if not tickets:
            print("Nothing new to check -- every entity was already here at the last checkpoint.")
            return
        args_path = os.path.join(WORK_DIR, "workflow_args.json")
        with open(args_path, "w") as f:
            json.dump(tickets, f, indent=2)
        total = sum(t["n"] for t in tickets)
        print(f"{len(tickets)} batch(es), {total} candidates. "
              f"Workflow args written to:\n{args_path}")
        print("Next: invoke Workflow with scriptPath="
              "'transcribe/workflows/reconcile_terms.js' and this file's "
              "contents as args, then save its result and run "
              "'ingest-workflow-result'.")
    finally:
        conn.close()


def _cmd_ingest_workflow_result(args):
    conn = _db.open_connection()
    try:
        summaries = ingest_workflow_result(conn, args.result_path)
        for s in summaries:
            print(f"  {s['entity_type']}: {s['raised']} raised, {s['skipped']} already covered")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build", help="Build reconcile tickets for entities new since the last checkpoint")
    p_build.add_argument("tables", nargs="*",
                          help=f"Restrict to these entity tables (default: all five; "
                               f"choices: {', '.join(ENTITY_TABLES)})")
    p_build.set_defaults(func=_cmd_build)

    p_ingest = sub.add_parser(
        "ingest-workflow-result", help="Ingest a reconcile_terms.js workflow result JSON")
    p_ingest.add_argument("result_path")
    p_ingest.set_defaults(func=_cmd_ingest_workflow_result)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
