# transcribe — MVTM transcription pipeline

A two-pass pipeline that turns the column / ad PNGs produced by the
parent MVTM project into diplomatic transcripts (pass 1) and then
interpreted *items* with extracted metadata (pass 2).

## Why it lives in its own subfolder

This pipeline runs at its own pace and must not bottleneck the
column / ad cutting in the parent project. It writes to its own
SQLite database (`transcribe/data/transcribe.db`) and reads parent
state read-only by attaching `data/mvtm.db`.

Nothing in the parent project imports from `transcribe/`. The only
direction of dependency is `transcribe/` → parent (read-only DB
access, on-disk PNG reads, and the parent's `mvtm_cli.py` for the
repair execution layer).

## How the LLM work happens

This pipeline does **not** call the paid Anthropic API. The Python
code here manages state, prepares units of work, and ingests
results. The actual transcription / interpretation is done by
Claude Code spawning subagents via the Agent tool — covered by the
user's Claude Code subscription.

### Three layers, three files

The work is structured into three layers, each with a separate
home so versioning, reuse, and per-call data stay clean:

1. **The agent definition** lives in
   `.claude/agents/<name>.md` (project root, not under
   `transcribe/`). The YAML frontmatter sets the agent's name,
   description, default model, and tool restrictions; the body
   holds the durable transcription instructions (what
   "diplomatic transcript" means, what to ignore, what to flag,
   the response JSON shape, the cross-check rules, the tolerance
   for slanted columns). **The agent file is the source of
   truth for the durable instructions.** When the instructions
   change, edit the agent file — the change is visible in
   `git log` and in the prompt-hash on every subsequent ticket.

2. **The user-invocable workflow** lives in
   `.claude/skills/transcribe-issue/SKILL.md`. It documents the
   procedure the orchestrator (Claude Code in an interactive
   session) follows: claim → dispatch agents in parallel →
   ingest. The user can trigger it with `/transcribe-issue
   1892-01-01`. The skill does not itself read images or
   produce transcripts — that's the agent's job. The skill
   captures the orchestration decisions (parallel batch size,
   default model, what to do on Haiku-vs-Sonnet comparison runs).

3. **The per-call ticket** is a small JSON file under
   `transcribe/work/columns/<row-id>.json`, written by
   `transcribe.claim_columns`. It carries only the variable
   bits: image path, image SHA-256, column position, neighbour
   boundaries, registered ads, h-rules in this column, and a
   prompt-hash. The orchestrator reads the ticket, sends the
   image and the ticket JSON to a `column-transcriber` agent
   via the Agent tool, and waits for the JSON envelope.

### Why three layers, not one big template

Earlier in the design the durable instructions and the per-call
context were a single Markdown template with a `{{CONTEXT_BLOCK}}`
placeholder. Splitting them lets us:

- **Version the instructions cleanly.** A change to "what
  diplomatic means" is a change to the agent file; per-call
  data didn't change. The diff is honest.
- **Reuse the same agent across pipelines.** A
  `column-transcriber` and (later) an `ad-transcriber` can share
  most of the body and differ only in the per-call payload they
  receive.
- **Pick the model per call.** The Agent tool's `model:`
  parameter overrides the agent's default frontmatter, so a
  single `column-transcriber` definition serves both the
  Sonnet-default production runs and the Haiku-vs-Sonnet
  comparison runs.
- **Hash the design fingerprint, not the call.** `prompt_hash`
  on each row covers (agent body + per-call context) — stable
  across model overrides, sensitive to instruction changes, so
  we can tell which "design" produced a given transcript.

### A typical session

1. User runs the slash command `/transcribe-issue 1892-01-01`,
   or asks "transcribe 1892-01-01 columns" in plain English.
2. Claude Code reads the skill, then runs
   `python3 -m transcribe.claim_columns 1892-01-01` — produces
   N pending tickets in `transcribe/work/columns/`.
3. Claude Code reads the new tickets and dispatches
   `column-transcriber` agents in parallel via the Agent tool,
   passing the image path and the per-call context JSON in the
   `prompt:` parameter, and (optionally) overriding `model:` for
   a Haiku/Sonnet comparison run.
4. As each agent returns its JSON envelope, Claude Code runs
   `python3 -m transcribe.ingest_column_result <row-id>` to
   write the result back into `transcribe.db`.
5. Pass-2 (items) follows the same loop with per-page tickets.

## Two databases

- `data/mvtm.db` — owned by the parent. Opened **read-only** here
  via `ATTACH DATABASE 'data/mvtm.db' AS mvtm` (using the
  `?mode=ro` URI).
- `transcribe/data/transcribe.db` — owned by this pipeline.
  Created by `python3 -m transcribe.bootstrap_db`.

Cross-database joins use the `mvtm.` schema prefix in plain SQL.

## Quick start

```bash
# One-time setup: create the DB.
python3 -m transcribe.bootstrap_db

# (Future) claim work for one issue:
python3 -m transcribe.cli claim columns 1892-01-01
python3 -m transcribe.cli claim ads     1892-01-01

# (Claude Code session does the agent dispatch here.)

# Ingest results:
python3 -m transcribe.cli ingest column <ticket-id>

# Pass-2:
python3 -m transcribe.cli claim items 1892-01-01
# (agents run; ingest results)

# Inspect:
python3 -m transcribe.cli status --issue 1892-01-01
python3 -m transcribe.cli show item <id>
python3 -m transcribe.cli export 1892-01-01 --format md
```

## Repairs

Either pass can raise a structured repair ticket when it spots:

- a column cut that's wrong (too narrow, too wide, missing column,
  extra column),
- an ad span / height that's wrong,
- physical page damage or printing issues,
- transcripts that suggest items were incorrectly fused or split.

Repair tickets live in the `repairs` table. The pipeline never
auto-mutates `mvtm.db`. Instead, each repair carries a
`suggested_cli` field with the `mvtm_cli.py` invocation needed to
fix the upstream state. Running it is a manual step. After the
fix, re-running the relevant pass picks up the new image content
(by SHA-256) and creates a fresh transcript row; the prior row
stays for history.

```bash
python3 -m transcribe.cli repairs list --status open
python3 -m transcribe.cli repairs show <id>
python3 -m transcribe.cli repairs act  <id>   # prints the suggested CLI
python3 -m transcribe.cli repairs resolve <id> --note "fixed via cut adjust"
```

## Schema

The canonical schema is `transcribe/schema.sql`. Read it for the
full picture. Highlights:

- `column_transcripts` — pass-1A. One row per column-image content.
- `ad_transcripts`     — pass-1B. One row per ad-image content.
- `items`              — pass-2. Discrete units of newspaper
  content (article / display_ad / classified_ad / notice /
  masthead / cartoon / letter / announcement / table / index /
  other). Each carries a bounding box anchored to known column
  edges and h-rules.
- `item_column_spans`  — many-to-many: item ↔ column-transcript
  with char offsets and per-column vertical extent.
- `item_ad_associations` — items that *are* ads link to MVTM ad
  UUIDs.
- `people`, `organizations`, `places`, `products`, `events` —
  entity tables. Each has a `normalised_key` for first-pass loose
  dedup.
- `item_<entity>_mentions` — junctions with role + exact
  `mention_text` + char offsets.
- `repairs` — structured repair tickets.
- `transcribe_runs` — orchestrator audit log.

## Coordinates

All bounding boxes are page-percentages, never pixels. pct↔px
conversions go through `coordinates.py` in the parent repo. See
`/Users/peter/Projects/MVTM/CLAUDE.md` for the rule.

## Independence rules

- `transcribe/` does not write to `mvtm.db`. Period.
- `transcribe/` does not modify on-disk PNG / JSON artefacts in the
  parent. It reads them.
- Repairs flag upstream issues but do not auto-execute fixes.
- The parent project does not import anything from `transcribe/`.

## File map

```
.claude/agents/column-transcriber.md   # agent definition + instructions
                                       # (source of truth for what
                                       # transcribers do)
.claude/skills/transcribe-issue/
       SKILL.md                        # the /transcribe-issue workflow

transcribe/
  CLAUDE.md                            # auto-loaded scope notes for
                                       # future sessions in this folder
  README.md                            # this file
  schema.sql                           # canonical schema
  bootstrap_db.py                      # creates transcribe.db
  db.py                                # connection + state-transition
                                       # helpers
  claim_columns.py                     # writes pending column tickets
  ingest_column_result.py              # (next) writes one result back
  tests/                               # schema round-trip + future
  data/transcribe.db                   # (gitignored)
  work/columns/<row-id>.json           # (gitignored) per-column tickets
  work/results/<row-id>.json           # (gitignored) agent results
```

The agent file and the skill file are the only `.claude/`
contents tracked in git — see `.gitignore` for the negation rule.
