# MVTM project notes

## Current status — read this first, keep it current

**Last updated: 2026-08-09.** This section is a live pointer, not a
durable rule — overwrite it (don't append to it) whenever the active
work changes materially: a campaign finishes, a new one starts, or a
session pauses mid-task for a reason a fresh session needs to know
(subagent cap, content-filter block, waiting on the user). The goal is
that a session starting cold can read this and `transcribe/work/experiments.jsonl`'s
tail and reconstruct state without replaying a whole prior transcript.

**OCR+LLM route (1980s+ issues) is built and stable** —
`transcribe/ocr_llm.py` (render/OCR/cleanup/items pipeline),
`.claude/agents/ocr-cleanup.md` + `ocr-items.md` (Read-only, durable
rules cached), `transcribe/routing.py` (1980 cutoff),
`.claude/skills/ocr-transcribe-issue/SKILL.md`,
`transcribe/workflows/ocr_llm_issue.js`. Validated end-to-end on
2001-01-03 (12pp), 1994-01-05 (12pp), 1986-01-08 (18pp). Monitor at
`transcribe/ocr_llm_monitor.html` (own JSON store, LaunchAgent-
refreshed, zero DB load from viewing). Not yet done:
`items_ocr_ext.item_hocr`/`full_text_markdown` unpopulated; no
content-filter retry ladder. Full detail in git log around
2026-08-09, not repeated here.

**Entity registry is now real** — `people`/`organizations`/`places`/
`products`/`events` carry `first_seen_date`/`last_seen_date` (MIN/MAX
per mention, schema v6+), `transcribe/entity_candidates.py` prefetches
candidates for the LLM to match against (decade-windowed for people),
`transcribe/merge_entity.py` is the one-line CLI for merging
duplicates (`python3 -m transcribe.merge_entity <type> <keep> <drop>
[--alias]`). `transcribe/entities.html` is the browsable table
(search/filter/sort/paginate, URL-synced). Established curation
patterns this session, now also baked into `ocr-items.md` /
`items-classifier.md` for future extraction:
  - Period abbreviations (Wm./Geo./Chas./Thos./Jas./Robt./Ed.)
    expand to full names; original stays in `mention_text`, not a
    separate field — that field already existed for exactly this.
  - Products: `name` is the generic category ("Baking Powder"), brand
    goes in `manufacturer`, `mention_text` keeps the branded form as
    printed.
  - Recurring event types where each instance differs only by
    who/where (marriages) genericize to one `name` ("Marriage");
    **deaths were explicitly left alone** — don't extend the pattern
    there without asking, a death may carry more individually-
    important distinguishing value than a marriage announcement.
  - When splitting/merging changes the date range, merge outright on
    identical ranges; for people, a large date gap argues against
    same-identity (ask); for products/events as generic categories, a
    date gap doesn't argue against merging (a category legitimately
    spans decades) — different reasoning for different entity kinds,
    don't apply one rule uniformly.

**Pre-1980 items/entities: only 1912-12-27 is actually done**
(1,133→ wait, 160 items, 718 distinct terms). 59 other pre-1980 issues
have `column_transcripts` done (pass-1A) but **zero** `ad_transcripts`
(pass-1B never run) and **zero** `items`. Audited 2026-08-09, don't
re-derive without reading this first:
  - **Ad PNGs are archived off local disk for every pre-1980 issue**,
    including 1912-12-27's own already-transcribed ones (cold-storage
    archival, not specific to any one date). Pass-1B can't run
    without restoring them from wherever they're archived to.
  - **Corrected finding, verified against `claim_items.py`'s actual
    code (not the skill doc's stated prose):** pass-2 (items-
    classifier) does NOT hard-require pass-1B. The only real gate is
    columns being done; ad transcripts are pulled in if present but
    their absence doesn't block a ticket. `classify-items-page/
    SKILL.md`'s "pass-1B must land first" is a quality expectation,
    not a technical one — items-classifier can extract people/places/
    orgs/events from column body text fine without ads; only ad-
    specific content itself would be missing on pages that have real,
    untranscribed ads. This was asserted as a hard blocker earlier in
    error — verify against the code, not the skill doc's prose, if
    this comes up again.
  - **Don't trust `mvtm.detected_ads` at face value for 1930-04-11,
    1962-02-01, 1973-01-11** (the three merged-issue reallocation
    dates — see `layout_observations.md`). Their ad detection is
    still filed under the *original wrong* date bucket (1930-04-04,
    1962-01-25, 1973-01-04 respectively); these three show "zero
    detected ads" but are not actually ad-free. Confirmed by checking
    the old-date buckets have real ads (4-15 per page) while the
    reallocated dates have none at all in `detected_ads`.
  - **Paused here deliberately.** Running pass-2 across the 59
    unblocked issues is technically possible right now (per the
    corrected finding above) but the user wants to resolve some
    structural things first — specifics not yet stated as of this
    writing. Don't start a broad pass-2 run without checking in first.

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
