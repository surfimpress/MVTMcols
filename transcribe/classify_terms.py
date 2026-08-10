"""Independent term-classification queue.

Backfills org_type/place_type/product_type/event_type on entities
that are missing them. Deliberately decoupled from the render/
cleanup/items pipeline (transcribe/ocr_llm.py) that creates those
entities: it runs whenever, over whatever's accumulated corpus-wide,
regardless of which issue or route produced the entity. See
CLAUDE.md's "Current status" for why this split exists -- the
items-pass (Sonnet + image, ~318s/call) was bundling expensive
segmentation work with classification work the pre-1980 route
already showed doesn't need an image at all.

NULL in the type column is the readiness signal -- no separate claim/
status column needed. A term-classifier call always assigns a
plausible value (see .claude/agents/term-classifier.md), so nothing
gets silently left pending forever, and nothing gets double-processed
by a second sweep.

Same three-layer split as the rest of transcribe/: this module does
the deterministic parts (query pending entities, build batched
tickets, ingest results) and never calls an LLM itself.

Usage::

    python3 -m transcribe.classify_terms build
    # -> writes ticket(s) under transcribe/work/classify_terms/,
    #    a workflow_args.json, and prints next steps
    # dispatch via Workflow (transcribe/workflows/classify_terms.js),
    # save its result, then:
    python3 -m transcribe.classify_terms ingest-workflow-result <result.json>
"""

from __future__ import annotations

import argparse
import json
import os

from . import db as _db
from . import nomenclature as _nomenclature

WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "classify_terms")
MAX_BATCH = 150  # entities per ticket/call -- keeps prompts modest, still
                  # lets the taxonomy-reference prefix amortize over many
                  # entities per call rather than one-entity-per-call.
MAX_CONTEXT_SNIPPETS = 3

# entity table -> (type column, mention junction table, junction FK column)
CLASSIFIABLE = {
    "organizations": ("org_type", "item_organizations_mentions", "organization_id"),
    "places": ("place_type", "item_places_mentions", "place_id"),
    "products": ("product_type", "item_products_mentions", "product_id"),
    "events": ("event_type", "item_events_mentions", "event_id"),
}

PROMPT_TEMPLATE = """Classify {n} {entity_type} entities for their {type_field}
field. Read the batch file at {entities_path} -- it has the current
taxonomy reference (known_values) and the entities to classify
(entities), in the shape described in your agent instructions.

Write your answer as a JSON array only: [{{"id": "...", "value": "..."}}, ...]
"""


def _entity_context(conn, table: str, entity_id: str,
                     junction: str, fk: str) -> list[str]:
    rows = conn.execute(
        f"SELECT m.mention_text, i.headline FROM {junction} m "
        f"JOIN items i ON i.id = m.item_id WHERE m.{fk}=? "
        f"LIMIT {MAX_CONTEXT_SNIPPETS}",
        (entity_id,),
    ).fetchall()
    out = []
    for r in rows:
        snippet = r["mention_text"] or r["headline"]
        if snippet and snippet not in out:
            out.append(snippet)
    return out


def pending_entities(conn, table: str) -> list[dict]:
    type_col, junction, fk = CLASSIFIABLE[table]
    has_mfr = table == "products"
    cols = "id, name" + (", manufacturer" if has_mfr else "")
    rows = conn.execute(
        f"SELECT {cols} FROM {table} WHERE {type_col} IS NULL ORDER BY name"
    ).fetchall()
    out = []
    for r in rows:
        entry = {"id": r["id"], "name": r["name"]}
        if has_mfr and r["manufacturer"]:
            entry["manufacturer"] = r["manufacturer"]
        context = _entity_context(conn, table, r["id"], junction, fk)
        if context:
            entry["context"] = context
        if table == "products":
            # Nomenclature only catalogs museum-object-type goods, not
            # perishables/retail groceries -- an empty result here is
            # expected and correct for those, not a failure. A network
            # hiccup against the live endpoint shouldn't break ticket
            # building for the whole batch, so degrade to no candidates.
            try:
                candidates = _nomenclature.search_terms(r["name"])
            except Exception as e:
                print(f"classify_terms: nomenclature lookup failed for "
                      f"{r['name']!r}: {e}")
                candidates = []
            natives = [c for c in candidates if c["top_category"]]
            if natives:
                entry["nomenclature_candidates"] = [
                    {"uri": c["uri"], "label": c["label"], "path": c["path"]}
                    for c in natives
                ]
        out.append(entry)
    return out


def known_values(conn, table: str) -> list[dict]:
    type_col, _junction, _fk = CLASSIFIABLE[table]
    rows = conn.execute(
        f"SELECT {type_col} AS value, count(*) AS n FROM {table} "
        f"WHERE {type_col} IS NOT NULL GROUP BY {type_col} ORDER BY n DESC"
    ).fetchall()
    return [{"value": r["value"], "count": r["n"]} for r in rows]


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_tickets(conn, tables: list[str] | None = None) -> list[dict]:
    """Write one ticket per batch of up to MAX_BATCH pending entities,
    per table. Returns the list of {entity_type, type_field, ticket_path,
    entities_path, n, prompt} dicts (the workflow_args shape)."""
    tables = tables or list(CLASSIFIABLE.keys())
    os.makedirs(WORK_DIR, exist_ok=True)
    tickets = []
    for table in tables:
        type_col, _junction, _fk = CLASSIFIABLE[table]
        pending = pending_entities(conn, table)
        if not pending:
            continue
        ref = known_values(conn, table)
        table_dir = os.path.join(WORK_DIR, table)
        os.makedirs(table_dir, exist_ok=True)
        for batch_idx, batch in enumerate(_chunk(pending, MAX_BATCH)):
            payload = {
                "entity_type": table, "type_field": type_col,
                "known_values": ref, "entities": batch,
            }
            entities_path = os.path.join(table_dir, f"batch_{batch_idx}.json")
            with open(entities_path, "w") as f:
                json.dump(payload, f)
            prompt = PROMPT_TEMPLATE.format(
                n=len(batch), entity_type=table, type_field=type_col,
                entities_path=entities_path,
            )
            ticket_path = os.path.join(table_dir, f"batch_{batch_idx}_ticket.json")
            ticket = {
                "entity_type": table, "type_field": type_col,
                "entities_path": entities_path, "n": len(batch), "prompt": prompt,
            }
            with open(ticket_path, "w") as f:
                json.dump(ticket, f, indent=2)
            tickets.append(ticket)
    return tickets


def ingest_assignments(conn, entity_type: str, type_field: str,
                        assignments: list[dict]) -> dict:
    """Apply {"id", "value"} assignments to one entity table. Logs (but
    doesn't block on) any value not already in known_values -- visibility
    into organic taxonomy growth, same spirit as entity_candidates.py's
    truncation logging."""
    existing = {v["value"] for v in known_values(conn, entity_type)}
    updated, new_values, nomenclature_matches = 0, set(), 0
    for a in assignments:
        value = a.get("value")
        if not value:
            continue
        if entity_type == "products" and a.get("nomenclature_uri"):
            uri = a["nomenclature_uri"]
            # external_reference is derived here, never taken from the
            # agent -- it's just the URI's own last path segment, and
            # deriving it deterministically means the agent can't get
            # the reference number wrong.
            reference = uri.rstrip("/").rsplit("/", 1)[-1]
            conn.execute(
                "UPDATE products SET product_type=?, external_category=?, "
                "external_uri=?, external_reference=?, external_terminology=? "
                "WHERE id=?",
                (value, a.get("nomenclature_category"), uri, reference,
                 _nomenclature.TERMINOLOGY_NAME, a["id"]),
            )
            nomenclature_matches += 1
        else:
            conn.execute(
                f"UPDATE {entity_type} SET {type_field}=? WHERE id=?",
                (value, a["id"]),
            )
        updated += 1
        if value not in existing:
            new_values.add(value)
    conn.commit()
    if nomenclature_matches:
        print(f"classify_terms: {entity_type} -- {nomenclature_matches} "
              f"grounded in Nomenclature")
    if new_values:
        print(f"classify_terms: {entity_type}.{type_field} gained new "
              f"value(s) not previously in the corpus: {sorted(new_values)}")
    return {"entity_type": entity_type, "updated": updated,
            "new_values": sorted(new_values)}


def ingest_workflow_result(conn, result_path: str) -> list[dict]:
    """result_path: JSON array of {entity_type, type_field, assignments}
    (one entry per ticket the workflow processed), matching what
    transcribe/workflows/classify_terms.js returns."""
    with open(result_path) as f:
        data = json.load(f)
    # Accept either the bare result array or the harness's own
    # TaskOutput-style {"result": [...], "summary": ..., ...} wrapper --
    # confirmed as a real bug in the sibling ingest_workflow_result
    # functions (ocr_llm.py, extract_terms.py, reconcile_terms.py)
    # 2026-08-10, not just a hypothetical edge case.
    results = data["result"] if isinstance(data, dict) and "result" in data else data
    summaries = []
    for r in results:
        if not r or not r.get("assignments"):
            continue
        summaries.append(ingest_assignments(
            conn, r["entity_type"], r["type_field"], r["assignments"]))
    return summaries


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _cmd_build(args):
    bad = [t for t in args.tables if t not in CLASSIFIABLE]
    if bad:
        raise SystemExit(f"unknown table(s): {bad}; choose from {list(CLASSIFIABLE.keys())}")
    conn = _db.open_connection()
    try:
        tables = args.tables or None
        tickets = build_tickets(conn, tables)
        if not tickets:
            print("Nothing pending -- every entity already has its type field set.")
            return
        args_path = os.path.join(WORK_DIR, "workflow_args.json")
        with open(args_path, "w") as f:
            json.dump(tickets, f, indent=2)
        total = sum(t["n"] for t in tickets)
        print(f"{len(tickets)} batch(es), {total} entities pending. "
              f"Workflow args written to:\n{args_path}")
        print("Next: invoke Workflow with scriptPath="
              "'transcribe/workflows/classify_terms.js' and this file's "
              "contents as args, then save its result and run "
              "'ingest-workflow-result'.")
    finally:
        conn.close()


def _cmd_ingest_workflow_result(args):
    conn = _db.open_connection()
    try:
        summaries = ingest_workflow_result(conn, args.result_path)
        for s in summaries:
            print(f"  {s['entity_type']}: {s['updated']} updated"
                  + (f", new values: {s['new_values']}" if s["new_values"] else ""))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build", help="Build classify tickets for all entities missing a type field")
    p_build.add_argument("tables", nargs="*",
                          help=f"Restrict to these entity tables (default: all four; "
                               f"choices: {', '.join(CLASSIFIABLE.keys())})")
    p_build.set_defaults(func=_cmd_build)

    p_ingest = sub.add_parser(
        "ingest-workflow-result", help="Ingest a classify_terms.js workflow result JSON")
    p_ingest.add_argument("result_path")
    p_ingest.set_defaults(func=_cmd_ingest_workflow_result)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
