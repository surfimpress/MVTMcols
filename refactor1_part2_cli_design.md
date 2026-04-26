# Refactor 1 Part 2 — LLM-operations CLI surface (design)

**Status:** design only. Not implemented. Out of refactor-1 scope.
**Companion:** Part 1 (split_page CLI alignment) landed 2026-04-26 in
commit `4039ca0` on the `refactor-1` branch.

## Why this exists

The MVTM workflow is moving toward a two-stage pipeline:

1. **Classic Python** — `process_issue.py` cuts up an issue (column
   boundaries, ad boxes, headlines, body-text strips, column PNGs).
2. **LLM follow-up** — for each cut-up issue, an LLM agent does three
   jobs:
   - **(a) Anomaly correction.** Spot cases the Python pipeline got
     wrong (a column boundary in the wrong place, an ad bbox that
     missed half the ad, a headline strip that bled into the byline,
     two articles fused into one segment) and fix them.
   - **(b) Diplomatic transcripts.** Produce a faithful diplomatic
     transcript for each segmented unit (column-piece, ad, headline).
   - **(c) Cataloguing.** Classify each unit (article / ad / notice /
     letter / etc.), extract structured metadata (titles, bylines,
     places, dates, prices), and emit a catalogue record.

Anomaly correction (a) is what creates the CLI requirement. The agent
needs primitive operations to act on: "recut this page with a different
column count," "shift this ad's right edge by 2%," "re-extract this
column's headline strip after I move the boundary." Those operations
must use **the same functions the pipeline uses** so a corrected page
ends up in the same shape and downstream layers (transcripts,
catalogue) can be re-run idempotently.

Part 1 was the warm-up: kill the parallel `split_page` implementation
so there's one detection chain. Part 2 turns the column-pipeline into
a callable CLI surface.

## Shared-functions principle

Each CLI command is a thin orchestrator that:

1. Reads existing state from the DB / disk.
2. Calls the **same pipeline functions** `process_issue` calls.
3. Writes results back to the DB / disk in the **same schema**.
4. Marks the affected rows as hand-edited (so a future
   `process_issue` rerun doesn't clobber them — see "Hand-edit
   markers" below).
5. Re-runs the downstream layers that depend on the changed input.

The shared-functions rule is the load-bearing constraint. If a CLI
ever invokes its own column-detection helper, or its own ad-bbox
adjustment math, drift starts on day one. **Every CLI command's
implementation must be ≤ ~30 lines** — read, dispatch, write. If it's
longer, it's smuggling logic that belongs in the pipeline.

## Tooling shape — single CLI vs. many

Three options were considered:

| Option | Pros | Cons |
|---|---|---|
| **One CLI per operation** (`recut_page.py`, `adjust_ad.py`, …) | matches existing pattern; no shared CLI framework | each gets its own arg parser, drift between args |
| **One `mvtm` umbrella with subcommands** (`mvtm recut-page`, `mvtm adjust-ad`) | shared parser, shared error handling, shared DB connection | requires picking + adopting a CLI framework |
| **Existing scripts grow flags** (`process_issue.py --recut-page=2`) | minimal new code | overloads the orchestrator; conflates batch and surgical operations |

**Recommendation: option 2** — a single `mvtm` umbrella in a new
`mvtm_cli.py` that dispatches subcommands. argparse subparsers handle
this without external dependencies. Keeps the existing per-stage
scripts (`split_page.py`, `detect_ads.py`, etc.) as standalone
diagnostic CLIs; the umbrella is the LLM-facing surface.

## The command set — first cut

### `mvtm recut-page <year> <month> <day> <page> [--columns N] [--pitch P]`
Re-run column detection for one page using overrides. Calls
`detect_strips → cluster_boundaries → place_columns` with a
`PageContext` whose `issue_columns` / `issue_pitch` come from the
flags instead of the corpus aggregate.
- Reads: existing `page_layouts`, `page_geometry`, `detected_ads`
  rows for the page.
- Writes: replaces the `page_layouts` row; replaces column PNGs in
  `columns/<date>/p<n>/`; rewrites `page_meta.json`,
  `page_analysis.json`. Marks the row hand-edited.
- Re-runs: nothing automatically. The user (or LLM) chains it with
  `mvtm recompute-layers` if downstream layers need refreshing.

### `mvtm adjust-ad <ad_id> [--x PCT] [--y PCT] [--w PCT] [--h PCT]`
Move/resize a single ad bbox. Each `--x/--y/--w/--h` is **absolute**
in page-pct coordinates; missing flags leave that dimension alone.
- Reads: `detected_ads` row for the id.
- Writes: updated `detected_ads` row; regenerated ad cutout PNG (uses
  `extract_ad_images` from `detect_ads.py`); regenerated column PNGs
  for the affected page (because column PNGs have ad-region holes
  punched out — `extract_columns(... ads_with_ids=…)` is the shared
  function). Marks the row hand-edited.

### `mvtm split-ad <ad_id> --at-y PCT`
The Python pipeline sometimes fuses two stacked ads into one
rectangle. `split-ad` divides one row into two at the given y
coordinate, inheriting x extent from the parent. Inverse:
`mvtm merge-ads <id1> <id2>`.

### `mvtm extract-column <year> <month> <day> <page> <col_idx>`
Re-run `extract_columns` for one column without touching detection
boundaries. Useful when the LLM has fixed an ad bbox and just needs
the column PNG rebuilt. Pure call into shared `extract_columns(…)`.

### `mvtm recompute-layers <year> <month> <day> <page> [--layers L1,L2,…]`
Re-run the post-detection layers (headlines, body-text strips,
horizontal rules, large-type detection) for one page. Calls the
existing `detect_headlines`, `detect_body_text`, etc. directly.
Leaves boundaries and ads alone.

### `mvtm rerun-issue <year> <month> <day> [--from-stage STAGE]`
Convenience: run `process_issue` for one issue, but skip stages that
are already marked hand-edited. `--from-stage` lets the LLM say "I
trust ads, redo everything from boundaries onward."

### `mvtm show <year> <month> <day> <page>`
Read-only inspector: dumps the current layout, ad list, headline
list, body-text strips, hand-edit markers — everything the LLM might
need before it decides what to act on. JSON output by default; flag
for human-readable.

## Hand-edit markers

Each pipeline-managed row needs a way to tell `process_issue` "don't
overwrite me on rerun." Proposal: a `hand_edited` column on each
table that gets edited by CLI commands.

- `page_layouts.hand_edited` (BOOLEAN, DEFAULT 0)
- `detected_ads.hand_edited` (BOOLEAN, DEFAULT 0)
- `page_geometry.hand_edited` (BOOLEAN, DEFAULT 0)

`process_issue` checks the flag before each write and skips
hand-edited rows (with a `[skip P3 hand-edited]` log line so it's
visible). The CLI commands set the flag to 1 on every write.

A bulk reset is one SQL statement when the LLM wants to throw away
its corrections and let the pipeline re-decide:
`UPDATE page_layouts SET hand_edited = 0 WHERE …`.

**Tradeoff considered:** instead of a flag, store the override in a
parallel `hand_edits` table that `process_issue` joins on read. That's
cleaner but adds a join everywhere; the flag-on-row approach is
ugly-but-local and matches the existing one-row-per-page schema.

## Rollback

Every CLI command:

1. Snapshots the affected DB rows to a `cli_history` table before
   writing (`(table, row_id, before_json, after_json, ts, command)`).
2. Returns a transaction id.
3. Supports `mvtm undo <transaction_id>` to restore the snapshotted
   rows and regenerate the affected files.

Cheap to implement (a single decorator wrapping each write), and the
LLM gets a real undo button without us needing to rely on git for the
DB. The DB backup pattern (`data/mvtm.db.pre-*.bak`) we use for
refactors stays — `cli_history` is for the LLM's per-command rollback,
not for catastrophic recovery.

## Output discipline

The LLM is going to read these CLI outputs as input to its next
action. Two requirements:

1. **JSON by default, human-readable on `--human`.** The LLM should
   not have to parse a "Page: P3 7c [11% 11% 11%]" line.
2. **Stable schemas.** Once a command's JSON output ships, the field
   names and types are frozen. Adding fields is fine; removing or
   renaming requires a version bump (`mvtm --output-version=2 …`).

## Failure modes — the LLM must see them

When a CLI command fails, the failure must surface in JSON so the
LLM can react. Categories:

- `validation_error` — bad arguments (e.g. `--y 110` is out of range).
  The LLM should retry with corrected arguments.
- `not_found` — the row/file the LLM referenced doesn't exist. The
  LLM should call `mvtm show` to refresh its view.
- `pipeline_error` — a shared pipeline function raised. The LLM
  should NOT retry blindly; surface the traceback and stop.
- `would_clobber_hand_edit` — operation would overwrite a hand-edited
  row without `--force`. The LLM should decide whether to override.

Each failure category gets a stable error code in the JSON output.

## What to build first

A walking skeleton, then pad it out:

1. **`mvtm show <…>`** — pure read, no writes, no schema changes.
   Forces us to nail the JSON schema before we start mutating things.
2. **`mvtm recompute-layers <…>`** — also no schema changes (layers
   are regenerated outputs, not DB rows). Exercises the shared-call
   pattern end-to-end.
3. **`hand_edited` columns + `cli_history` table.** Schema migration
   needs a DB backup and care. Land this before any mutating command.
4. **`mvtm adjust-ad`** — first mutating command. Smallest blast
   radius (one ad row, one image regen).
5. **`mvtm extract-column`** — second mutating command. Tests the
   ads-with-ids → column-PNG path under CLI invocation.
6. **`mvtm recut-page`** — the big one. Re-runs detection.
7. **`mvtm split-ad` / `merge-ads`** — once the pattern is mature.
8. **`mvtm rerun-issue --from-stage`** — last; ties everything
   together.

Each step is a small commit on its own branch. None should land
without a regression on at least one issue (1947-11-06 is the
incumbent baseline; a freshly-cut issue from a different era would
be useful too).

## What this design deliberately doesn't decide

- **LLM ↔ CLI integration.** Whether the LLM calls `mvtm` via shell,
  via an MCP server, via a Python API — that's the job of whatever
  layer drives the LLM agent. The CLI just needs to be callable from
  any of those.
- **Concurrency.** The CLI assumes one operation at a time. If the
  LLM agent ever runs commands in parallel against the same issue,
  we'll need row-level locking — but cross that bridge when we have
  evidence we need it.
- **Per-region edits inside a column PNG.** The LLM might want to
  draw a box that says "this segment is one article." That's a
  *segmentation* operation, not a *layout* operation, and belongs in
  whatever Stage-2 system replaces `find_splits.py` /
  `classify_segments.py`. Not part of this CLI surface.

## Outstanding questions for the next session

1. **Argparse subparsers vs. Click.** The codebase has zero CLI
   framework dependencies today. argparse keeps it that way; Click
   gives nicer ergonomics. Slight preference for argparse to match
   the existing pattern, but worth a one-line decision.
2. **JSON output format.** Per-command, or a uniform envelope with
   `{ok, command, transaction_id, result, errors}`? Slight
   preference for the envelope — gives the LLM a single shape to
   parse.
3. **Existing per-stage CLIs.** Keep them as-is (intended diagnostic
   tools), or have the umbrella absorb them (`mvtm split-page` runs
   what `python split_page.py` does today)? Slight preference for
   keep-as-is — they're for humans, not the LLM agent.

When we return to this, the answers to those three are the first
fork in the road.
