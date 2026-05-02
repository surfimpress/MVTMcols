# transcribe/ — scope notes for Claude Code sessions

This folder is the transcription pipeline for the MVTM project. It
is independent of the parent cutting pipeline: it reads `mvtm.db`
read-only via `ATTACH DATABASE` and never writes to it.

Read `transcribe/README.md` for the full picture. The notes here
are the things you (a future Claude Code session) need at hand
before touching this folder.

## The three layers, and where they live

The LLM work is split across three files. **Do not collapse
them.** They earn their keep by separating concerns that change at
different rates and serve different audiences.

| Layer | File | What it holds |
|---|---|---|
| Agent definition | `.claude/agents/column-transcriber.md` | YAML frontmatter (name, default model, tools) + the durable transcription instructions. **Source of truth** for what a column transcriber does. |
| User-invocable workflow | `.claude/skills/transcribe-issue/SKILL.md` | The procedure the orchestrator follows: claim → dispatch agents in parallel → ingest. Triggered by `/transcribe-issue YYYY-MM-DD`. |
| Per-call ticket | `transcribe/work/columns/<row-id>.json` | The variable bits: image path, column position, neighbour boundaries, registered ads, h-rules, prompt-hash. Written by `transcribe.claim_columns`. |

When the **transcription instructions** change (what to flag, the
response shape, the cross-check rules), edit
`.claude/agents/column-transcriber.md`. The change is visible in
`git log` and the prompt-hash on every subsequent ticket reflects
it.

When the **orchestration procedure** changes (batch size, default
model, comparison run logic), edit
`.claude/skills/transcribe-issue/SKILL.md`.

When **what we collect per call** changes (e.g. add another upstream
signal to the ticket), edit `transcribe/claim_columns.py`.

## The Python boundary

Python in `transcribe/` does **not** call any LLM. It only:

- manages SQLite state (`transcribe.db`),
- prepares per-call tickets,
- ingests agent results.

The orchestrator (Claude Code in your current session) is the one
that calls the Agent tool and gets transcripts back. If you're
tempted to add an `anthropic` SDK dependency or a `call_llm.py`
module, **stop**. The LLM work runs under the user's Claude Code
subscription via the Agent tool; that decision is durable
(`feedback_use_local_agents_not_api.md` in user memory).

## Coordinates and DB rules

- All bounding boxes are page-percentages. pct↔px conversions go
  through the parent project's `coordinates.py`. Never write
  inline `int(x_pct / 100 * w)` in this folder either.
- `mvtm.db` is read-only here. The ATTACH uses `?mode=ro` so it's
  structurally enforced, not just convention.
- The `repairs` table flags upstream issues but does not
  auto-execute fixes. Each repair carries a `suggested_cli` field
  with the `mvtm_cli.py` invocation needed; running it is the
  user's call.

## Re-cuts and history

A column or ad whose PNG SHA-256 changes (because the cutting
stage was re-run) creates a **new** transcript row. The old row
stays for history. The unique key is `(year, month, day, page,
col_idx, image_sha256)` for columns, `(ad_uuid, image_sha256)` for
ads. Don't change this — it's the audit trail for the repair
loop.

## What the orchestrator does and doesn't do

**Does:**
- Run the claim / dispatch / ingest loop (the `transcribe-issue`
  skill).
- Pass each ticket to a `column-transcriber` agent via the Agent
  tool, optionally with a `model:` override for comparison runs.
- Save agent results to `transcribe/work/results/<row-id>.json`
  before invoking the ingester.

**Doesn't:**
- Run `mvtm_cli.py` mutators on the user's behalf — repair acts
  are explicit, surfaced through `transcribe.repairs`.
- Auto-batch across many issues without checking in. Each issue
  is a discrete unit of work the user has agreed to.
- Mix model results into one row. If both Haiku and Sonnet ran
  the same column, surface both to the user and let them choose
  the canonical one before ingest.
