---
name: transcribe-issue
description: Run the column-transcription loop for one issue (YYYY-MM-DD). Claims pending columns, sends each to a column-transcriber agent (defaulting to Sonnet), and records the results. Optionally compares Haiku and Sonnet on the same columns.
---

# /transcribe-issue YYYY-MM-DD [--model sonnet|haiku|compare] [--page N] [--limit N]

Run the column-transcription loop for one issue of the Almonte
Gazette.

This skill is the orchestrator's procedure. The actual reading is
done by `column-transcriber` agents (see
`.claude/agents/column-transcriber.md`); the actual state changes
are done by Python in `transcribe/` (see `transcribe/README.md`).
The orchestrator's job is to glue them together.

## Steps

1. **Claim the columns.** Run:
   ```
   python3 -m transcribe.claim_columns YYYY-MM-DD
   ```
   (add `--page N` or `--limit N` if scoped). This walks the
   issue's pages, hashes each column PNG, inserts a `claimed`
   stub row in `column_transcripts`, and writes a per-column
   ticket file under `transcribe/work/columns/<row-id>.json`.

2. **Read the new tickets.** Each ticket is a small JSON file
   carrying the row id, the column's position, neighbour
   boundaries, registered ads, h-rules in the column, a
   prompt-hash, and — since 2026-05-02 — a `slices` list. The
   slices list is the manifest produced by `transcribe.slice`;
   each entry has a slice index, the slice PNG path
   (repo-relative under `transcribe/work/slices/<row-id>/`),
   the y-extent in page-percent, and rule-class metadata for
   the joiner. Read every ticket file that doesn't yet have a
   result.

3. **Send each ticket to a `column-transcriber` agent in
   parallel.** Default model is `sonnet` (the agent's frontmatter
   default). For each ticket, call the Agent tool with:
   - `subagent_type="column-transcriber"`
   - `prompt`: a brief user message that hands the agent the
     **slice list** (not the full column PNG) and the per-call
     context, e.g.:
     ```
     Transcribe the following slices of column <col_idx> on
     page <page>, issue <YYYY-MM-DD>. Each slice is a PNG cut
     at a horizontal rule with ~20px overlap on top and bottom.

     Slices (read in order, return one record per slice with
     the matching idx):
       idx 0:  <slice00.png>
       idx 1:  <slice01.png>
       ...

     Per-call context:
     <ticket JSON, pretty-printed>

     Return the JSON envelope described in your instructions
     under "Sliced mode" — no surrounding prose, no markdown
     fence. Do not insert rule markers (`---`, `--`) inside
     slice transcripts; the orchestrator inserts them.
     ```
   - `model`: pass `model="haiku"` or `model="sonnet"` if the
     user has asked for a specific one or for a comparison.
   - Send batches of around 4–6 columns in parallel; wait for
     each batch to return before queuing the next, so a
     transient failure doesn't lose a whole issue's work.

   **Sub-slice tip.** Some tall column pieces are sub-divided
   in the manifest (`subdivided: true`, with consecutive
   `sub_idx` values). Treat them as separate input slices to the
   agent — return one record per slice — but the joiner won't
   insert a rule marker between them, because they belong to the
   same h-rule-bounded item.

4. **Save each result and ingest.** When an agent returns its
   JSON envelope, **the first thing you do is save it to disk** —
   `transcribe/work/results/<row-id>.json` (or `.sonnet.json` /
   `.haiku.json` for comparison runs). Do this before any
   summarisation, analysis, or display in chat. Compaction can
   drop the chat-only copy at any time; the file on disk is the
   only durable record. Then run:
   ```
   python3 -m transcribe.ingest_column_result <row-id>
   ```
   The Python ingester validates the envelope, calls
   `mark_column_done`, and writes a row in `repairs` if the
   agent flagged one.

   **Also append a line to `transcribe/work/experiments.jsonl`**
   for every dispatch (success or failure). One JSON object per
   line, with at minimum:
   `{ts, row_id, model, subagent_type, prompt_hash, result_path,
   transcript_chars, repair_needed, status, notes}`. This is the
   experiments log; it's the only place that records *which model
   ran on which row when* across exploratory and production runs.
   It survives compaction; chat doesn't.

5. **At the end of the issue**, summarise: how many columns
   succeeded, how many failed, how many repairs were raised, and
   any quality flags worth surfacing. Don't ingest ads or items
   yet — those are separate skills.

## Comparing Haiku and Sonnet

If the user asks for a comparison run (`--model compare` or just
"run both on this column"):

1. Claim columns as normal.
2. For each ticket, dispatch **two** Agent calls in parallel —
   one with `model="haiku"`, one with `model="sonnet"`.
3. Save the two results to
   `transcribe/work/results/<row-id>.haiku.json` and
   `<row-id>.sonnet.json`.
4. Do **not** ingest both — the DB row carries one canonical
   transcript. Surface the two side-by-side to the user and ask
   which they want as canonical, or whether they want to defer
   the choice and inspect more cases first.

## Things to watch for

- The agent should return a single JSON object. If it returns
  prose-wrapped JSON or a markdown fence, strip the wrapping
  before saving the result file. The ingester will fail on a
  malformed envelope rather than silently dropping the row.
- If the agent flags `repair_needed: true`, the repair shows up
  in the `repairs` table after ingest. Don't act on it
  automatically — repairs are surfaced for human review via
  `transcribe repairs list`.
- If many columns on one page raise `partial_cut` or the same
  repair pattern, the upstream cutting stage probably needs a
  re-run of that page rather than per-column repairs. Note that
  in the run summary.
