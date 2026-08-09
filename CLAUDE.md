# MVTM project notes

## Current status — read this first, keep it current

**Last updated: 2026-08-08.** This section is a live pointer, not a
durable rule — overwrite it (don't append to it) whenever the active
work changes materially: a campaign finishes, a new one starts, or a
session pauses mid-task for a reason a fresh session needs to know
(subagent cap, content-filter block, waiting on the user). The goal is
that a session starting cold can read this and `transcribe/work/experiments.jsonl`'s
tail and reconstruct state without replaying a whole prior transcript.

**Active work (as of this session): OCR+LLM route for 1980s+ issues.**
Pre-1980 column-transcription production continues on the existing
path — see `transcribe/PLAYBOOK.md` for that procedure and its own
live status section (1% of the corpus done: 57/5,666 issues, 452/
44,826 pages; two review-agent bugs confirmed but not yet fixed —
`quality_flags.adjacent_text_visible` over-triggers at 83.5%, and
`subdivide_slice.py`'s `assemble` step drops `confidence` on Tier-4
reconstructed slices. Both still open, see the playbook for detail).

This session's new thread, prompted by the 1980s+ issues' resistance
to column detection (a dead end, not just a discount — see
`layout_observations.md`): built and validated a whole-page
**Tesseract OCR + LLM correction** alternative that bypasses column
cutting entirely.

- **Validated on 2001-01-03, pages 1-3** (a real modular/non-grid
  layout). Pipeline: Tesseract 5.5.3, `tessdata_best`, Sauvola local-
  adaptive thresholding (fixes grey-sidebar/fold-shadow blackout —
  recovers ~70% more words vs default Otsu) → confidence-triage
  text-only LLM cleanup (only blocks below conf 85 sent) → LLM item
  segmentation from the page image + block list. `effort: "low"` +
  triage together bring page cost to ~70-75K tokens vs ~123K
  unoptimized. Full writeup + tap-to-inspect artifact:
  `transcribe/comparison_tesseract_2001-01-03.html`.
- **Schema extended for this route** (schema.sql version 4, additive
  only — no changes to existing tables/columns): new peer tables
  `pages` (page-level OCR/render facts — didn't exist before, page
  was just an int column everywhere), `page_ocr_blocks` (peer of
  `column_transcripts` — one row per Tesseract block), `items_ocr_ext`
  (1:1 extension of `items`, OCR-route-only fields: `item_hocr`,
  `full_text_markdown`, `media_paths_json` — its existence for an
  item_id *is* the provenance marker), `item_ocr_block_spans` (peer
  of `item_column_spans`). DB backed up first to
  `transcribe/data/transcribe.db.pre-ocr-llm-schema_20260808.bak`.
- **This issue's test data is now live in the DB**, not just the
  artifact: 3 pages, 332 OCR blocks, 38 items, all cross-checked
  (block-reference coverage, FK integrity, bbox ranges). One real bug
  caught during backfill and fixed in the DB the same way it was
  fixed in the artifact: a stray `{` character block had Tesseract
  confidence 88 (above the 85 triage threshold) despite being noise,
  so it slipped the LLM cleanup pass untouched — the *threshold*
  isn't foolproof against confident garbage, worth remembering if the
  triage cutoff gets tuned later. See `transcribe/
  backfill_2001_ocr_llm.py` — kept as a historical record of the
  exact mapping (block-id indexing, display-px vs full-page-px
  coordinate spaces), **not a repeatable tool** (its source paths
  were this session's /tmp files).
- **Not yet done / next session:** generalize the backfill into an
  actual repeatable ingestion path (today it's bespoke to one issue's
  session-local files); decide `layout_class` routing logic for which
  issues use this route vs the column-cut pipeline; the item-markup
  prompt should get a one-line addition telling the LLM not to merge
  scattered/noise blocks into a single page-spanning bbox (root cause
  of a tap-target bug already fixed in the artifact — see the
  `buildItemLayer` z-order fix in the HTML for the symptom, the
  prompt-side fix itself is not yet written into `hocr_econ_test.js`
  or wherever the production item-markup prompt ends up living).
  `--user-words` custom dictionaries confirmed to have zero effect on
  the modern LSTM engine — don't revisit without a materially
  different angle.

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
