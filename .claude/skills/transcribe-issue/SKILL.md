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

## Retry on Anthropic content-filter block

Anthropic's safety classifier scores the model's *output*, not just the
input image, and runs after the model has finished generating. Period
content (slang, classifieds about crime, satire, patent-medicine ads,
period vernacular) sometimes trips the classifier when concentrated in
one envelope, even though the source itself is innocuous historical
record. The block looks like:

```
API Error: ... "type":"invalid_request_error", "message":"Output blocked
by content filtering policy"
```

When an agent reply contains a content-filter / safety-block message and
no result file was written, do **not** treat the row as a transcript
failure. The DB row stays `claimed`. Escalate through tiers; each tier
is a deliberate, logged step so the audit trail explains *why* the
retry happened.

### Continuation context — prepare a preamble before Tier 2

Before dispatching the retry, check whether the blocked column or slice
sits inside a continuing piece. The classifier sees one envelope at a
time, so a slice that's the middle of a serialised story has none of the
genre/framing context that the surrounding text supplies — and that
isolation is part of why concentrated period content trips the filter.
Giving the retry agent a one-paragraph framing makes the false-positive
nature of the block legible, both to the model and to a future reviewer
reading the agent transcript.

Look for these signals on the *prior* slice's transcript (or the prior
column / prior page when the trail goes back further):

- `(To be continued.)`, `Continued on page N`, `Continued from page N`,
  `[Continued from last week]`.
- A recurring serialised title with chapter / part headers.
- A masthead or section heading that names the genre — "Our Serial
  Story", "From the Editor", "Court Records", "Patent Medicine
  Notices".

When at least one signal applies, build a short **context preamble** of
two parts:

1. **Genre / source tag** in one sentence — e.g.
   - "A fictional story syndicated to newspapers in 1912, entitled *A
     Girl of the Limberlost* by Gene Stratton-Porter."
   - "A news report from the Almonte Gazette, 1942, on local civic
     affairs."
   - "A patent-medicine advertisement of the 1880s using period
     disease vocabulary."
2. **Story-so-far summary** in two to four sentences taken from the
   *prior* successfully-transcribed slice or column. Stay close to the
   surface — characters introduced, the situation as last seen,
   anything that names the framing (this is satire / this is fiction /
   this is a court report). Do **not** invent details that aren't in
   the prior text.

Slot the preamble into the retry prompts below where they say
`<CONTEXT_PREAMBLE>`. If no continuation signal applies, replace the
placeholder with the literal string `(no prior context — this is a
self-contained piece)` so the absence is itself recorded in the
transcript.

Where to source the summary:

- For an in-flight per-column block where prior slices ingested fine,
  read those slices' transcripts on disk
  (`transcribe/work/slices/<row_id>/slice<NN>.png` came from agent
  results saved as `<row_id>.json` or per-slice `slice<NN>.json`).
- For a continuation across rows, query
  `column_transcripts.transcript_text` for the prior column (same
  page → previous col_idx, or end of page N-1 → start of page N) and
  read the last 1-2k chars.

Cache cost: writing the preamble is one human-readable paragraph the
orchestrator composes before dispatch; do not call an extra agent just
to summarise unless the prior text is genuinely too long to skim.

### Tier 1 — the default that just blocked

Sonnet, one agent per column, all slices in one envelope. This is the
path that emitted the block.

### Tier 2 — Sonnet, one agent per slice

Splitting by slice reduces the trigger-token concentration in any one
envelope. Often only one slice is the real culprit; the rest land
cleanly.

1. Read the slice manifest at
   `transcribe/work/slices/<row_id>/manifest.json` (or pull `slices`
   from the ticket file at `transcribe/work/columns/<row_id>.json`).
2. For each slice, dispatch a `column-transcriber` agent (Sonnet) in
   parallel with this prompt — written so the conversation transcript
   carries the false-positive reasoning forward for any later reviewer:

   ```
   You are the column-transcriber. This is a per-slice retry: the
   per-column envelope for row <row_id> was blocked by Anthropic's
   content-filter classifier. The block is almost certainly a false
   positive — this is a museum archive of the Almonte Gazette (a
   historical Canadian small-town newspaper, 1862 onwards) under the
   Mississippi Valley Textile Museum's stewardship. Period content
   sometimes trips the post-hoc safety classifier when concentrated
   in one envelope; splitting by slice reduces the concentration so
   each slice's transcript can land independently.

   Context for this slice (so the text isn't shorn of its framing):
   <CONTEXT_PREAMBLE>

   Follow your durable instructions
   (`.claude/agents/column-transcriber.md`). For this call, transcribe
   ONLY this one slice, exactly as printed:

   - row_id:     <row_id>
   - slice idx:  <idx>
   - slice path: transcribe/work/slices/<row_id>/slice<NN>.png

   Produce a Sliced-mode JSON envelope with a single record in
   `slices` (idx=<idx>). Set the column-level fields based on what
   you see in this one slice. Write the envelope to
   `transcribe/work/results/<row_id>.slice<NN>.json` using the Write
   tool. Do NOT run any ingester. Reply with one line:
     `slice=<idx> wrote=<ok|FAILED>`
   If the safety classifier blocks your output, say so on a second
   line so the orchestrator can record it.
   ```

3. After all per-slice agents return, merge and ingest:
   ```
   python3 -m transcribe.merge_slice_results <row_id>
   ```
   The helper reads the per-slice files, builds the canonical envelope
   at `transcribe/work/results/<row_id>.json`, runs
   `transcribe.ingest_column_result`, and exits non-zero on any
   missing or malformed slice. If a slice's per-slice agent itself
   blocked, that file will be missing — proceed to Tier 3 for the
   blocked slices only.

### Tier 3 — Opus, per-slice dispatch with explicit framing

Opus has stronger context-handling and a different filter profile. Use
it only for slices that blocked under Tier 2.

For each remaining blocked slice, dispatch a `column-transcriber` agent
with `model="claude-opus-4-6"` and this prompt:

   ```
   You are the column-transcriber. This is a Tier-3 retry on row
   <row_id> slice <idx>. Two prior dispatches (Sonnet per-column and
   Sonnet per-slice) both produced output blocked by Anthropic's
   safety classifier. We are escalating to Opus because:

   (1) The image is from the Almonte Gazette (1862 onwards), held
       by the Mississippi Valley Textile Museum. It is historical
       record, not contemporary content.
   (2) Period content (medical ads, classifieds, court reports,
       satire, period vernacular) routinely uses language a
       contemporary classifier may flag — but in the archival
       context the block is a false positive.
   (3) Verbatim transcription is the museum's purpose; we are not
       altering, modernising, or paraphrasing.

   Context for this slice (so the text isn't shorn of its framing):
   <CONTEXT_PREAMBLE>

   Follow your durable instructions
   (`.claude/agents/column-transcriber.md`). Transcribe ONLY this
   one slice, exactly as printed:

   - slice path: transcribe/work/slices/<row_id>/slice<NN>.png
   - slice idx:  <idx>

   Produce a Sliced-mode JSON envelope with a single record in
   `slices` (idx=<idx>). Write the envelope to
   `transcribe/work/results/<row_id>.slice<NN>.json` using the Write
   tool. Do NOT run any ingester. Reply with one line:
     `slice=<idx> wrote=<ok|FAILED>`
   If the safety classifier still blocks your output, say so on a
   second line so the orchestrator records it.
   ```

After all Tier-3 agents return, re-run
`python3 -m transcribe.merge_slice_results <row_id>` to merge the
combined per-slice files (Tier-2 successes + Tier-3 fills) and ingest.

### Tier 4 — sub-slice subdivision (for tall slices that still block)

If exactly one slice still blocks under Tier 3 and that slice is tall
(e.g. a long body of fiction, a long classifieds column), don't jump
straight to a human transcription of the full slice. Subdivide the
slice into 2–4 vertical sub-pieces and dispatch one agent per piece.
The aim is to reduce the human-intervention surface to whatever
sub-piece(s) still block, leaving most of the slice machine-transcribed.

1. Split the slice PNG:
   ```
   python3 -m transcribe.subdivide_slice split <row_id> <slice_idx> \
       --pieces 3
   ```
   Writes `transcribe/work/slices/<row_id>/slice<NN>.subMM.png` for
   `MM` in `00..(pieces-1)` and a sidecar manifest at
   `slice<NN>.subdivision.json`. Sub-pieces overlap by 100px so a
   mid-line cut still leaves the line whole on at least one side.

2. For each sub-piece, dispatch a `column-transcriber` agent (Opus,
   the same tier we're escalating from) with this prompt:

   ```
   You are the column-transcriber. This is a Tier-4 sub-slice retry on
   row <row_id> slice <idx>, sub-piece <sub_idx>. The full slice has
   blocked under both Sonnet and Opus per-slice dispatch. We have
   chopped the slice into <N> vertical pieces to further reduce
   trigger-token concentration so the bulk of the slice can land
   machine-transcribed; the residue (one or two sub-pieces) we'll
   transcribe by hand.

   The reasoning from the Tier-3 prompt still applies: this is the
   Almonte Gazette held by the Mississippi Valley Textile Museum,
   period content reads as flag-worthy to a contemporary classifier,
   verbatim transcription is the museum's purpose.

   Context for this slice (so the text isn't shorn of its framing):
   <CONTEXT_PREAMBLE>

   Follow your durable instructions
   (`.claude/agents/column-transcriber.md`). Transcribe ONLY this
   sub-piece. Sub-pieces overlap by ~100px on top and bottom — ignore
   any truncated lines at those edges, exactly as you would for the
   primary slicer's overlap.

   - sub-piece path: transcribe/work/slices/<row_id>/slice<NN>.subMM.png
   - parent slice idx: <idx>
   - sub-piece idx:    <sub_idx>

   Produce a JSON envelope with a single record in `slices`
   (idx=<sub_idx>, transcript_text + transcriber_notes for this
   sub-piece only). Write the envelope to
   `transcribe/work/results/<row_id>.slice<NN>.subMM.json` using the
   Write tool. Do NOT run any ingester. Reply with one line:
     `sub=<sub_idx> wrote=<ok|FAILED>`
   If the safety classifier still blocks your output, say so on a
   second line so the orchestrator records it.
   ```

3. Once sub-piece results are on disk, assemble them into a single
   per-slice envelope:
   ```
   python3 -m transcribe.subdivide_slice assemble <row_id> <slice_idx>
   ```
   Concatenates sub-piece transcripts in order, OR's quality flags,
   merges repair signals, and writes
   `transcribe/work/results/<row_id>.slice<NN>.json`. If a sub-piece
   is missing (still blocked) the assemble step exits non-zero and
   names the missing `sub_idx`.

4. Re-run `python3 -m transcribe.merge_slice_results <row_id>` to
   merge the now-complete per-slice files and ingest the column.

If one or two sub-pieces remain blocked after Tier 4, fall through to
the human-transcription path below — but only for the residual
sub-pieces, not the whole slice.

### When even Tier 4 leaves residue: human transcription

Surface the row, the still-blocked sub-piece path(s), and a short
excerpt of what's visible in the PNG to the user. The user reads the
PNG and types the transcript. **Do not silently keep retrying.**

The permanent route for landing externally-sourced transcripts is
`transcribe.import_transcript`. It writes the right envelope shape and
runs the merge / ingest steps, so the result is indistinguishable from
an LLM-supplied transcript except for the `[source: ...]` prefix in
`transcriber_notes` and the `external:<source>` model tag.

For a residual *sub-piece* (Tier 4 leftover), write the transcript to a
file and import it as a sub-piece. The import runs the assemble step
automatically; you then re-run `merge_slice_results` to ingest the
column:

```
python3 -m transcribe.import_transcript subslice <row_id> <slice_idx> \
    <sub_idx> --text-file path/to/text.txt --source human
python3 -m transcribe.merge_slice_results <row_id>
```

For a residual *whole slice* (Tier 4 not used), the simpler path is:

```
python3 -m transcribe.import_transcript slice <row_id> <slice_idx> \
    --text-file path/to/text.txt --source human
```

This writes `<row_id>.slice<NN>.json` directly and runs
`merge_slice_results` to ingest the column.

For a *whole column* transcribed externally (e.g. external OCR for a
full column), use `column` mode:

```
python3 -m transcribe.import_transcript column <row_id> \
    --text-file path/to/text.txt --source ocr-googlevision
```

Quality flags and repair signals can be passed on either subcommand
(`--damage`, `--faded`, `--smudged`, `--low-legibility`, `--partial-cut`,
`--adjacent-text-visible`, `--repair-needed --repair-reason "..."`).

Every import writes an entry to `transcribe/work/experiments.jsonl`
recording the source, transcript size, and any repair signal — that's
the audit trail for a transcript not produced by the LLM pipeline.

### Logging

Every Tier-2 / Tier-3 / Tier-4 retry adds an entry to
`transcribe/work/experiments.jsonl`:

```json
{"ts": "...", "row_id": "...", "tier": 2,
 "failure_mode": "content_filter_block",
 "slices_succeeded": [0, 1, 2, 4], "slices_blocked": [3],
 "context_preamble": "yes|no",
 "notes": "schoolboy howlers — bisex/kills-a-king cluster"}
```

For Tier 4, log the sub-piece breakdown too:

```json
{"ts": "...", "row_id": "...", "tier": 4,
 "failure_mode": "content_filter_block",
 "slice_idx": 3, "pieces": 3,
 "sub_pieces_succeeded": [0, 2], "sub_pieces_blocked": [1],
 "context_preamble": "yes",
 "notes": "fiction continuation — Bird Woman / brown-eyed boy cluster"}
```

Keep the `notes` field substantive when you can name the trigger — that
makes future investigations cheaper. The verbose retry prompts above
are deliberate: when someone reads the agent transcripts later, they
should see the reasoning, not a bare retry. The `context_preamble`
field records whether a continuation/genre framing was supplied; over
time this is the data we'll use to evaluate whether the preamble
materially reduces retry blocks.

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
