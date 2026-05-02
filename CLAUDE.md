# MVTM project notes

## `instructions/` is the durable knowledge base — keep it current

Three files in `instructions/` document things that don't live in the code,
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

- **`rasterisation_pipeline.md`** — the map of who renders what, at
  which DPI, in which mode, and which on-disk artefacts feed which
  consumers. Read this before touching `pdf_utils.py`, the embedded-
  bitmap fast path, or any writer that produces `page_raw.png` /
  `*_col*.png` / `ads/p<N>/*.png`. Cross-links `dpi_constants.py` for
  per-stage DPI rationale. **Update when:**
  - a new on-disk artefact is added or removed
  - a writer changes mode/DPI (e.g. RGB → mode='1', or 150 → 300)
  - the embedded-bitmap gate criteria change, or a new fast-path is
    added
  - the cache contract in `pdf_utils` changes (new fields, new
    derivation paths, new eviction policy)
  - a detector starts re-reading from disk — that's a contract
    change worth surfacing immediately
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

## `coordinates.py` is the point of truth for pct ↔ px conversions

All pct ↔ px (and pct ↔ PDF-points) conversions in this codebase go
through `coordinates.py`. Do not write inline `int(x_pct / 100 * w)` or
`round(px / w * 100, N)` in new code; import the helpers instead.

**Why this matters:** wrong-origin errors (measuring against image
height instead of page height, or vice versa, or the wrong rect) have
been a recurring class of bug. Centralising the conversion forces a
deliberate choice of which dimension to pass — the helper signature is
the place to think about origin discipline. Re-deriving the formula
inline is where the mistakes happen.

**The helpers (see module docstring for full discussion):**
- `pct_to_px(pct, dim)` — page percentage → integer pixel position
  (uses `round`, not `int`/truncate)
- `pct_to_px_float(pct, dim)` — same, but returns float for chained
  arithmetic where intermediate rounding would lose precision (areas,
  bridge calcs, PDF-point conversions)
- `px_to_pct(px, dim)` — pixel position → page percentage, rounded to
  2 decimals (the canonical precision across the pipeline)
- `pct_to_frac` / `frac_to_pct` — when working with `fitz.Rect` clip
  fractions
- `clamp_pct(pct, lo=0, hi=100)` / `clamp_px(px, dim)` — boundary
  clamping

**Don't add new helpers without reason.** The current set covers every
conversion in the codebase. If a callsite needs something new, first
check whether `pct_to_px_float` plus a `round()` at the call site
already covers it.

**Don't reintroduce inline conversions even for one-off use.** Three
lines of `int(x / 100 * w)` invite a fourth, and the fourth is always
where someone passes the wrong `w`.
