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
The candidate window is bounded at BOTH ends -- since < created_at <=
as_of, where as_of is captured once when build_tickets runs, not "now"
at ingest time. This matters: Unit 3 (term-extractor) keeps creating
entities in the background while a build -> dispatch -> ingest cycle is
in flight, so stamping the checkpoint at ingest time would silently and
permanently skip anything created during that window (its created_at
would always be earlier than the new checkpoint). The as_of snapshot is
held in schema_meta under a pending key between build and ingest, and
only promoted to the real checkpoint once ingest actually runs.

The dictionary (the full existing name list per type) and confirmed
examples are each fetched once per run as context, not per-candidate --
one lookup, same spirit as classify_terms.py's known_values(). The
dictionary is capped and recency-sorted the same way Unit 3's candidate
lists already are (entity_candidates.capped_rows) rather than sent
uncapped -- a name that hasn't been mentioned in years is unlikely to be
what a brand-new mention needs deduping against, and this keeps a
ticket's size bounded regardless of how large the corpus grows.
Candidates are additionally chunked (CANDIDATE_CHUNK_SIZE) so no single
ticket's candidate list grows unboundedly either -- a run with a large
backlog (e.g. the unavoidable first-ever bootstrap, since nothing has a
checkpoint yet) produces more tickets, not bigger ones.

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
PENDING_CHECKPOINT_KEY = "reconcile_terms_pending_as_of"
CONFIRMED_EXAMPLES_LIMIT = 50  # soft cap; truncation is logged, never silent

# The dictionary is repeated once per chunk of the same table (each
# ticket is an independent, self-contained agent call), so its size
# directly multiplies total token cost across a big backlog -- capped
# well below Unit 3's MAX_CANDIDATES=500 for that reason: this tier is
# a second-pass safety net after terminology_cleanup.py's Python
# heuristics already catch same-bucket matches, not the only place
# name reuse happens, so a tighter recency-capped sample is an
# acceptable tradeoff. CANDIDATE_CHUNK_SIZE is set close to
# DICTIONARY_CAP so a big one-off backlog (e.g. the unavoidable first-
# ever bootstrap) doesn't multiply the dictionary's cost many times
# over just to keep individual tickets small -- see build_tickets' and
# dictionary's docstrings. In the normal steady-state case (a couple
# of issues' worth of genuinely new entities per run), this produces a
# single small ticket per table regardless of either constant.
DICTIONARY_CAP = 150
CANDIDATE_CHUNK_SIZE = 150

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


def _get_pending_checkpoint(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key=?", (PENDING_CHECKPOINT_KEY,)
    ).fetchone()
    return row["value"] if row else None


def _set_pending_checkpoint(conn, when: str) -> None:
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (PENDING_CHECKPOINT_KEY, when),
    )
    conn.commit()


def _clear_pending_checkpoint(conn) -> None:
    conn.execute("DELETE FROM schema_meta WHERE key=?", (PENDING_CHECKPOINT_KEY,))
    conn.commit()


def new_entities(conn, table: str, since: str | None, as_of: str) -> list[dict]:
    """Entities created in (since, as_of] -- bounded at both ends so the
    window this call actually read is fully reproducible from as_of
    alone (see module docstring for why the upper bound matters)."""
    name_col = _cleanup.NAME_COL[table]
    if since is None:
        rows = conn.execute(
            f"SELECT id, {name_col} AS name FROM {table} "
            f"WHERE created_at <= ? ORDER BY {name_col}",
            (as_of,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT id, {name_col} AS name FROM {table} "
            f"WHERE created_at > ? AND created_at <= ? ORDER BY {name_col}",
            (since, as_of),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def dictionary(conn, table: str) -> list[dict]:
    """The current {id, name} list for a type -- capped and recency-
    sorted (entity_candidates.capped_rows), not the full table; see
    module docstring for why. Carries id (unlike Unit 3's candidate
    lists) because this tier's whole job is identity matching -- a
    candidate found to duplicate an existing dictionary entry needs a
    real id to report, not just a name."""
    name_col = _cleanup.NAME_COL[table]
    return [{"id": r["id"], "name": r["name"]}
            for r in _entity_candidates.capped_rows(conn, table, name_col, limit=DICTIONARY_CAP)]


def confirmed_examples(conn, table: str, limit: int = CONFIRMED_EXAMPLES_LIMIT) -> list[dict]:
    """Approved llm- or human-provenance duplicate_candidate rules for
    this type -- the whole feed-forward mechanism. No new storage: this
    reads exactly what apply_terminology_decisions.py already writes to
    terminology_rules when a human clicks "approve always" on an
    llm-sourced review, or manually flags a pair via entities.html
    (also provenance="human" once materialized -- see
    apply_terminology_decisions._materialize_manual_review). Either way
    it's a confirmed fact about this corpus's naming patterns, at least
    as trustworthy as an automated match. A python-provenance "approve
    always" never shows up here -- that decision stays a simple
    mechanical rule with no further effect, per the plan's design.
    """
    rows = conn.execute(
        "SELECT proposed_fix_json FROM terminology_rules "
        "WHERE entity_type=? AND review_kind='duplicate_candidate' "
        "AND decision='approve' AND provenance IN ('llm', 'human') "
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
    """One or more tickets per entity type that has any new-since-
    checkpoint candidates -- chunked to CANDIDATE_CHUNK_SIZE so a
    single ticket's candidate list stays bounded no matter how large
    the backlog is (the unavoidable first-ever bootstrap included,
    since nothing has a checkpoint yet). The dictionary and confirmed
    examples are computed once per table, not once per chunk, and are
    themselves capped (see dictionary()'s docstring). Returns the list
    of {entity_type, candidates_path, n, prompt} dicts (the
    workflow_args shape).

    Captures as_of once here and stashes it as a pending checkpoint --
    ingest_workflow_result() promotes it to the real checkpoint after a
    successful ingest, rather than this call advancing the checkpoint
    itself. See module docstring for why the timing matters."""
    tables = tables or list(ENTITY_TABLES)
    since = _get_checkpoint(conn)
    as_of = _db.now_iso()
    os.makedirs(WORK_DIR, exist_ok=True)
    tickets = []
    for table in tables:
        candidates = new_entities(conn, table, since, as_of)
        if not candidates:
            continue
        table_dictionary = dictionary(conn, table)
        table_confirmed = confirmed_examples(conn, table)
        chunks = _chunk(candidates, CANDIDATE_CHUNK_SIZE)
        for i, chunk in enumerate(chunks):
            suffix = f"_{i}" if len(chunks) > 1 else ""
            payload = {
                "entity_type": table,
                "candidates": chunk,
                "dictionary": table_dictionary,
                "confirmed_examples": table_confirmed,
            }
            candidates_path = os.path.join(WORK_DIR, f"{table}{suffix}.json")
            with open(candidates_path, "w") as f:
                json.dump(payload, f)
            prompt = PROMPT_TEMPLATE.format(
                entity_type=table, n=len(chunk), path=candidates_path)
            ticket_path = os.path.join(WORK_DIR, f"{table}{suffix}_ticket.json")
            ticket = {"entity_type": table, "candidates_path": candidates_path,
                       "n": len(chunk), "prompt": prompt}
            with open(ticket_path, "w") as f:
                json.dump(ticket, f, indent=2)
            tickets.append(ticket)
    if tickets:
        _set_pending_checkpoint(conn, as_of)
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
    transcribe/workflows/reconcile_terms.js returns. Promotes the
    as_of snapshot build_tickets() stashed as a pending checkpoint to
    the real checkpoint -- not "now" at this call's own time, which
    would silently skip anything created during the build-to-ingest
    gap (see module docstring). Falls back to now() only if no pending
    marker exists (e.g. ingest invoked without a matching build call),
    so the checkpoint always advances rather than getting stuck."""
    with open(result_path) as f:
        results = json.load(f)
    summaries = []
    for r in results:
        if not r:
            continue
        summaries.append(ingest_matches(conn, r["entity_type"], r.get("matches") or []))
    pending = _get_pending_checkpoint(conn)
    _set_checkpoint(conn, pending or _db.now_iso())
    _clear_pending_checkpoint(conn)
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
