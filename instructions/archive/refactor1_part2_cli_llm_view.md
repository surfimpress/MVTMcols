# Refactor 1 Part 2 — LLM-side view of the CLI surface

**Status:** design only. Companion to `refactor1_part2_cli_design.md`,
written from the consumer (LLM agent) seat after the 2026-04-27
ad-heavy detection work surfaced concrete cases where the agent would
need primitives the producer-side design hadn't fully spelled out.

**Read the producer-side doc first.** It defines the shared-functions
principle, the umbrella-CLI shape, hand-edit markers, rollback, and
output discipline. This doc only covers what *I* (the LLM) would
need to actually do the job and where the existing design has gaps.

## The job, concretely

After `process_issue` finishes, I open an issue and look at what the
pipeline produced. I find three classes of mistake to fix:

1. **Column boundaries in the wrong place / count.** Either an extra
   phantom column on the edge, or a missing real column the detector
   didn't see (e.g. 1947-02-06 P8 with no body-text signal at the
   leftmost position).
2. **Ad bboxes wrong.** A 4-col ad caught as a 3-col strip (under-
   capture) or an ad bbox swallowing real body text (over-capture,
   e.g. 1947-02-13 P6).
3. **Downstream artefacts wrong** because (1) or (2) were wrong:
   headlines that bled across an actual boundary, body-text strips
   placed inside an ad zone, large-type missed because the column
   was clipped.

For each mistake I want to:

a. **See it.** Look at the page with overlays so I can confirm the
   pipeline got it wrong.
b. **Fix it surgically.** Change one thing — a boundary position,
   an ad bbox, a column count.
c. **See the fix landed.** Re-render the affected artefacts and
   look at them again before moving on.
d. **Move on confidently.** The fix sticks across future
   `process_issue` reruns; if it doesn't, I want a clear undo button.

## A walkthrough — fixing 1947-02-06 P8

This is the case from today. P8 has 6c [21.0 .. 89.2] but the page
clearly has a 7th column on the left starting around x=10. The
placement engine couldn't recover it because no body-text signal
existed in that strip. As an LLM I'd:

```
# 1. Survey
mvtm show 1947-02-06 8
  → JSON: layout {cols: 6, boundaries: [21.0, ...]}, ads [...],
    headlines [...], body_text [...], hand_edited: false

# 2. Look at the page with overlays
mvtm view 1947-02-06 8 --overlay=boundaries,ads --dpi 150
  → returns a page PNG path; agent reads it visually

# 3. Look at the strip where I think a column is missing
mvtm crop 1947-02-06 8 --x-pct 5 --w-pct 15 --y-pct 0 --h-pct 100
  → returns a tall strip PNG so I can verify a real column lives there

# 4. Diagnose: why didn't the pipeline place a boundary at x≈10?
mvtm explain-layout 1947-02-06 8
  → JSON: detected_boundaries [...], pitch 11.0, R3_left 17.34,
    placement_offset_score: -4.63, boundaries_dropped_at_clip:
    [{x_pct: 10.04, reason: "outside_left_limit_15.14"}]

# 5. Inject the boundary the pipeline couldn't find a signal for
mvtm add-boundary 1947-02-06 8 --x-pct 10.04
  → response: { ok: true, transaction_id: 42,
    new_layout: {cols: 7, boundaries: [10.04, 21.0, ...]},
    regenerated: ["columns/1947-02-06/p8/*_col1.png", ...] }

# 6. Verify
mvtm view 1947-02-06 8 --overlay=boundaries
  → re-read PNG; confirm boundary lands inside a gutter

mvtm crop 1947-02-06 8 --col-idx 0
  → look at just the new leftmost column

# 7. Re-run downstream layers so headlines/body get re-detected
mvtm recompute-layers 1947-02-06 8

# 8. Done; row is hand-edited, won't be clobbered by future reruns

# If I'm wrong:
mvtm undo 42
```

## What the producer-side design has — and what it doesn't

The producer-side doc covers:

- `mvtm show` (read-only inspector) ✓
- `mvtm recut-page --columns N` (re-run detection with overrides) ✓
- `mvtm adjust-ad / split-ad / merge-ads` ✓
- `mvtm extract-column` ✓
- `mvtm recompute-layers` ✓
- `mvtm rerun-issue --from-stage` ✓
- `mvtm undo <txn>` ✓
- `hand_edited` flag, `cli_history` table ✓
- JSON-by-default, error categories ✓

What I'd add or sharpen, from the consumer seat:

### 1. Direct boundary editing — not just `recut-page`

`recut-page --columns 7` would be wrong for 1947-02-06 P8: the
placement engine has no target signal at x≈10, so re-running with
`--columns 7` either re-clusters poorly or refuses. I need to inject
a boundary *directly*, bypassing detection.

```
mvtm add-boundary <date> <page> --x-pct PCT
mvtm move-boundary <date> <page> --col-idx I --to-x-pct PCT
mvtm delete-boundary <date> <page> --col-idx I
mvtm set-boundaries <date> <page> --x-pcts P1,P2,...,Pn  # whole-page replace
```

Each of these:
- Validates monotonicity (no out-of-order) and minimum gap (no two
  boundaries within < 4% of page width).
- Re-runs `extract_columns(...)` automatically — column PNGs are a
  derived artefact, the LLM shouldn't have to remember to refresh
  them.
- Sets `page_layouts.hand_edited = 1`.
- Returns the new layout in the response.

`recut-page` stays for the case where I want re-detection at a
different column count, just expressed via a different entrypoint.

### 2. Visual primitives — `view` and `crop`

`mvtm show` is JSON. To validate visually I need image primitives.

```
mvtm view <date> <page> [--overlay=L1,L2,...] [--dpi N]
  → renders the full page at requested DPI with toggleable overlays:
    boundaries, ads, headlines, body_text_regions, h_rules,
    large_type. Returns a path to a temp PNG. Same renderer the
    page_viewer.html uses (don't duplicate the overlay logic).

mvtm crop <date> <page> [--x-pct X --w-pct W --y-pct Y --h-pct H]
                       [--col-idx I]
                       [--ad-id A]
                       [--dpi N]
  → renders a sub-region. One of (x/w/y/h) | col-idx | ad-id is
    required. Useful for "look at just this strip before deciding
    whether to inject a column there."
```

These are read-only and don't touch the DB; they're file-output
inspectors. Cheap to ship early, and `view` is the thing I'd reach
for *first* every time I open a new page.

### 3. `explain-layout` — surfacing why detection landed where it did

I need to know what the pipeline *saw* and *decided*, not just the
result. Without this I can't tell whether to inject a missing column
or accept the pipeline's choice.

```
mvtm explain-layout <date> <page>
  → JSON:
    {
      detected_boundaries: [{x_pct, score}],
      pitch: 11.0,
      pitch_source: "issue_aggregate" | "page_pitch_adopted",
      r3_left: 17.34,
      r3_right: 99.92,
      content_band_left: 15.14,    # after slack applied
      content_band_right: 102.12,
      grid_offset_chosen: -4.63,
      grid_offset_score: 18.7,
      boundaries_dropped_at_clip: [{x_pct, reason}],
      validator_actions: [
        {action: "drop_right", reason: "...", confidence: 0.88}
      ]
    }
```

Implementation: re-run `place_standard()` with an instrumented
context that records its scoring trace. Don't add logging to the hot
path — make this a separate code path that pays for the trace.

### 4. `explain-ad` — why this ad bbox, why this size

Equivalent for ad detection. When an ad bbox is wrong, I need to see
what signal led the detector here so I can decide if it's a fixable
parameter case or a "the detector got the wrong thing entirely" case.

```
mvtm explain-ad <ad_id>
  → JSON: {
      detection_strategy: "border_box" | "consensus_block" | ...,
      raw_evidence: { ... whatever the detector kept ... },
      neighbouring_ads: [...],
      column_overlap: [{col_idx, fraction_covered}]
    }
```

### 5. Output: include the *previous* state on every mutating call

The producer-side doc says JSON-out. From the consumer seat I want
*before-and-after* on every mutating command, not just the after.
Mental model:

```json
{
  "ok": true,
  "transaction_id": 42,
  "command": "add-boundary",
  "before": { "boundaries": [21.0, 32.3, ...], "cols": 6 },
  "after":  { "boundaries": [10.04, 21.0, ...], "cols": 7 },
  "regenerated_artefacts": [...],
  "hand_edits_set": ["page_layouts:1947-02-06:8"],
  "warnings": []   // e.g. "new boundary lands inside ad bbox X"
}
```

The before/after diff is what I cite back when the user asks me what
I changed; without it I'd have to call `show` twice.

### 6. Warnings, not just errors

Some mutations are technically valid but suspicious — e.g. injecting
a boundary that lands inside an ad bbox, or moving a boundary across
a detected headline. I want these as *warnings* in the response, not
errors. The mutation goes through, but I see the flag and decide
whether to investigate or back out.

Categories:
- `boundary_inside_ad` — boundary lands within an ad bbox
- `boundary_crosses_headline` — moves across a headline
- `extreme_pitch_change` — new layout has CV > 0.3
- `column_count_unusual` — count is < 5 or > 8 for the era

### 7. Batch / triage commands

When I sit down to fix an issue, I want a worklist, not to
discover problems one page at a time.

```
mvtm flag-issue <date>
  → JSON: list of pages with any of:
    - quality_flags non-empty
    - validator drops
    - placement scoring below threshold
    - ad / boundary collision
  ranked by likely-needs-attention.

mvtm flag-batch <year> [--month M]
  → same, across an issue range. The output is the LLM's queue.
```

This isn't a pipeline change — it's a query over existing fields.
But it's the entry point I'd use most.

### 8. A "scratch" mode for trying changes without committing

```
mvtm <command> ... --scratch
```

Runs the command, regenerates artefacts to a separate `scratch/`
directory, but does NOT touch the DB or the `columns/` tree. The
response is still JSON with before/after. Lets me preview a move
before committing.

Cheap if every mutating command takes an `output_root` parameter
internally; the scratch path just rewrites that root.

## Schema additions implied by the consumer view

Beyond the producer-side doc:

- `cli_history` should record the **command line** verbatim, not
  just the table/before/after. When I'm debugging "what happened to
  this page yesterday" I want to see the actual `mvtm` invocation.
- `page_layouts.placement_trace` — optional JSON column for the
  scoring trace `explain-layout` returns. Filled by the pipeline
  when run with `--trace`. Empty by default; the data is large and
  I only need it when something's wrong.

## What I deliberately don't need (yet)

- **Per-region edits inside a column.** The producer doc says this
  is segmentation, not layout. Agreed.
- **Direct headline / body-text bbox edits.** Cheaper to fix the
  upstream cause (column or ad) and re-run `recompute-layers`. If a
  case turns up where that's not enough, add it later.
- **Multi-page transactions.** A correction is one page, one row.
  The two cases I might want this for (cross-page article
  continuation, two-page spread) both belong in Stage 2 anyway.
- **A live REPL.** The CLI is good enough — every operation is a
  one-shot command with JSON in/out.

## Order I'd want these built (consumer-priority, not producer-cost)

The producer-side doc gives an implementation order based on blast
radius (start with read-only, progress to mutating). From the
consumer seat the priority is *which commands unlock me to do real
work*:

1. `mvtm show` — can't start without it.
2. `mvtm view` and `mvtm crop` — without these I can't validate
   anything visually, and I'd be flying blind. Higher priority than
   the producer doc gives them (they're not even in the list).
3. `mvtm explain-layout` — once I have eyes, I need to know what
   the pipeline saw. Priority equal with `view`.
4. `mvtm flag-issue` — gives me a worklist instead of having to
   sweep pages randomly.
5. `mvtm add-boundary / move-boundary / delete-boundary` — the
   surgical primitives for column fixes.
6. `mvtm adjust-ad / split-ad / merge-ads` — same for ads.
7. `mvtm recompute-layers` — to refresh downstream after (5) or (6).
8. `mvtm undo` — must land before any mutating command goes live.
9. `mvtm recut-page` — useful but rarely the right tool; I'll mostly
   reach for direct boundary editing instead.
10. `mvtm rerun-issue --from-stage` — last; this is the
    "Anya-finished-her-pass, redo everything downstream" command.

`undo` listed at 8 is conditional: it's a hard requirement before
*any* mutation, so really 8 is a prerequisite of 5/6 — either ship
it first or ship it alongside the first mutating command.

## Open questions specific to the consumer side

1. **Image format / size.** `view` and `crop` return PNGs. At what
   DPI? 150 DPI is fast and big enough to see column structure;
   450 DPI matches the pipeline but is huge. Default 150, with a
   flag to bump.
2. **Where do `view` and `crop` outputs live?** Temp dir per
   command? A persistent `inspect/` tree the LLM can revisit? I'd
   default to a content-addressed temp path so identical inputs
   share files; let the LLM cache the path itself.
3. **How does the LLM signal "I've finished with this page"?**
   A `mvtm finalize <date> <page>` no-op that writes a marker, so
   later reruns can show "human-reviewed pages: N of 8"? Or just
   `hand_edited` is enough? Probably the latter — adding more state
   is overkill.

When this work resumes, the producer-side doc's "first three forks
in the road" still apply (argparse-vs-Click, JSON envelope shape,
keep-or-absorb existing per-stage CLIs). The consumer-side adds:

4. **Image-primitive priority.** Confirm `view` and `crop` ship in
   the first wave alongside `show`, not after the mutating commands
   land.
5. **Trace storage.** Decide whether `placement_trace` lives on
   `page_layouts` as a column or in a sidecar table. Sidecar is
   cleaner; column is one fewer JOIN.
