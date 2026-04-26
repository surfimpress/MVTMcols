# MVTM project notes

## `instructions/` is the durable knowledge base — keep it current

Two files in `instructions/` document things that don't live in the code,
and that future agents (including future-me) will rely on as context:

- **`detection_methods_review.md`** — the catalogue of detection
  strategies in the pipeline. Each strategy has a file/function pointer,
  what it detects, the signal it uses, an effectiveness assessment, and
  a production-suitability verdict. **Update when:**
  - a new detection module is added (e.g. a new `detect_X.py`) — append
    a new numbered section using the same structure (What / Signal /
    Effectiveness / What it lacks / Production suitability)
  - an existing strategy is replaced, demoted, retired, or materially
    changed in approach (not just parameter tweaks)
  - a "TODO" or "should be replaced" verdict gets resolved — change the
    verdict to "Done" / "Canonical" and explain what landed; don't just
    delete the old framing
  - the summary table at the bottom needs a row added or moved
  - **append a dated entry** to the "Update history" section at the
    bottom of the file describing the change

- **`layout_observations.md`** — corpus-level field notes: column
  counts by era, per-issue observations, recurring layout patterns,
  scan conditions. **Update when:**
  - a new issue is processed and surfaces something noteworthy (a new
    pattern, a previously unseen failure mode, an exceptionally clean
    detection worth flagging as a reference point)
  - an era's typical column count is corrected by new aggregate data
    (run `python3 layout_intelligence.py data/mvtm.db` for the live
    aggregate before changing the table)
  - a new layout template is observed (e.g. another era-specific
    page-N convention)
  - a recurring layout pattern lands in code — note the implementation
    location alongside the pattern description so the two stay linked
  - **append a dated entry** to the "Update history" at the bottom

`instructions/archive/` holds historical docs (the original
`newspaper_column_analysis_pipeline.md` and the now-delivered
`plan_archive_three_rectangles.md`). Read for context if relevant; do
not update.

### Why this matters

The pipeline grows by accretion. If these notes go stale, the next agent
(me, in a future conversation) builds the wrong mental model and makes
locally-defensible changes that erode working behaviour — exactly the
failure mode flagged in the global feedback rules. Treat updating
`instructions/` as part of the work, not a chore at the end.

When you commit a change that adds, removes, or materially modifies a
detector or a layout convention, the same commit (or the next one)
should update the relevant `instructions/` file. Don't batch updates
across multiple feature commits — the link between code change and doc
change should be visible in `git log`.
