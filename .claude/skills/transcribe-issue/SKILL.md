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
   default). Each agent is responsible for the full per-column
   transaction: read slices → write the envelope to disk → run
   the ingester → report status. This makes every column atomic.
   If the orchestrator session dies mid-batch, in-flight columns
   that have already returned are committed to the DB, and any
   that haven't returned can be re-dispatched cleanly from their
   ticket file (the row stays in `claimed` status until ingest).

   For each ticket, call the Agent tool with:
   - `subagent_type="column-transcriber"`
   - `model`: pass `model="haiku"` or `model="sonnet"` if the
     user has asked for a specific one or for a comparison;
     otherwise the agent's frontmatter default is used.
   - `prompt`: a brief user message structured as numbered steps:
     ```
     You are the column-transcriber. Follow these steps in order.

     1. Read `.claude/agents/column-transcriber.md` for your
        durable instructions.
     2. Read the ticket file:
        `transcribe/work/columns/<row_id>.json`.
        It contains the per-call context and the slices list.
     3. Read each slice PNG in `slices` order. Each slice has
        ~20px overlap on top and bottom — ignore truncated text
        at those edges.
     4. Produce the JSON envelope per "Sliced mode" in your
        instructions. One record per slice with matching idx.
        Do not insert rule markers (`---` / `--`) inside slice
        transcripts — the joiner inserts them.
     5. Write the envelope to
        `transcribe/work/results/<row_id>.json` using the Write
        tool. The file must contain ONLY the JSON envelope.
     6. Run the ingester via Bash:
          `python3 -m transcribe.ingest_column_result <row_id>`
        Confirm exit 0. The ingester validates the envelope,
        marks the row 'done', and raises a repair ticket if you
        flagged repair_needed.
     7. Reply with one line:
          `row_id=<row_id> slices=<N> ingested=<ok|FAILED>`
        If ingest failed, include the ingester's error message
        on a second line so the orchestrator can act on it.
     ```
   - Send batches of around 4–8 columns in parallel; the slowest
     column dominates wall-clock, so larger fan-out is mostly
     free. Wait for each batch to return before queuing the next,
     so a transient failure doesn't lose a whole issue's work.

   **Sub-slice tip.** Some tall column pieces are sub-divided
   in the manifest (`subdivided: true`, with consecutive
   `sub_idx` values). Treat them as separate input slices to the
   agent — return one record per slice — but the joiner won't
   insert a rule marker between them, because they belong to the
   same h-rule-bounded item.

4. **For each agent that reported `ingested=ok`**, the row is
   already in the DB and any repair has been raised. The
   orchestrator's job per column is then just to append a line
   to `transcribe/work/experiments.jsonl`:
   ```
   {ts, row_id, model, subagent_type, prompt_hash, result_path,
    transcript_chars, repair_needed, status, notes}
   ```
   This is the experiments log; it's the only place that records
   *which model ran on which row when* across exploratory and
   production runs. It survives compaction; chat doesn't.

   **For agents that reported `ingested=FAILED`** (malformed
   envelope, validation error), the result file is still on
   disk. Inspect it, decide whether to repair manually or
   re-dispatch the agent against the same ticket. The DB row
   stays in `claimed` until a successful ingest lands.

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
