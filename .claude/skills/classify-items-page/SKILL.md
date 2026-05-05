---
name: classify-items-page
description: Run the pass-2 items classifier on one page (YYYY-MM-DD --page N). Claims a per-page ticket assembling all column transcripts and ad transcripts on the page, dispatches an items-classifier agent, and ingests the items, spans, ad associations, and entity mentions.
---

# /classify-items-page YYYY-MM-DD --page N

Run pass-2 (items classification) for one page of the Almonte
Gazette. Pass-2 runs after pass-1A (column transcription) and
pass-1B (ad transcription) have both landed for the page.

This skill is the orchestrator's procedure. The actual
interpretation is done by an `items-classifier` agent (see
`.claude/agents/items-classifier.md`); the actual state changes are
done by Python in `transcribe/`. The orchestrator's job is to glue
them together.

## Prerequisites

- All columns on the target page have a `done` row in
  `column_transcripts` for the current image content.
- All registered ads on the target page have a `done` row in
  `ad_transcripts` for the current image content.

If either is incomplete, run `/transcribe-issue` and
`/transcribe-ad-issue` for the date first. The claimer falls back
to "no ticket written" silently when the inputs aren't ready, so
nothing breaks — but you'll get nothing done either.

## Steps

1. **Claim the page.** Run:
   ```
   python3 -m transcribe.claim_items YYYY-MM-DD --page N
   ```
   This collects every `done` column transcript on the page (latest
   per `col_idx` if there are multiple — handles re-cuts), every
   `done` ad transcript on the page (latest per `ad_uuid`), the
   page's column boundary positions and page-geometry, and the
   page-level h-rules. It computes a `content_hash` over the sorted
   transcript ids and skips the page if items already exist tagged
   with that hash. Otherwise it writes a per-page ticket file at
   `transcribe/work/items/<YYYY-MM-DD>_p<N>.json`.

2. **Read the ticket.** The ticket carries:
   - the page's column transcripts (id, col_idx, transcript_text,
     char_count, slice_boundaries — slice markers are inline in
     transcript_text as `---` for full-width rules and `--` for
     narrow rules);
   - the page's ad transcripts (ad_transcript_id, ad_uuid, bbox_pct,
     cols_spanned, transcript_text);
   - the page_state (num_columns, boundary_positions, page_geometry);
   - the page-level h_rules (page-percent y-positions and widths);
   - the content_hash (idempotency key) and the prompt_hash.

3. **Dispatch one items-classifier agent.** One agent per page —
   the classifier needs to see the whole page at once so it can
   reason about cross-column items and consistent item boundaries.
   Default model is `sonnet` (the agent's frontmatter default).

   Call the Agent tool with:
   - `subagent_type="items-classifier"` (or, if the registry doesn't
     expose the custom name yet, `subagent_type="general-purpose"`
     plus an explicit instruction to read
     `.claude/agents/items-classifier.md` first — see the column
     transcriber fallback memory).
   - `prompt`: a brief user message structured as numbered steps:
     ```
     You are the items-classifier. Follow these steps in order.

     1. Read `.claude/agents/items-classifier.md` for your durable
        instructions.
     2. Read the ticket file:
        `transcribe/work/items/<YYYY-MM-DD>_p<N>.json`.
        It contains every column transcript and ad transcript on the
        page, plus the page state and h-rules.
     3. Identify the items on the page. For each item:
          - pick item_type, headline (if any), byline (if any),
            summary, language, classification_confidence;
          - anchor it to one or more column_spans and/or ad_uuids;
          - extract entity mentions (people, organizations, places,
            products, events) with role, mention_text, span_start /
            span_end into the item's full_text, and confidence;
          - flag repair_needed if the cutting/transcription stage's
            output makes the item hard to interpret.
     4. Produce the JSON envelope per your instructions:
          {items: [...], page_repair_needed, page_repair_reason}
        Use `---` and `--` markers in transcript_text as item-boundary
        hints (full-width rule = item separator by default, narrow
        rule = sub-divider within one item by default).
     5. Write the envelope to
        `transcribe/work/results/<YYYY-MM-DD>_p<N>.json` using the
        Write tool. The file must contain ONLY the JSON envelope.
     6. Run the ingester via Bash:
          `python3 -m transcribe.ingest_item_result <YYYY-MM-DD>_p<N>`
        Confirm exit 0. The ingester validates the envelope, derives
        page-percent bboxes from your column spans + ad anchors, and
        inserts items, spans, ad associations, and entity mentions.
     7. Reply with one line:
          `page=<YYYY-MM-DD>_p<N> items=<count> ingested=<ok|FAILED>`
        If ingest failed, include the ingester's error message on a
        second line so the orchestrator can act on it.
     ```

4. **For an `ingested=ok` reply**, the items are in the DB and any
   repairs are raised. Append a line to
   `transcribe/work/experiments.jsonl`:
   ```
   {ts, page_id, model, subagent_type, prompt_hash, result_path,
    items_count, repairs_raised, status, notes}
   ```

   **For `ingested=FAILED`**, inspect the result file. Common
   failures: an item references a `column_transcript_id` not on the
   page (typo / stale id), a span with `start_offset >= end_offset`,
   or an unknown `item_type`. Surface the error and either repair
   the result file by hand or re-dispatch.

5. **Spot-check before scaling.** After the first few pages,
   eyeball the items table:
   ```
   sqlite3 transcribe/data/transcribe.db \
     "SELECT id, item_type, headline, length(full_text), bbox_top_pct,
             bbox_bottom_pct, length(summary) AS sum_chars
        FROM items WHERE year=YYYY AND month=MM AND day=DD AND page=N
       ORDER BY bbox_top_pct;"
   ```
   And the entity counts:
   ```
   sqlite3 transcribe/data/transcribe.db \
     "SELECT 'people' AS k, COUNT(*) FROM people
       UNION ALL SELECT 'organizations', COUNT(*) FROM organizations
       UNION ALL SELECT 'places', COUNT(*) FROM places
       UNION ALL SELECT 'products', COUNT(*) FROM products
       UNION ALL SELECT 'events', COUNT(*) FROM events;"
   ```
   Look for: items with implausible bboxes (top > bottom, or both
   = 0/100), items with empty `full_text` that aren't ads, ad items
   without an `ad_uuid` association, entities that should obviously
   merge but didn't (the dedup is permissive — first cut accepts
   imperfect coverage; cross-corpus merging is a later pass).

## Things to watch for

- The agent's char offsets must be Python-style half-open intervals
  into the column transcript_text. The ingester slices
  `transcript_text[start:end]` directly. `start >= end` is invalid.
- If the agent emits `start_offset` / `end_offset` that fall mid-line
  inside the ticket's transcript_text, that's allowed but harder to
  audit — encourage offsets that align with slice_boundaries when
  possible. The `---` / `--` markers in transcript_text are at
  newline-only lines, so anchoring to a marker line gives clean cuts.
- The `page_geometry` block carries `text_left`, `text_right`, and
  `binding_side` — but **not** vertical text-area extents. The
  ingester uses `slice_boundaries.y_top_pct` / `y_bottom_pct` for
  per-span vertical bounds; if a column has no slice metadata
  (legacy full-image transcripts), the item's vertical extent
  defaults to 0–100 and any ad anchors take over.
- Re-running pass-2 after a column or ad has been re-transcribed
  (new transcript ids) creates a fresh ticket — items go to a new
  batch tagged with the new content_hash, and the prior batch stays
  for history. There's no auto-merge; use SQL to retire the old
  batch when you're satisfied with the new one.
- Repairs raised at `target_kind='page'` cover both per-item
  repair flags and the page-level `page_repair_needed`. Don't act
  on them automatically — surface for human review via
  `transcribe.repairs`.

## Items-table conventions reminder

- `bbox_left_pct` / `bbox_top_pct` / `bbox_right_pct` /
  `bbox_bottom_pct` are NOT NULL and computed by the ingester.
- `crosses_columns` is set to 1 when the item spans more than one
  `col_idx`.
- `is_inset` is set to 1 when the item has both `column_spans` and
  `ad_uuids` (a display ad embedded mid-column).
- `crosses_pages` is always 0 in this pass; cross-page continuation
  is captured via `continued_to_item_id` / `continued_from_item_id`
  but linking those is out of scope today.
- `notes` carries `content_hash=<hex>` for the idempotency check.
  Don't overwrite it.
