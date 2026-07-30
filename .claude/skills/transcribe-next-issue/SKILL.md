---
name: transcribe-next-issue
description: Pick a random untranscribed issue spanning the full run, transcribe all its columns, then delete the local downloads. Designed to be called from /loop or a schedule.
---

# /transcribe-next-issue [--max-year YYYY]

Pick one random untranscribed issue from across the run (default
1861–1979, spanning diverse periods rather than just the earliest
decades), run the full column-transcription loop for it, then
delete the downloaded PNGs to keep local disk free. Designed to be
called repeatedly via `/loop` or a scheduled agent.

This skill is intentionally gentle: one issue at a time, sequential
column batches, 0.5 s between Drive downloads. The goal is steady
background progress without taxing Claude or triggering Drive
rate-detection.

## Steps

### 1. Pick the next issue

```
python3 -m transcribe.pick_issue [--max-year YYYY]
```

Prints one `YYYY-MM-DD`. If it prints nothing (all done or no data),
report completion and stop. Capture the date string for the steps below.

### 2. Claim columns (downloads from Drive)

```
python3 -m transcribe.claim_columns YYYY-MM-DD
```

Downloads each column PNG from Google Drive (~0.5 s per file) into
`transcribe/work/downloads/YYYY-MM-DD/`, slices them locally into
`transcribe/work/slices/<row-id>/`, and writes tickets to
`transcribe/work/columns/<row-id>.json`.

Print the claim summary. If 0 tickets were written (all already done),
skip to step 5 (cleanup).

### 3. Transcribe the columns

Follow the same agent-dispatch procedure as `/transcribe-issue`:

- Read all ticket files in `transcribe/work/columns/` whose `row_id`
  corresponds to this issue (the ticket's `issue.year / month / day`
  field matches).
- Send batches of **4–6** columns in parallel to `column-transcriber`
  agents. Wait for each batch before dispatching the next.
- For each agent that returns `ingested=ok`, log to
  `transcribe/work/experiments.jsonl`.
- Handle `ingested=FAILED` and content-filter blocks per the retry
  tiers documented in `/transcribe-issue`.

### 4. Delete local downloads

After all agents have returned (whether ok or failed), remove the
downloaded source PNGs:

```
python3 -c "
from transcribe.download import remove_issue_downloads
from transcribe.db import REPO_ROOT
n = remove_issue_downloads(REPO_ROOT, 'YYYY-MM-DD')
print(f'Removed {n} downloaded PNGs for YYYY-MM-DD')
"
```

Slices under `transcribe/work/slices/` are left in place (small,
useful for debugging); only the full-resolution source PNGs are removed.

### 5. Summarise

Report:
- Issue date and how many columns were claimed / transcribed / failed
- How many repair tickets were raised
- How many issues remain (run `python3 -m transcribe.pick_issue --stats`)

## Pace notes

- **One issue per invocation.** Don't pick multiple issues in one run.
- **4–6 columns per parallel batch.** The slowest column dominates
  wall-clock, so larger fan-out is mostly free within a batch — but
  keep batches bounded so a transient failure doesn't lose a whole
  issue.
- **Drive download rate**: 0.5 s between files in `claim_columns` — no
  additional delay needed at the skill level.
- **Between issues**: if running via `/loop`, the natural gap between
  sessions is the pace. Don't add an artificial sleep here.

## When all issues are done

`pick_issue` returns exit code 1 and prints an error to stderr. Report
this to the user and stop the loop.
