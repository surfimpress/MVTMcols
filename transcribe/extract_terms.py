"""Independent term-extraction queue.

Unit 3 of the OCR+LLM route's split pipeline. Finds every person/
organization/place/product/event mentioned in already-segmented items
(ocr-items, transcribe/ocr_llm.py, writes items.full_text) and records
them as raw mentions, reusing the exact same entity-upsert/dedup path
(transcribe/ingest_item_result.py:_insert_mentions/upsert_entity) that
used to be called inline from ocr-items' own output. Deliberately
decoupled in time and code from segmentation -- runs whenever, over
whatever's accumulated, same spirit as transcribe/classify_terms.py
(its own module docstring explains the same split for type
classification; this module is the equivalent for extraction).

items.terms_extracted_at (schema v12) is the readiness signal -- NULL
means not yet processed, same pattern as classify_terms.py's
"NULL in the type column" signal. Scoped to the OCR+LLM route only
(year >= routing.COLUMN_CUT_CUTOFF_YEAR): the pre-1980 route's items
already get entity mentions from items-classifier's own ingest and
would otherwise show up here as false positives (nothing else sets
terms_extracted_at for them).

No entity-matching/dedup work happens in the extraction agent itself
(term-extractor.md) -- it writes mentions as plain text, no candidate
list, no id. All matching is upsert_entity's normalise_key(name) match,
which runs the same way regardless of who calls it. terminology_cleanup.py
remains the separate pass that catches spelling-variant near-duplicates
upsert_entity's exact-key match can't -- unchanged by this module.

Usage::

    python3 -m transcribe.extract_terms build
    # -> writes ticket(s) under transcribe/work/extract_terms/,
    #    a workflow_args.json, and prints next steps
    # dispatch via Workflow (transcribe/workflows/extract_terms.js),
    # save its result, then:
    python3 -m transcribe.extract_terms ingest-workflow-result <result.json>
"""

from __future__ import annotations

import argparse
import json
import os

from . import db as _db
from . import ingest_item_result as _ingest_items
from . import routing as _routing

WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "extract_terms")
MAX_BATCH = 100  # items per ticket/call

# Page furniture / already-noise item types -- low signal, not worth a
# call. Same "prefer to skip a marginal mention than invent one" spirit
# already established for the extraction guidance itself.
EXCLUDED_ITEM_TYPES = ("masthead", "other")

PROMPT_TEMPLATE = """Extract entity mentions from {n} already-segmented items.
Read the batch file at {items_path} -- it has the items to process
({{"items": [...]}}), in the shape described in your agent instructions.

Write your answer as a JSON array only: [{{"id": "...", "people": [...], "organizations": [...], "places": [...], "products": [...], "events": [...]}}, ...]
"""

# entity table -> (junction table, junction FK column, mention name keys)
ENTITY_TABLES = (
    ("people", "item_people_mentions", "person_id", ("name", "full_name")),
    ("organizations", "item_organizations_mentions", "organization_id", ("name",)),
    ("places", "item_places_mentions", "place_id", ("name",)),
    ("products", "item_products_mentions", "product_id", ("name",)),
    ("events", "item_events_mentions", "event_id", ("name",)),
)


def pending_items(conn) -> list[dict]:
    placeholders = ",".join("?" * len(EXCLUDED_ITEM_TYPES))
    rows = conn.execute(
        f"SELECT id, item_type, headline, full_text FROM items "
        f"WHERE terms_extracted_at IS NULL AND year >= ? "
        f"AND item_type NOT IN ({placeholders}) "
        f"AND full_text IS NOT NULL AND full_text != '' "
        f"ORDER BY year, month, day, page",
        (_routing.COLUMN_CUT_CUTOFF_YEAR, *EXCLUDED_ITEM_TYPES),
    ).fetchall()
    return [dict(r) for r in rows]


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_tickets(conn, max_batch: int = MAX_BATCH) -> list[dict]:
    """Write one ticket per batch of up to max_batch pending items.
    Returns the list of {items_path, n, prompt} dicts (the
    workflow_args shape)."""
    pending = pending_items(conn)
    if not pending:
        return []
    os.makedirs(WORK_DIR, exist_ok=True)
    tickets = []
    for batch_idx, batch in enumerate(_chunk(pending, max_batch)):
        items_path = os.path.join(WORK_DIR, f"batch_{batch_idx}.json")
        with open(items_path, "w") as f:
            json.dump({"items": batch}, f)
        prompt = PROMPT_TEMPLATE.format(n=len(batch), items_path=items_path)
        ticket_path = os.path.join(WORK_DIR, f"batch_{batch_idx}_ticket.json")
        ticket = {"items_path": items_path, "n": len(batch), "prompt": prompt}
        with open(ticket_path, "w") as f:
            json.dump(ticket, f, indent=2)
        tickets.append(ticket)
    return tickets


def ingest_assignments(conn, extractions: list[dict],
                        all_item_ids: set[str] | None = None) -> dict:
    """Apply a list of {id, people, organizations, places, products,
    events} per-item raw-mention dicts -- reuses the exact same
    ingest_item_result._insert_mentions/upsert_entity path ocr_llm.py
    used to call inline from ocr-items' own output, just fed by this
    module's extractions instead. Marks each processed item's
    terms_extracted_at so it isn't picked up again.

    all_item_ids, when given, is the full set of items actually sent
    for extraction (not just the ones with entries in `extractions`) --
    every id in it gets terms_extracted_at set even if it has zero
    mentions, so a legitimately-empty item is never mistaken for
    "not processed yet" on a future pending_items() scan."""
    item_ids = {e["id"] for e in extractions if e.get("id")}
    if all_item_ids:
        item_ids |= set(all_item_ids)
    if not item_ids:
        return {"items_processed": 0, "mentions_inserted": 0}
    placeholders = ",".join("?" * len(item_ids))
    rows = conn.execute(
        f"SELECT id, year, month, day FROM items WHERE id IN ({placeholders})",
        list(item_ids),
    ).fetchall()
    dates = {r["id"]: f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}" for r in rows}

    now = _db.now_iso()
    by_id = {e["id"]: e for e in extractions if e.get("id")}
    items_processed, mentions_inserted = 0, 0
    for item_id in item_ids:
        mention_date = dates.get(item_id)
        if mention_date is None:
            continue  # unknown/stale item id -- skip rather than error
        e = by_id.get(item_id, {})
        for entity_table, junction_table, fk_col, name_keys in ENTITY_TABLES:
            mentions_inserted += _ingest_items._insert_mentions(
                conn, item_id=item_id,
                mentions=e.get(entity_table) or [],
                entity_table=entity_table,
                junction_table=junction_table,
                junction_fk_col=fk_col,
                name_keys=name_keys,
                mention_date=mention_date,
            )
        conn.execute(
            "UPDATE items SET terms_extracted_at=? WHERE id=?",
            (now, item_id),
        )
        items_processed += 1
    conn.commit()
    return {"items_processed": items_processed, "mentions_inserted": mentions_inserted}


def _ticket_item_ids(tickets: list[dict]) -> set[str]:
    """Every item id actually sent across a set of tickets, read back
    from their items_path files -- the structural source of truth for
    "what was sent", independent of what the agent chose to return."""
    ids = set()
    for t in tickets:
        with open(t["items_path"]) as f:
            payload = json.load(f)
        ids.update(item["id"] for item in payload["items"])
    return ids


def ingest_workflow_result(conn, result_path: str) -> dict:
    """result_path: a flat JSON array of {id, people, organizations,
    places, products, events} dicts, one per item across every ticket
    the workflow processed -- matches what
    transcribe/workflows/extract_terms.js returns (already flattened
    across tickets there, since no per-ticket routing metadata is
    needed -- unlike classify_terms.js, every item here carries its
    own id).

    Marks terms_extracted_at for every item actually sent, not just
    ones the agent chose to include in its output -- an item genuinely
    found to have zero mentions is still "done", and trusting the
    agent's own omission to double as that signal is exactly the bug
    this guards against (confirmed happening: 365 of 640 items in the
    first real run came back empty and were silently never marked
    done, because the agent was told -- and, separately, chose -- to
    omit them). Reads workflow_args.json from the same directory as
    result_path if present, since build/ingest always share WORK_DIR.
    """
    with open(result_path) as f:
        data = json.load(f)
    # Accept either the bare result array or the harness's own
    # TaskOutput-style {"result": [...], "summary": ..., ...} wrapper --
    # both have shown up on disk depending on how the file was saved
    # (see ocr_llm.py's ingest, which handles the same ambiguity).
    extractions = data["result"] if isinstance(data, dict) and "result" in data else data
    all_ids = None
    args_path = os.path.join(os.path.dirname(os.path.abspath(result_path)), "workflow_args.json")
    if os.path.exists(args_path):
        with open(args_path) as f:
            tickets = json.load(f)
        all_ids = _ticket_item_ids(tickets)
    return ingest_assignments(conn, extractions, all_item_ids=all_ids)


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _cmd_build(args):
    conn = _db.open_connection()
    try:
        tickets = build_tickets(conn, args.max_batch)
        if not tickets:
            print("Nothing pending -- every eligible item already has terms_extracted_at set.")
            return
        args_path = os.path.join(WORK_DIR, "workflow_args.json")
        with open(args_path, "w") as f:
            json.dump(tickets, f, indent=2)
        total = sum(t["n"] for t in tickets)
        print(f"{len(tickets)} batch(es), {total} items pending. "
              f"Workflow args written to:\n{args_path}")
        print("Next: invoke Workflow with scriptPath="
              "'transcribe/workflows/extract_terms.js' and this file's "
              "contents as args, then save its result and run "
              "'ingest-workflow-result'.")
    finally:
        conn.close()


def _cmd_ingest_workflow_result(args):
    conn = _db.open_connection()
    try:
        summary = ingest_workflow_result(conn, args.result_path)
        print(f"  {summary['items_processed']} items processed, "
              f"{summary['mentions_inserted']} mentions inserted")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build", help="Build extraction tickets for all items missing terms_extracted_at")
    p_build.add_argument("--max-batch", type=int, default=MAX_BATCH, dest="max_batch")
    p_build.set_defaults(func=_cmd_build)

    p_ingest = sub.add_parser(
        "ingest-workflow-result", help="Ingest an extract_terms.js workflow result JSON")
    p_ingest.add_argument("result_path")
    p_ingest.set_defaults(func=_cmd_ingest_workflow_result)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
