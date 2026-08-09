# MVTM project notes

## Current status — read this first, keep it current

**Last updated: 2026-08-09.** This section is a live pointer, not a
durable rule — overwrite it (don't append to it) whenever the active
work changes materially: a campaign finishes, a new one starts, or a
session pauses mid-task for a reason a fresh session needs to know
(subagent cap, content-filter block, waiting on the user). The goal is
that a session starting cold can read this and `transcribe/work/experiments.jsonl`'s
tail and reconstruct state without replaying a whole prior transcript.

**Active work: OCR+LLM route for 1980s+ issues — pipeline built,
repeatable, and Workflow-orchestrated.** Pre-1980 column-transcription
production continues unchanged on its own path — see
`transcribe/PLAYBOOK.md` for that procedure and its own live status
section (1% of the corpus done: 57/5,666 issues, 452/44,826 pages;
two review-agent bugs confirmed but not yet fixed — `quality_flags.
adjacent_text_visible` over-triggers at 83.5%, and `subdivide_slice.
py`'s `assemble` step drops `confidence` on Tier-4 reconstructed
slices. Both still open, see the playbook for detail).

This session's thread, prompted by the 1980s+ issues' resistance to
column detection (a dead end, not just a discount — see
`layout_observations.md`): built a whole-page **Tesseract OCR + LLM
correction** route that bypasses column cutting entirely, took it from
one-off exploration to a real repeatable pipeline, and ran it
end-to-end on a full issue via `Workflow`.

- **Full pipeline module: `transcribe/ocr_llm.py`.** Deterministic
  parts only (render at 300dpi via `pdf_utils`, Tesseract with Sauvola
  thresholding + `tessdata_best`, hOCR parsing at `ocr_carea` block
  granularity — confirmed empirically to match Tesseract's own block
  count, not assumed), DB writes, ticket/prompt construction. Never
  calls an LLM itself — mirrors the column-cut pipeline's
  claim/dispatch/ingest split. CLI: `python3 -m transcribe.ocr_llm
  render <date> --page N`, then `ingest-cleanup` / `ingest-items`
  after the two LLM passes run. `ensure_tessdata_best()` and the
  entity-candidate prefetch (see below) are the two "reliably
  slow-moving, pre-compute once" pieces — neither re-fetches per page.
- **Two dedicated agent types** replace generic Agent-tool dispatch:
  `.claude/agents/ocr-cleanup.md` (text-only correction, `tools:
  Read`) and `.claude/agents/ocr-items.md` (item segmentation + entity
  tagging, `tools: Read`). Durable task rules live in these files, not
  resent every call — per-call prompts in `ocr_llm.py` now carry only
  the variable bits (date/page/file paths), mirroring
  `column-transcriber`'s already-validated split. New agent `.md`
  files are **not** picked up mid-session — confirmed twice this
  session (registry only refreshed after a session boundary). Use the
  documented fallback (general-purpose + sonnet, told to Read the
  agent file itself) if you need one before a fresh session starts.
- **Geometry-first item-markup fix, measured not assumed.** The first
  real item-markup run did exhaustive pixel-level border verification
  on every item: 100,949 tokens, **40 tool calls**, 590s. The prompt
  now says derive item boundaries from block adjacency/column
  x-position first, use the image only for genuinely ambiguous
  regions, and explicitly says not to pixel-verify every box. Re-run
  on the same page: **3-4 tool calls**, 395-414s. Token count didn't
  drop (~105-110K) because that same call now also does entity
  extraction (real new output, not overhead) — the fix's win is
  tool-calls/time, not tokens, and that's the honest way to state it.
- **Entity registry: first_seen_date/last_seen_date landed** (schema
  v6, additive — see version history in `schema.sql`). `upsert_entity`
  in `ingest_item_result.py` now does MIN/MAX on every mention, not
  just first-write; backfilled from existing `item_*_mentions` history
  (450/522 people etc. got real dates — the rest have zero linked
  mentions, a pre-existing gap, left NULL rather than fabricated).
  `transcribe/entity_candidates.py` does the token-efficient lookup:
  one bulk query per page (not per-mention), people filtered to a
  ±40yr window around the issue's date via the decade-bucketed
  `idx_people_decade` expression index, organizations/places/products/
  events unfiltered (small tables, persist across decades). Verified
  on real data: "Almonte" correctly matched its existing 1912-12-27
  entity and extended `last_seen_date` to 2001-01-03 — an 89-year
  span, MIN/MAX working as designed.
- **2001-01-03 fully processed, all 12 pages** (not 10 — `render-issue`
  found 2 more pages beyond an earlier manual assumption; don't guess
  an issue's page count, enumerate it from `mvtm.files`). 191 items,
  1,312 OCR blocks, 0 uncovered blocks after a fix, 1,490 entity
  mentions (708 people, 264 orgs, 404 places, 64 products, 50 events).
  Ran through the actual `Workflow`-orchestrated pipeline across two
  batches (pages 5-10, then 11-12) — script at
  `transcribe/workflows/ocr_llm_issue.js`. One real gap caught by a
  block-coverage check (not assumed clean): page 9 had 4 blocks (idx
  32-35, a health-insurance-adjacent ad fragment) the item-markup pass
  never assigned to any item — added as an honest raw catch-all item
  (matches the p4 "Stray mark" precedent — no fabricated label) rather
  than left orphaned or silently dropped.
- **Full issue-level integration landed** (the two items this session
  left open have both been built):
  - `transcribe/routing.py` — `route_for_date(year)` /
    `layout_class_for_date(year)`, hard cutoff at 1980 (matches this
    project's actual reality: 1980s+ cutting/QA was never signed off,
    so "column-cut done and QA'd" and "pre-1980" are the same
    question as of this writing — see the module docstring for when
    to override per-issue instead of moving the cutoff).
  - `transcribe/ocr_llm.py` gained three CLI commands:
    `render-issue DATE` (enumerates every page from `mvtm.files`,
    renders+OCRs+tickets all of them, idempotent, writes a
    Workflow-ready args file), `ingest-workflow-result PATH`
    (ingests a whole Workflow run's result array, idempotent against
    partial re-runs), `verify-coverage DATE` (the block-coverage check
    that caught the page 9 gap, now a real command instead of an ad
    hoc script).
  - `.claude/skills/ocr-transcribe-issue/SKILL.md` documents the full
    procedure end to end. Explicitly does NOT yet have
    `transcribe-issue`'s content-filter escalation ladder,
    Haiku/Sonnet comparison mode, or download cleanup — noted as
    "not yet built" rather than silently absent.
- **New monitor: `transcribe/ocr_llm_monitor.html`.** Own compiled-
  stats store (`transcribe/ocr_llm_stats.json`, gitignored, generated)
  built by `transcribe/build_ocr_llm_stats.py`. Zero DB access from
  the monitor page itself — it only fetches the JSON, polling every
  20s. The JSON is kept fresh by a new LaunchAgent,
  `com.mvtm.ocr_llm_stats` (`tools/refresh_ocr_llm_stats.py` +
  `tools/com.mvtm.ocr_llm_stats.plist`, installed and confirmed
  running this session), on its own 60s loop — independent of
  whatever Workflow is or isn't running, deliberately avoiding
  `build_repair_stats.py`'s own documented past mistake (being
  invoked on every page-completion event by a live loop). Live at
  `https://mcmniintstdio.surfaceimpression.com/MVTM/transcribe/
  ocr_llm_monitor.html` (behind the existing Cloudflare Access gate,
  verified serving 200 after redirect, not just assumed from the
  URL's past use).
- **`transcribe/backfill_2001_ocr_llm.py`** (pages 1-3, an earlier
  session-local one-off) is now fully superseded by the real pipeline
  above — don't extend it.
- **Not yet done / next session:**
  - `items_ocr_ext.item_hocr`/`full_text_markdown` columns exist but
    nothing populates them yet.
  - The `args` parameter to `Workflow` arrived as a non-array once
    this session (crashed `pipeline()` before any agent ran) — worked
    around defensively in the script (`Array.isArray(args) ? args :
    JSON.parse(args)`), root cause not diagnosed. Watch for a repeat.
  - `--user-words` custom dictionaries confirmed to have zero effect
    on the modern LSTM engine — don't revisit without a materially
    different angle.
  - No content-filter-block retry ladder for `ocr-cleanup`/`ocr-items`
    calls yet (see the skill's "Not yet built" section) — build it if
    it actually recurs, not preemptively.

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
