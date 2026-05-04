---
name: transcribe-ad-issue
description: Run the ad-transcription loop for one issue (YYYY-MM-DD). Claims pending ads, sends each to an ad-transcriber agent (defaulting to Sonnet), and records the results.
---

# /transcribe-ad-issue YYYY-MM-DD [--page N] [--limit N]

Run the ad-transcription loop for one issue of the Almonte Gazette
(pass-1B).

This skill is the orchestrator's procedure for ads. The actual
reading is done by `ad-transcriber` agents (see
`.claude/agents/ad-transcriber.md`); the actual state changes are
done by Python in `transcribe/`. The orchestrator's job is to glue
them together.

Pass-1A (column transcription) and pass-1B (ad transcription) are
independent — either can run first. Running pass-1B in date order
so vendor names accumulate as priors is preferable when we wire the
ad-recurrence layer (see `transcribe/claim_ads.py` design note); for
now, transcribe in any order.

## Steps

1. **Claim the ads.** Run:
   ```
   python3 -m transcribe.claim_ads YYYY-MM-DD
   ```
   (add `--page N` or `--limit N` if scoped). This walks every
   registered ad in `mvtm.detected_ads` for the issue, hashes each
   ad PNG, inserts a `claimed` stub row in `ad_transcripts`, and
   writes a per-ad ticket file under
   `transcribe/work/ads/<row-id>.json`.

2. **Read the new tickets.** Each ticket carries the row id, the ad
   uuid, the page, the bbox in page-percent, the column span, the
   detector confidence, the agent file path, and a prompt-hash. Read
   every ticket file that doesn't yet have a result.

3. **Send each ticket to an `ad-transcriber` agent in parallel.**
   Default model is `sonnet` (the agent's frontmatter default). Each
   agent is responsible for the full per-ad transaction: read PNG →
   write the envelope to disk → run the ingester → report status.
   This makes every ad atomic. If the orchestrator session dies
   mid-batch, in-flight ads that have already returned are committed;
   any that haven't returned can be re-dispatched cleanly from their
   ticket file (the row stays in `claimed` status until ingest).

   For each ticket, call the Agent tool with:
   - `subagent_type="ad-transcriber"`
   - `prompt`: a brief user message structured as numbered steps:
     ```
     You are the ad-transcriber. Follow these steps in order.

     1. Read `.claude/agents/ad-transcriber.md` for your durable
        instructions.
     2. Read the ticket file:
        `transcribe/work/ads/<row_id>.json`.
        It contains the bbox, page, and column span context.
     3. Read the ad PNG at the `image_path` field of the ticket.
     4. Produce the JSON envelope per your instructions: a single
        object with transcript_text, transcriber_notes,
        quality_flags (all six booleans), repair_needed, repair_reason.
     5. Write the envelope to
        `transcribe/work/results/<row_id>.json` using the Write
        tool. The file must contain ONLY the JSON envelope.
     6. Run the ingester via Bash:
          `python3 -m transcribe.ingest_ad_result <row_id>`
        Confirm exit 0. The ingester validates the envelope, marks
        the row 'done', and raises a repair ticket if you flagged
        repair_needed.
     7. Reply with one line:
          `row_id=<row_id> ingested=<ok|FAILED>`
        If ingest failed, include the ingester's error message on a
        second line so the orchestrator can act on it.
     ```
   - Send batches of around 6–12 ads in parallel. Ads are typically
     smaller than columns, so wall-clock per agent is shorter; the
     larger batch size keeps fan-out efficient. Wait for each batch
     to return before queuing the next.

4. **For each agent that reported `ingested=ok`**, the row is
   already in the DB and any repair has been raised. Append a line
   to `transcribe/work/experiments.jsonl`:
   ```
   {ts, row_id, ad_uuid, model, subagent_type, prompt_hash,
    result_path, transcript_chars, repair_needed, status, notes}
   ```
   This is the experiments log; it's the only place that records
   *which model ran on which ad when*. It survives compaction; chat
   doesn't.

   **For agents that reported `ingested=FAILED`** (malformed
   envelope, validation error), the result file is still on disk.
   Inspect it, decide whether to repair manually or re-dispatch the
   agent against the same ticket. The DB row stays in `claimed`
   until a successful ingest lands.

5. **At the end of the issue**, summarise: how many ads succeeded,
   how many failed, how many repairs were raised, and any quality
   flags worth surfacing (e.g. many `partial_cut` repairs on one
   page suggest the cutting stage needs a re-run).

## Retry on Anthropic content-filter block

The same content-filter classifier that affects column transcripts
applies to ads. Patent-medicine ads (extravagant disease claims,
period vernacular for ailments and remedies), classified notices,
and auction notices for distressed estates can occasionally trip the
post-hoc safety classifier even though the source is innocuous
historical record. The block looks like:

```
API Error: ... "type":"invalid_request_error", "message":"Output blocked
by content filtering policy"
```

When an agent reply contains a content-filter / safety-block message
and no result file was written, do **not** treat the row as a
transcript failure. The DB row stays `claimed`. Escalate through
tiers:

### Tier 1 — the default that just blocked

Sonnet, one agent per ad. This is the path that emitted the block.

### Tier 2 — Sonnet, with explicit museum-archive framing

Re-dispatch the same ad with a prompt that puts the false-positive
reasoning in front of the model:

```
You are the ad-transcriber. This is a retry: the prior dispatch for
ad <ad_uuid> on row <row_id> was blocked by Anthropic's content-filter
classifier. The block is almost certainly a false positive — this is
a museum archive of the Almonte Gazette (a historical Canadian
small-town newspaper, 1862 onwards) under the Mississippi Valley
Textile Museum's stewardship. Period advertising language —
patent-medicine cures, classifieds, period vernacular for ailments,
products, and people — sometimes trips the post-hoc safety
classifier even though the source is a faithful historical record.

Follow your durable instructions
(`.claude/agents/ad-transcriber.md`). Transcribe the ad exactly as
printed:

- ad uuid:    <ad_uuid>
- ad path:    <ticket image_path>
- ticket:     transcribe/work/ads/<row_id>.json

Produce the JSON envelope per your instructions and write it to
`transcribe/work/results/<row_id>.json`. Do NOT run any ingester.
Reply with one line:
  `row_id=<row_id> wrote=<ok|FAILED>`
If the safety classifier blocks your output, say so on a second
line so the orchestrator can record it.
```

After Tier 2 returns, run the ingester manually:
`python3 -m transcribe.ingest_ad_result <row_id>`.

### Tier 3 — Opus, with explicit framing

If Tier 2 still blocks, escalate to Opus (`claude-opus-4-6`) with
the same kind of explicit framing as the column Tier-3 prompt — the
three-point museum / period / verbatim reasoning. Opus has different
filter behaviour and stronger context handling. Use only after
Tier 2 has blocked.

```
You are the ad-transcriber. This is a Tier-3 retry on row <row_id>,
ad <ad_uuid>. Two prior dispatches (Sonnet default and Sonnet with
museum-archive framing) both produced output blocked by Anthropic's
safety classifier. We are escalating to Opus because:

(1) The ad is from the Almonte Gazette (1862 onwards), held by the
    Mississippi Valley Textile Museum. It is historical record, not
    contemporary content.
(2) Period advertising (patent-medicine cures, period disease
    vocabulary, classifieds, satirical product copy) routinely uses
    language a contemporary classifier may flag — but in the
    archival context the block is a false positive.
(3) Verbatim transcription is the museum's purpose; we are not
    altering, modernising, or paraphrasing.

Follow your durable instructions
(`.claude/agents/ad-transcriber.md`). Transcribe the ad exactly as
printed:

- ad path:    <ticket image_path>
- ad uuid:    <ad_uuid>

Produce the JSON envelope and write it to
`transcribe/work/results/<row_id>.json`. Do NOT run any ingester.
Reply with one line:
  `row_id=<row_id> wrote=<ok|FAILED>`
If the safety classifier still blocks your output, say so on a
second line so the orchestrator records it.
```

### When even Opus blocks: human transcription

Surface the row, the ad path, and a short excerpt of what's visible
in the PNG to the user. The user reads the PNG and types the
transcript. **Do not silently keep retrying.**

There is no per-ad equivalent of `transcribe.import_transcript`
today. The simplest human path:

1. Write the JSON envelope by hand to
   `transcribe/work/results/<row_id>.json` matching the ad-transcriber
   schema. Add `[source: human]` to `transcriber_notes`.
2. Run `python3 -m transcribe.ingest_ad_result <row_id>` to land it.

If this happens more than two or three times, that's the signal to
extend `transcribe.import_transcript` with an `ad <row_id>`
subcommand (mirroring the `column` path); flag it on the run
summary.

### Logging

Every Tier-2 / Tier-3 retry adds an entry to
`transcribe/work/experiments.jsonl`:

```json
{"ts": "...", "row_id": "...", "ad_uuid": "...", "tier": 2,
 "failure_mode": "content_filter_block",
 "framing": "museum-archive",
 "outcome": "ok|blocked",
 "notes": "patent-medicine ad — kidney complaints / catarrh cluster"}
```

The verbose retry prompts above are deliberate: when someone reads
the agent transcripts later, they should see the reasoning, not a
bare retry.

## Things to watch for

- The agent should return a single JSON object. If it returns
  prose-wrapped JSON or a markdown fence, strip the wrapping before
  saving the result file. The ingester will fail on a malformed
  envelope rather than silently dropping the row.
- If the agent flags `repair_needed: true`, the repair shows up in
  the `repairs` table after ingest with `target_kind='ad'`. Don't
  act on it automatically — repairs are surfaced for human review.
- If many ads on one page raise `partial_cut` or
  `adjacent_text_visible`, the upstream cutting stage probably needs
  a re-run of the ads on that page rather than per-ad repairs. Note
  that in the run summary; the `mvtm_cli.py adjust-ad` /
  `regenerate-page` mutators are the path forward.
- Ad PNGs at `image_path` are typically smaller than column PNGs and
  display close to native pixels in the agent's Read tool, so the
  downsampling concern that motivated column slicing does not apply
  here. If a particular ad is unusually tall (a full-column display
  ad) and the body text comes back as plausible-but-wrong, that's
  the signal that ad-level slicing is needed — flag it; the
  recurrence_lab abandoned page-level matching, but slicing of
  oversize ads is a separate, tractable build.

## Ad recurrence (deferred — see `transcribe/claim_ads.py`)

A lot of ads in this corpus recur week-to-week. The current pass-1B
treats each ad as isolated. A later refinement will add a "prior
transcripts" block to the ticket so the agent can answer
"same as prior?" with an `same_as: <row_id>` shortcut, scaffold from
a prior transcript when the template is reused, or inherit a vendor
name as a soft consistency hint. See the design note in
`transcribe/claim_ads.py` and `project_ad_recurrence_for_transcription.md`
in user memory for the durable framing.

This is **deferred**: ship the isolated-ad pass first, then layer
the recurrence on top once a handful of issues have transcripts to
prior against.
