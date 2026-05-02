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

A typical session:

1. User says "transcribe 1892-01-01 columns".
2. Claude Code runs `python3 -m transcribe.cli claim columns 1892-01-01`
   — produces N pending tickets in `transcribe/work/columns/`.
3. Claude Code reads the tickets and dispatches Agent subagents
   with the prompt template + image + context block. Multiple
   subagents in parallel.
4. As each subagent returns its JSON envelope, Claude Code runs
   `python3 -m transcribe.cli ingest column <id>` to write the
   result back into `transcribe.db`.
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
