# Transcription playbook — read this first if you're picking up cold

This is the current, canonical procedure for running MVTM column
transcription at scale. It supersedes ad hoc manual dispatch for any
run of more than a handful of columns. If you're a fresh session with
no memory of prior conversation, this file plus `transcribe/data/transcribe.db`
plus `transcribe/work/experiments.jsonl` is everything you need — you
do not need to reconstruct anything from a prior chat transcript.

## Why this exists

Manual per-column dispatch (read ticket → dispatch agent → wait →
record usage → dispatch next) was tried at scale across several
issues and reliably produced silent gaps: columns that were never
dispatched at all, discovered only by chance much later. Two separate
incidents happened in the same session (2026-08-05): 1958-09-25 lost
p5c3 and p10c7 to a page-column-count miscount; 1970-08-20 lost five
consecutive columns (p3c0–p3c4) to a plain sequencing skip. Both were
only caught because the user asked "have you stopped again?" and a
full reconciliation was run. That is not a reliable safety net — the
structural fix is to never derive "what's left to dispatch" from
memory, and to never run one-at-a-time turn-by-turn dispatch for bulk
work.

## The script

`transcribe/PLAYBOOK.md` (this file) is the *procedure*. The actual
orchestration logic lives in a `Workflow` script,
**`transcribe_continuous.js`**. As of 2026-08-05 the canonical copy
is at:

```
/private/tmp/claude-502/-Users-peter-Projects-MVTM/c2abdec1-315d-4b5e-8ae9-b9ff87b93f57/scratchpad/transcribe_continuous.js
```

**That path is a session-scratch path and will not survive a session
reset.** The first thing a fresh session should do if this file is
gone is recreate it — the full source is reproduced in
`instructions/transcribe_continuous.js.snapshot` (copy it back to a
fresh scratch path and pass it as `scriptPath`) — see "Recreating the
script" below if that snapshot is itself missing or stale.

## What the script does

Two modes, selected via `args.mode`:

### `produce` mode (default) — bulk transcription

```
# Step 1 -- orchestrator (me, via Bash) claims + queries directly, no agent:
python3 transcribe/orchestrator_claim_query.py 1886-07-16 1897-11-05 > claim_query_result.json

# Step 2 -- pass the resulting items array into the Workflow:
Workflow({
  scriptPath: "<path to transcribe_continuous.js>",
  args: { mode: "produce", dates: ["1886-07-16", "1897-11-05"], items: [...from claim_query_result.json...], model: "sonnet" }
})
```

- `dates`: an array of `YYYY-MM-DD` issue dates (or pass a single
  `date` string). **Processed as one continuous queue, not separate
  sequential issues** — there is no barrier between issues, so the
  tail of one issue's stragglers runs alongside the head of the next.
  This is the structural fix for "idle slots at the end of an issue."
- `model`: `sonnet` (default), `haiku`, or `opus`.
- `limit`: optional cap on total columns processed this run (useful
  for a bounded test before committing to a full run).
- `items`: **required** for produce mode as of 2026-08-05 (factory-
  system rework). A pre-built array (`{id, page, col_idx, n_slices,
  date, y, m, day}` per column) that the orchestrator builds by
  running `orchestrator_claim_query.py <date> <date> ...` directly via
  Bash **before invoking `Workflow` at all**. Claim/query is
  deterministic, mechanical work (download + slice + a `SELECT`) with
  zero LLM judgment needed, so it no longer runs as a dispatched agent
  inside the script -- it runs as plain Python the orchestrator
  executes itself. In both cases it's **zero tokens**, always -- but
  wall time is NOT always cheap: measured 2026-08-05, 4.06s for 134
  columns across 3 issues that had already been Drive-cached (picked
  and eyeballed earlier in the project), versus **50.73s for 28
  columns (~1.8s/column) on 1870-01-08, a genuinely cold date touched
  for the first time**. Don't assume claim is always sub-2s -- a cold
  issue's real cost is the Drive download, and it scales with column
  count. This is exactly why prepping the next issue *during* the
  current run's Transcribe phase (rather than after it finishes)
  matters: it hides that real ~50s behind agent work instead of it
  being a visible gap between batches. Compare either figure to the
  prior agent-dispatched version's 10+ minutes and 100K+ tokens *per
  issue* (see "Ticket-inlining" below -- same root cause, more severe,
  since even the cheap id/page/col_idx/n_slices fields were being
  regenerated as output tokens by an LLM for no reason). The script
  throws immediately if `args.items` is missing in produce mode -- it
  will not silently fall back to the old agent-based claim/query.
- Each column runs a bounded, automatic content-filter retry ladder:
  **Tier 1** (whole column) → if blocked, **Tier 2** (per-slice
  parallel retry + merge). If a column is *still* blocked after
  Tier 2, it is **not** further auto-escalated — Tier 3 (Opus) and
  Tier 4 (sub-slice) require genre/continuation context a script
  can't reliably construct, so those columns are flagged in the
  final report for manual escalation (`SKILL.md`'s existing tiered
  procedure). This is a deliberate scope boundary, not an oversight.
- Concurrency is NOT manually capped at a fixed number (the old "6"
  or "12" targets) — it uses the `Workflow` tool's own automatic
  concurrency ceiling (`min(16, cpu cores - 2)`), immune to the
  reactive-dispatch lag that caused manual dispatch to rarely hit its
  nominal target. **But that ceiling is hardware-bound, not tunable:
  this machine has 8 cores, so the real ceiling here is 6
  concurrent agents, not 12-16.** Don't expect more than 6 in flight
  on a single `Workflow` run on this machine — that's a structural
  fact, not something to debug further if observed.
- **Ticket-inlining was tried and reverted — do not reintroduce
  without re-measuring at real scale.** The idea: fetch every
  column's full ticket JSON during "Claim & query" and embed it
  directly in each Tier-1 prompt, removing the per-column agent's own
  ticket Read. Measured in production (2026-08-05, aborted run
  `wf_wwyyj3ato`): for issues of 28-56 columns, the query step itself
  took 6+ minutes and 100K-150K tokens per issue and was still
  climbing. Root cause: a `Workflow` script has no file access, so
  any data that reaches the script has to be regenerated as output
  tokens by an agent — batching dozens of multi-KB tickets through
  one structured-output call is far more expensive than each Tier-1
  agent just Reading its own ticket file (measured ~0.4-0.8s per the
  earlier sampled traces). The saving being chased (~0.3-0.5s per
  column, later) was real but tiny; the actual cost of the "fix" was
  minutes and hundreds of thousands of tokens per issue. Reverted to:
  query returns only id/page/col_idx/n_slices (cheap), each Tier-1
  agent Reads its own ticket file as it always did. Same lesson
  applies to the agent-instructions file, which was correctly never
  inlined for the same underlying reason.
- Claiming/querying multiple dates happens sequentially in the
  orchestrator's own Bash step (`orchestrator_claim_query.py` loops
  over dates), but since it's plain Python doing local downloads and a
  SQLite `SELECT` (not agent dispatch), the whole thing still finishes
  in low single-digit seconds for dozens of columns across several
  issues — there's no meaningful "wait for the next issue's download"
  gap in practice, and all issues are ready before `Workflow` is even
  invoked.
- **Not yet proven by a real run:** as of 2026-08-05, only the
  `validate` branch has actually completed successfully. The first
  `produce` attempt (`wf_wwyyj3ato`) was stopped 10+ minutes in during
  the claim/query phase due to the ticket-inlining cost above — it
  never reached the Transcribe phase. That failure is what motivated
  moving claim/query out of the Workflow entirely (see the `items`
  bullet above). The Tier-1→Tier-2 retry ladder and reconciliation
  logic are STILL unproven by a real run, not just "unwatched." Treat
  the next `produce` attempt (using the new orchestrator-side
  claim/query) as the real first test.
- After the transcribe phase, a **reconciliation** step re-queries
  `status` counts per date and only declares an issue complete (runs
  cleanup: `remove_issue_downloads`, `build_repair_stats`, logs to
  `experiments.jsonl`) when the outstanding count is genuinely zero
  — never assumed from the dispatch list.
- **Known limitation:** columns recovered via Tier 2 get an
  approximate (under-counted) `duration_ms` in the timing dashboard,
  because the merge step self-times only its own brief work, not the
  parallel per-slice agents' real effort. This affects dashboard
  accuracy only, not transcript correctness.

### `validate` mode — Haiku-vs-Sonnet comparison

```js
Workflow({
  scriptPath: "<path>",
  args: { mode: "validate", validateDate: "1869-01-29", validateCount: 15 }
})
```

Dual-dispatches both models on the same N columns (no ingest), then
runs an independent review agent per column that reads the actual
source image and checks Haiku's transcript for genuine word-level
errors (not just omissions). Returns a structured report — it does
**not** auto-decide to switch production to Haiku. That decision is
the user's, based on the report. (Status as of 2026-08-05: a
15-column validation batch against 1869-01-29 is running — check
`/workflows` or the run ID logged in `experiments.jsonl` for the
result before deciding whether `produce` mode should default to
`haiku`.)

## Self-timing amendment (why it exists)

`.claude/agents/column-transcriber.md` now has a conditional
self-timing section: when a dispatch prompt explicitly asks for it
(which `transcribe_continuous.js`'s prompts always do), the agent
times its own run via `date +%s%3N` and calls
`transcribe.record_usage` itself, because a `Workflow` script has no
way to read the DB or observe agent wall-clock time directly — only
`agent()` calls (which run inside real Claude Code subagents with
Bash access) can do that. Manual dispatch (the old turn-by-turn
pattern, still fine for small one-off jobs) is unaffected — the
orchestrator still calls `record_usage` after the fact as before,
since that prompt template doesn't request self-timing.

## How to resume a run

```js
Workflow({
  scriptPath: "<path>",
  resumeFromRunId: "<run id, logged in experiments.jsonl each time a run starts>"
})
```

Completed `agent()` calls (same prompt + same options) return cached
results instantly; only new/changed work re-runs. Because the
"what's left" query happens fresh at the start of every `produce`-mode
run anyway, you can also just start a **new** run with the same
`dates` — it will naturally skip everything already `status='done'`
in the DB. The DB is the real checkpoint; the Workflow run ID is a
convenience for not re-paying for agents that already ran in this
exact process.

## Recreating the script if the scratch path is gone

1. Check `instructions/transcribe_continuous.js.snapshot` for the
   last-known-good copy of `transcribe_continuous.js`.
2. Copy it to a fresh path anywhere writable (the session scratch
   directory, or directly into `transcribe/` if you'd rather commit
   it — consider doing this once the design has proven itself over a
   few runs, so it isn't perpetually one accidental `rm` away from
   disappearing).
3. `transcribe/orchestrator_claim_query.py` (the produce-mode
   claim/query step, run directly by the orchestrator, not the
   Workflow script) is already committed in the repo — nothing to
   recreate there.
4. Invoke via `Workflow({ scriptPath: "<new path>", args: {...} })`
   per the modes above (produce mode needs `args.items` — run
   `orchestrator_claim_query.py` first, see the `produce` mode section
   above).

## Current status (update this section, don't let it go stale)

**Last updated: 2026-08-06.** Task #12 (5 decade-gap issues:
1900s/1920s/1930s/1950s/1970s) is fully complete, plus its 3
follow-up decade issues (1869-01-29, 1886-07-16, 1897-11-05). Two
continuous-queue production runs are in flight concurrently
(`wf_407ff5f5-eb1`: 1870-01-08/1903-05-22/1932-11-11;
`wf_383cc9e6-62d`: 1925-05-01/1955-06-16/1970-04-09) — see the
"task #12 follow-ups" and later update sections below for the
factory-system fixes, per-day timing chart, and concurrency-stacking
confirmation that came out of this work. `repair_monitor.html` was
redesigned this session: a full-width "issues completed, day by day"
bar chart now sits below the existing per-day speed line chart
(identical x-axis geometry/day-range so the two compare directly),
and all chart explanations moved into click-triggered `(i)`-button
popovers to cut clutter. Page retitled "Transcription monitor".

**Known harness quirk — `args` arrives as a JSON string, not an
object.** Despite the `Workflow` tool's own documentation saying to
pass `args` as a real JSON value (not a string) and that it's exposed
to the script "verbatim," three real runs
(`wf_9072929e-16a`, `wf_3897b44b-ddb`, `wf_a136244b-37e`) confirmed via
a forced diagnostic error that `typeof args === 'string'` at runtime
in this environment, even though the tool call passed a genuine
object. **`transcribe_continuous.js` now normalizes this defensively**
(`const runArgs = typeof args === 'string' ? JSON.parse(args) : (args || {})`,
near the top of the file) — always read `runArgs`, never bare `args`,
if you edit this script. If a future session writes a *new* Workflow
script from scratch, apply the same defensive parse rather than
trusting the documented behavior — this was reproduced 3 times, it is
not a fluke.

**Validation result (2026-08-05, `wf_a4c838f3-c95`): Haiku is NOT
ready for production, hardening or not.** 15 columns from 1869-01-29,
dual-dispatched Haiku vs Sonnet, each independently reviewed against
the source image (not against Sonnet — Sonnet was a second opinion
only). Result: **231 confirmed genuine word-level hallucinations**
(~15/column), including a character's surname spelled 3 different
wrong ways within one batch, sentence-level fabrications with no
basis in the source text, a wrong real place name (Brockville read as
"Brookville"), wrong dates, and — the specific failure mode the
self-check hardening was written to catch — fabricated content over a
source region that was genuinely blank, which Sonnet correctly
flagged as empty via `repair_needed`. Multiple independent reviewers
explicitly noted the hardening did not appear to reduce the error
rate. **Decision: the 3 queued decade issues (1869-01-29, 1886-07-16,
1897-11-05) should run on `sonnet`, not `haiku`, via `produce` mode.**
Do not revisit Haiku for bulk production without either a materially
different prompting approach or accepting a human-review pass on
every Haiku column — the self-check section alone is confirmed
insufficient.

**Factory-system rework (2026-08-05).** The first real `produce`
attempt (`wf_wwyyj3ato`) got stuck 10+ minutes into claim/query for
the 3 queued issues and was aborted (see "Ticket-inlining" above —
same root cause, worse: even the cheap id/page/col_idx fields were
being regenerated as agent output tokens for no reason). Per explicit
user direction ("that is precisely what I meant by a factory system —
rework the process to handle that"), claim/query moved entirely out
of the Workflow script and into a plain Python script
(`transcribe/orchestrator_claim_query.py`) run directly by the
orchestrator via Bash, since it's mechanical work (download + slice +
a `SELECT`) with no LLM judgment involved. Measured: **4.06s wall,
zero tokens** for all 134 outstanding columns across the 3 queued
issues (28+50+56), vs 10+ minutes/100K+ tokens per issue before. The
`produce`-mode branch of `transcribe_continuous.js` now requires
`args.items` (throws if missing) instead of deriving it internally.
`validate` mode is unchanged (still claims/queries via agent — small
comparison batches, not bulk, so the cost tradeoff doesn't apply the
same way). **First real produce-mode Workflow run using this design
is launching now** — still unproven end-to-end until it completes.

**Live status as of this run (2026-08-05/06, `wf_0fb941b2-c73`).**
Produce-mode run in progress on 1869-01-29/1886-07-16/1897-11-05
(sonnet, 134 items). 1869-01-29 finished fully (28/28) partway
through; the other two are still going. Two standalone `Monitor`
watchers are running alongside it, **entirely outside the Workflow's
agent pool** (same "mechanical work stays with the orchestrator"
principle as claim/query, so they cannot compete with the transcribe
agents for the 6-slot concurrency cap):
- **Page-completion refresh** (task `bfnb4fokr`): polls DB every 25s,
  reruns `build_repair_stats.py` only when a page's columns all hit
  `status='done'`, self-exits when the tracked dates are fully done.
  Answers "the monitor should update per page, not per issue" without
  touching the agent pool at all.
- **Stall watch** (task `bsi7gyxbu`): polls every 60s, tracks each
  running agent's transcript-file byte growth (not just elapsed time),
  flags `STALL SUSPECTED` only past a 300s floor AND 3 consecutive
  no-growth polls (180s of literal silence). The floor exists because
  legitimate columns have been measured up to 452s — a naive
  elapsed-time-only threshold would false-positive on real work.

**End-of-issue slowdown, root-caused (not just observed).** The
"we sometimes get problems at the end of an issue" pattern flagged
earlier is real and was directly confirmed on 1869-01-29: durations
climbed from a page-1 average of ~221s/column to a page-3 peak of
452s. Read the full agent transcripts (not just `duration_ms`) for the
two slowest columns (p3c5=452s, p3c6=363s): **both show one large
silent gap (397.7s / 312.7s) with zero tool calls**, between reading
the 4 slice images and writing the result — pure model generation +
self-check reasoning time, not a stuck agent, not image resampling,
not a tool-call loop. Both columns have genuinely hard source content
(unregistered price-list ad cropped at the edge; smudged/low-legibility
text with left-edge cutoff), consistent with the wider page-3 pattern.
**Conclusion: this specific slowdown is real content difficulty, not
infrastructure lag** — don't "fix" it by adding timeouts/kills without
separately verifying that on a case that DIDN'T just resolve on its
own.

**Known bug, fix deferred until this run completes (task tracked
in-session; hold off editing `column-transcriber.md` while agents are
actively reading it).** The self-timing snippet `date +%s%3N` fails on
macOS — `%N` isn't supported, so it prints a literal `N` and the
follow-up arithmetic throws `bad math expression`. Every agent
observed so far self-recovers on a 2nd/3rd retry by falling back to
whole-second precision, costing ~15-20s per column and explaining why
most recorded `duration_ms` values round to 1000ms. Fix: swap to a
portable elapsed-time method (e.g.
`python3 -c "import time; print(int(time.time()*1000))"`) in the
"Writing the result file" self-timing section.

**Next-issue prep (zero-gap handoff, in progress).** 1870s is the next
most under-covered decade (514 issues in corpus, only 1 with any
transcribed columns; 1980s is explicitly out of scope — still in
active cutting/QA, not ready for transcription). `1870-01-08` was
picked (clean: full `page_layouts` coverage, 0 done, 4 pages) and
pre-claimed via `orchestrator_claim_query.py` while this run was still
going — **took 50.73s for 28 columns (~1.8s/column), not the ~1-2s
seen for the 3 dates above**, because this is a genuinely cold date
(never touched before) vs. those 3 which were already Drive-cached
from being picked/eyeballed earlier in the project. Don't assume claim
is always sub-2s — a cold issue's real cost scales with column count,
which is exactly why prepping it during the current run's Transcribe
phase (rather than after) matters. Items are ready in
`claim_query_1870.json`; launch as the next produce-mode `Workflow`
call.

## Update, 2026-08-06 — task #12 follow-ups complete, factory-system fixes landed, first post-fix production run done

The 3 queued follow-up issues (1869-01-29, 1886-07-16, 1897-11-05,
134 columns) completed via `wf_0fb941b2-c73`. A full JIT-style waste
audit ran against its real transcripts (per user request — "1s ×
250,000 columns = 3+ days," find every small waste). Confirmed
findings, all landed:

- **Self-timing removed entirely** from `column-transcriber.md` — no
  more agent-side `date`/`record_usage` calls. The orchestrator now
  computes `duration_ms`/`tool_calls` from each agent's own transcript
  timestamps after the fact (a `Monitor` loop, not a Workflow
  `agent()` call, so it never touches the transcribe concurrency
  pool). More accurate than self-timing ever was, and removes the one
  Bash pattern per agent that fell outside the `Bash(python3 *)`
  allowlist (it caused the run's only real permission denial).
- **`ads_in_column[].masked` is now computed data, not a guess** —
  `claim_columns.py` checks pixel std/mean on each ad's region at
  ticket-build time (verified: confirmed-masked region measured
  std=0.0/mean=255.0 exactly; real content measured std=88-101).
  `column-transcriber.md` trusts this field outright and no longer
  flags `repair_needed` for the ordinary masked/unmasked case. Also
  added: ads commonly span multiple columns (a cut fragment isn't
  automatically a boundary defect), and agents don't need to
  reconcile semantic sense out of an ad fragment split across a
  column edge — but genuine lost article/body text still gets
  `column_boundary` flagged as before.
- **Batching investigation, fully resolved (not left open).** Giving
  agents every slice path upfront (so they don't need to read the
  ticket first to discover them) was tried to enable single-turn
  batching. It didn't change behavior — checked three ways (production
  transcripts, a self-test, two controlled subagent dispatches) before
  concluding: real production agents show flat 0.02–0.66s gaps between
  reads with no hidden per-round cost, so batching saves close to
  nothing for this pipeline specifically. Don't re-open this without
  new evidence — it was checked at full resolution, not assumed.
- **`build_repair_stats.py` corpus-cache added** — the corpus-wide
  structural query (44,826 `page_layouts` rows) was being recomputed
  on every single page-completion event by the live monitor; now
  cached for 1hr since it only changes when the cutting pipeline runs.
  10.9× speedup (3.27s → 0.30s cold vs warm), verified identical
  output.
- **Orphaned-row bug fixed in three places.** A re-cut column creates
  a new DB row (new `image_sha256`); the old row stays for history.
  The monitor, `orchestrator_claim_query.py`, and
  `transcribe_continuous.js`'s `reconcileDate`/`claimAndQuery` were
  all counting orphaned pre-recut rows as real outstanding work.
  Fixed via a `MAX(created_at)`-per-position filter in all three.
  Caught live: the user asked to finish "the last column" of
  1871-06-16, which turned out to already be fully done.
- **`ASSUMED_CONCURRENCY_FRACTION` tuned 0.75 → 0.85** based on real
  wall-clock data (81.7 min actual vs 104.3 min estimated for
  `wf_0fb941b2-c73`, implying ~0.957 real achieved concurrency).

A short 4-column validation test (`wf_87bc9020-b8c`, on pre-claimed
`1870-01-08` tickets) confirmed the self-timing removal and
masked-field trust both work correctly in practice before committing
to a full run.

**First full production run on the rewritten pipeline: `1947-05-01`**
(randomly picked from 461 clean 1940s candidates), 55/55 columns,
via `wf_19231888-ea6`. Two more bugs found and fixed during
reconciliation:

- **Reconcile schema ambiguity** — `reconcileDate`'s Python query only
  emits keys that actually occur (`{"done": 55}` when nothing is
  `claimed`), and the agent filling the `{done, claimed}` structured-
  output schema apparently defaulted the missing key to the wrong
  value (`claimed: 55` when it should have been `0`), so the Workflow
  reported `complete: false` and skipped cleanup despite the issue
  being genuinely done. Fixed: the query now always emits both keys
  explicitly via `.get(..., 0)`. Caught by checking the real DB
  directly rather than trusting the Workflow's own completion report
  — verify against source of truth, every time, no exceptions.
- **Usage-backfill result-format variance** — one agent's final status
  line started with the raw UUID instead of the word "Ingested"
  (`"e4fd43fc-... ingested: ..."` vs the expected `"Ingested
  e4fd43fc-...: ..."`), so the backfill monitor's strict prefix match
  missed it. Manually backfilled; not yet hardened for next time (see
  TODO below).

**Still open / TODO for the next session:**
- Broaden the usage-backfill monitor's match to check for a UUID
  pattern anywhere in the result text alongside case-insensitive
  "ingested", rather than an exact string-prefix match.
- Add the 2-minute rebuild debounce (already built and used for
  `1947-05-01`'s monitor) as the default in any future monitor script,
  not something re-authored per run.
- `1870-01-08` (28 columns, already claimed with the new `masked`
  field) is still queued and unclaimed-for-production — a natural
  next run whenever more decade-gap coverage is wanted.
call the moment `wf_0fb941b2-c73` finishes.
