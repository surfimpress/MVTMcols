# MVTM project notes

## Current status — read this first, keep it current

**Last updated: 2026-08-15.** This section is a live pointer, not a
durable rule — overwrite it (don't append to it) whenever the active
work changes materially: a campaign finishes, a new one starts, or a
session pauses mid-task for a reason a fresh session needs to know
(subagent cap, content-filter block, waiting on the user). The goal is
that a session starting cold can read this and `transcribe/work/experiments.jsonl`'s
tail and reconstruct state without replaying a whole prior transcript.

**"Scaled" pipeline experiment, started 2026-08-15 — READ
`instructions/scaled_pipeline.md` BEFORE TOUCHING `transcribe/scaled/`.**
An isolated parallel track testing whether structure can be derived from
classical signal (Tesseract's own hOCR layout output + geometry) instead
of paying an LLM to look at the page. Motivation, measured: the corpus is
70,063 pages and only 0.80% is done; the OCR+LLM route costs 77-104k
tokens/page and 93.4s/page, extrapolating to **5.4-7.3 billion tokens and
~76 days** for the full corpus. `items` segmentation alone is **72%** of
that.
  - **Key discovery:** `ocr_llm.parse_hocr()` discards nearly everything
    Tesseract emits — `ocr_separator` (a vertical one IS a column rule),
    `ocr_photo`, per-line `x_size` (font-size proxy), and Tesseract's own
    `ocr_header`/`ocr_caption`/`ocr_textfloat` classes. Recovered from the
    .hocr files already on disk at zero OCR/LLM cost: **5,081 regions,
    560 header + 447 caption lines** across 90 pages.
  - **Total isolation by direction:** `transcribe/scaled/` imports
    NOTHING from the rest of the repo — `_support.py` holds local copies
    of the coordinate and db helpers. Same DB (additive schema-v15 tables
    only), so results are comparable in one query. Delete the package and
    production is unaffected.
  - **Stage 1b is SLIVERS AT THE RIM** (`sliver_pass.py`), and it runs
    before everything else that reads Tesseract's regions. The binding
    gutter and sheet edge come back as `ocr_separator` AND `ocr_photo`
    (1980-04-06 p4 has a photo at x 0.00-2.34 spanning y 0.75-81.56 — a
    full-height strip down the binding). Three tiers: a sliver **wholly
    inside the outer 4-cell rim** goes outright; one **reaching past the
    rim** goes only if nothing aligns with it; and **the rim is pulled in
    per side wherever content blocks intrude**, because that means the
    margin really is narrow. Two rules make it work — **a sliver may not
    be corroborated by another sliver** (two shadows along one edge agree
    perfectly), and **the edge tested is the one the sliver runs PARALLEL
    to** (a shadow lies along its edge, it does not cross the page; taking
    the nearest edge in any direction killed a 122-cell full-width rule on
    1990-10-10 p5). Blocks are NEVER candidates. Measured: 537 removed
    over 90 pages, and the false-positive proxy — removals landing inside
    the content area — is **5.6% against 31.6%** for the band test it
    replaces. **Known residual:** a BLOCK bbox can have the shadow swept
    into it and is untouchable here (1990-10-10 p15's content left is 0.4
    cells, set by two full-width blocks that corroborate each other).
    View it with `experiments/block_grid.py`.
  - **Stage 1c is the PAGE CONTENT AREA** (`detect_content_area.py`),
    and it runs BEFORE columns. It **owns** `pages.content_left_pct`/
    `content_right_pct`/`content_top_pct`/`content_bottom_pct` — one
    owner, one writer, checked: `detect_hlines.store()` used to overwrite
    the top/bottom and stage 3 runs later, so **84 of 90 pages held a
    stored top stage 1c never produced**. That write is gone; stage 3
    now READS.
    **Rebuilt 2026-08-16, see `scaled_pipeline.md` §5s.** The stored box
    is: stage 1b removes rim slivers → **left/right by AGREEMENT** among
    the survivors, **top/bottom by EXTREME** (left/right sit on the column
    grid and agree, 68-80% of items; vertical position is not quantised
    and does not, 39-47%) → **OUTER PERIMETER** of that and the old
    line-derived box, so an edge need only be found by one of them →
    **every margin floored at 4 cells**. The floor is what makes the union
    safe on y, where the outer box otherwise ran to the foot of the sheet;
    it fires on bottom 42 pages, top 37, left 21, right 17. Margins median
    L6.1 R6.6 T5.6 B5.6 cells, minimum 4.0 everywhere. Items of all types
    outside the box 14.5% → 5.7%.
    **Why it's a separate step:** the fitter used to take its bounds from
    block-edge extremes, so a single sheet-edge artefact anchored the
    lattice to the physical page edge — `text_left` was 0.00% on many
    pages, up to 7.2% off.
    **WHAT ACTUALLY CONSUMES IT: only `separator_grid._within_content`,
    feeding zones.** `detect_grid` does NOT read it, contrary to what this
    file said until 2026-08-16 — verified by grep and by dry run (column
    counts identical on all 90 pages, no edge moving 0.01 cells). The
    older line derivation (`content_box`, clusters on x, extremes on y)
    is still one of the union's two inputs and still an IIIF layer.
  - **Stage 2 is COLUMNS — ONE pass** (`detect_grid.py`). Per
    `instructions/typesetting_practice.md`, fit a few numbers (margin,
    column width, gutter, count) rather than discover boundaries. Two
    global parameters (pitch, offset) are fitted across the page and one
    column width derived; columns come straight off the lattice, so **the
    gutter is constant down the page by construction** — which is what a
    gutter physically is. Corpus median gutter 0.48%.
    Measured on BLOCKS — hOCR lines contribute no edges except one
    constraint (below) and otherwise only set a minimum block height —
    weighted by item HEIGHT. Evidence is weighted by kind: text blocks
    full value, `ocr_separator` vertical rules and `ocr_photo` regions
    HALF. Rule edges are CROSSED OVER (a rule sits in the gutter, so
    rule.L bounds the previous column's right); photo edges map straight
    through (a photo sits ON the columns). The one line-derived
    constraint: the LAST column's right edge may not sit left of the
    rightmost hOCR line in the rightmost block.
    Column-count sense checks: a measure floor (`MIN_PITCH_PCT` 8.0, a
    column must be wide enough to set body text in) and a `low_evidence`
    flag below 60 text lines. **Everything else builds up from this.**
    Separators are now INPUTS, so they can no longer serve as independent
    ground truth — use `items.item_type='display_ad'` from the production
    route instead, which they contribute nothing to.
  - **Pass 2 (per-edge refinement) is ARCHIVED but NOT dead — we may
    return to it.** `transcribe/scaled/archive/refine_columns.py`, kept
    runnable. It leaned each column edge independently to the outermost
    nearby edge; measured, it made the gutter vary within the page on
    **54/89 pages (61%)**, following noise rather than the page, and the
    user's read was that pass 1 wins in almost every case. The problem it
    was built for is still unsolved: the scan's own scale drift across
    the page (~1.3% by the right-hand edge). Any retry must stay
    **parametric** — one global scale/skew term, gutter held constant —
    never per-edge.
  - **Display ads carry their own interior grid** — measured, open. 30%
    of all text blocks sit inside a display ad (100% on a full-page-ad
    page), and their internal sub-columns are what halved 1980-04-06 p2.
    `x_size` does not separate them cleanly (44 vs 36, overlapping).
    Excluding interiors was inconclusive and the test was confounded; see
    `instructions/scaled_pipeline.md` §5f before retrying.
  - **Boxes from CORNERS ALONE** (`experiments/ad_rectangles.py`) is the
    derivation that stuck. Standalone: corners in, rectangles out, no DB
    and no Tesseract. **One predicate — a rectangle is an item when no
    other corner interrupts its sides** — which rejects bridges, gutter
    slivers and unions of any depth by construction, and replaced SIX
    tuned thresholds. Works in CELLS (square by construction; page percent
    is two units and mixing them lost a real box). Order-independent, so
    the ordering bugs that plagued the earlier detectors cannot arise.
    p13: 8 rectangles, the complete set. Two earlier generations are in
    `archive/corner_quadrilaterals.py` and `archive/percent_box_filters.py`.
  - **Stage 2b is `detect_zones`** — boxed zones FROM THE GRID:
    Tesseract separators RAW -> `separator_grid` (square cells, corners)
    -> `ad_rectangles` (one predicate) -> content. Schema v20
    `page_zones`, 276 zones over 90 pages. **`rules.py` is NOT in this
    path** — its conjoined-dropping and fragment-rejoining were built for
    the rule-PAIRING detector, and measured against the corner
    derivation they give 259 zones against 276, worse on 15 pages and
    better on 10, losing p13's Sidewalk Sale. `build()` defaults
    `clean=False`; `--clean` is a diagnostic. Three docs claimed
    otherwise and were wrong.
    Each zone carries its blocks/lines/photos, column span, score and
    advisory flags (`empty`, `pictorial`, `duplicate`, `encloses`) —
    **geometry decides, content is evidence, nothing is dropped on a
    content test** (28.8% of boxes hold no text block and many are
    pictorial ads). Corpus flags: 54 empty, 14 pictorial, 2 encloses,
    zero duplicates. **`encloses` was previously unable to fire** — it
    demanded the inner zones' blocks exactly cover the outer's, which a
    headline outside the inner panel breaks, so it read 0 against 12
    real nestings and that zero was quoted as evidence the derivation
    was clean. It now means plain geometric nesting: 2 outer zones
    (p9 holds 11, p13 holds 1). A flag that cannot fire is worse than
    no flag.
    **Both halves of the predicate ask cluster MEMBERSHIP, never
    distance to a cluster centroid** (fixed 2026-08-16). Clusters are
    built by splitting on gaps, so a cluster can be WIDER than the
    tolerance that built it; comparing a corner to its own line's
    centroid then fails in both directions at once, and a fifth of a
    cell of centroid wobble decided whether a whole stack of real ads
    on 1980-04-06 p10 survived. **Rectangles may nest or be disjoint,
    never cross** — a separate pass, because the corner predicate is
    local and structurally cannot see it. Three earlier generations are
    archived: `detect_boxes_pairing`, `corner_quadrilaterals`,
    `percent_box_filters`; `experiments/confirm_boxes_ccl.py` is an
    independent connected-component cross-check.
  - **Superseded — old stage 2b (rule pairing)** (`detect_boxes.py`, `page_boxes` schema
    v18). Ruled rectangles — ads, notices, tenders, panels. Built from
    FOUR sides, allowing for three real properties of the print:
    **rounded corners** — sides stop 0.5–3.9% short of the join, which is
    exactly why naive corner-matching measured only 22% (4,452 endpoints,
    median distance 9.0%); a side must BRIDGE the box within `INSET_PCT`,
    never meet a corner. **Drop shadows** — opposite sides differ in
    weight (28px top vs 48px bottom on one box), so `side_px` is RECORDED
    and never filtered on; a version requiring matched weights found 2
    boxes on a whole page. **Stacked boxes share verticals** — so a box is
    emitted between each consecutive bridging horizontal PLUS one for the
    whole enclosure, giving containers and cells (Fraser's as one box and
    its price rows; the Sidewalk Sale grid and its cells).
    **The VERTICALS define the sides, not the horizontals** — extending to
    horizontal ends stretched boxes a whole column left into body text,
    because a bridging rule often belongs to a neighbour and overshoots.
    Page-edge verticals excluded as scan artefacts. ~9.6 boxes/page.
    **Containment matching was tried and REVERTED** (20.8/page, overlapping
    rectangles cutting across text on p6) — do not reintroduce it.
    **Tesseract both MERGES and SPLITS rules** — undo both before fitting.
    *Conjoined*: it emits the parts AND a merged region covering them
    (p13's left edge appears as 17px + 29px rules plus a 50px region
    spanning both), which manufactures boxes across boundaries that don't
    exist; detect via ">=2 others inside its RUN", NOT bbox containment,
    since the merge is often wider than its parts. *Fragmented*: the
    opposite — p13's Sidewalk Sale foot arrives as two pieces at y~95.5,
    so the largest box on the page was missed; collinear pieces are
    merged back. **Boxes NEST or are disjoint, never straddle** — inner boxes crossing
    their container and the column gutter was a real defect (p8: 70 -> 48
    boxes once crossings are dropped). A **three-sided box can be closed**
    when its verticals are a matched pair AND a barrier sits below, but it
    is marked `n_sides=3, needs_review=1` for an LLM pass to confirm —
    never presented as measured. **Judge by rendering, not the
    `display_ad` metric** (it counts notices and tenders as false
    positives). The vertical list MUST stay sorted by x — the pair loop
    requires `vr.x - vl.x >= MIN_WIDTH`, and SQLite returns rows
    unordered, so an unsorted list silently skipped every pair whose left
    rule happened to be listed second. That alone was hiding CENTENNIAL
    DOLLARS on p8 despite all four of its sides being present.
    **Known limit:** a few boxes genuinely have no bottom border in
    Tesseract's output (Smithson Motor Sales on p8) — geometry cannot
    recover those, see the pixel-rule note.
  - **Stage 3 is HORIZONTAL ALIGNMENTS** (`detect_hlines.py`,
    `render_hlines.py`, `page_hlines` schema v17). Every alignment carries
    a **column span** — on a post-1980 mosaic an alignment is local
    (columns 3-5 break while 1-2 run on), never a page-wide band. That is
    precisely why the archived band-first attempt failed: it required
    page-wide extent, and measured, only **20 of 2,226** horizontal
    `ocr_separator` rules span 8+ columns, so it discarded ~99% of the
    evidence. Strength is `n_columns` (how many DISTINCT columns agreed)
    — an evidence count, never a confidence score. Not a lattice fit:
    ads are sold by the column INCH so vertical rhythm is not quantised.
    Horizontal rules don't feed the stage-2 fit, so they independently
    corroborate it (rule endpoints within 1% of a column edge 52% vs 21%
    control). **Evidence is printed rules, photo edges and heading tops
    ONLY — block edges were removed**, they doubled the count to ~44/page
    with inferred boundaries that had no printed counterpart and obscured
    the real structure on render. Now ~20/page. No threshold baked in:
    all alignments stored, filtering is the caller's. `pages.content_top_pct`/
    `content_bottom_pct` need >=2 words per line (a one-word sheet-edge
    artefact moved a content top from 2.42% to 0.46%).
  - **Missing box rules: DON'T try to tune Tesseract** — measured across
    5 configs (Sauvola/Otsu/Leptonica-Otsu, psm 3/1, tables on/off) and
    separator output was IDENTICAL every time (12 on 1980-04-06 p5, 45 on
    p6), while the OCR text itself changed (3 distinct hashes) — so the
    variants applied and the layout analysis simply ignores them.
    Tesseract exposes no rule-sensitivity parameter. **Pixel-level rule
    detection does work** — thin-and-long filtering found the I.D.A. ad's
    top and bottom on p6 that Tesseract never reported. Prototype only
    (finds fewer rules overall so far):
    `transcribe/scaled/experiments/rule_detection_sources.py`.
    **LLM escalation is not yet justified** — the cheap classical route
    isn't exhausted.
  - **Stage 2c is PHOTO+CAPTION pairing** (`detect_captions.py`,
    `page_photo_captions` schema v19). A caption is the strip directly
    beneath a photo, within the photo's x-extent, terminated by a
    horizontal rule matching the photo's width, the next photo, or a gap.
    That one model covers both observed shapes: 1980-04-06 p1 (caption in
    TWO legs not following the page grid, closed by a rule at y 68.44
    matching the photo's 5.1-65.3 extent) and p3 (photo feature page,
    captions between photos). **Tesseract's `ocr_caption` is NOT the
    test** — only 5-7 lines/page, one caption block gets split across
    `ocr_caption`/`ocr_textfloat`/`ocr_header`, and it tags p7's page
    headline as a caption. Geometry decides; `ocr_caption` is recorded as
    corroboration. Multi-leg captions stay ONE record with `n_runs`.
    Nested ocr_photo regions are dropped (a caption can't belong to two
    photos). 45% of photos captioned corpus-wide.
  - **PAGE-PERCENT IS TWO UNITS.** `x%` is of page WIDTH, `y%` of page
    HEIGHT; they are interchangeable only on a square page. On
    1980-04-06 p13 the vertical reading is 1.41x the horizontal for the
    same physical distance. Every threshold written as one `_PCT`
    constant and applied to both axes is silently anisotropic — audited,
    and found in `drop_gutters` (whose "ratio" is not an aspect ratio: a
    SQUARE region scores 1.406), `merge_double_rules`, `_within_content`
    and `detect_boxes.INSET_PCT` (35px across vs 49px down). **Work in
    GRID CELLS, which are square by construction.** See §5z.7.
  - **READ `instructions/scaled_pipeline.md` §5z BEFORE CHANGING A
    DETECTOR.** Six documented process failures from this experiment,
    every one the same mistake in a different coat: substituting a number
    or a local check for looking at the page. Worst example — relaxing
    the corner requirement from 4 to 3 improved every corpus statistic
    (547→812 boxes, 80% agreement, under-finding pages 31→18) and the
    render was far worse; it was shipped on the numbers and reverted on
    sight. **Render the whole page before reporting anything.**
  - **NEXT, agreed 2026-08-16 — see `scaled_pipeline.md` §5o:** (1) columns
    pass 2 with boxed content and photos REMOVED, so the grid describes
    articles not ads — this is the fix for the measured 30%-of-blocks-
    inside-a-display-ad contamination that halved 1980-04-06 p2;
    (2) decide which boxed zones are articles rather than ads;
    (3) re-track horizontals now that boxes are known, separating ad
    boundaries from editorial ones; (4) join non-boxed content into single
    items, respecting modular layout — this is the 72%-of-token-cost prize.
  - **Confidence scoring is an ARCHIVED DEAD END**
    (`transcribe/scaled/archive/`). Earlier detectors discovered layout
    from weak signals then scored their own trustworthiness; every
    version certified itself, and each failure was caught only by
    rendering the page. `detect_grid` reports `fit` as a **diagnostic,
    with no gate**.
  - **Process lesson worth keeping:** the confidence metric initially
    scored a visibly-wrong page 0.853 because it measured only precision,
    never recall — the exact self-flattering-metrics failure
    `post1980_layout_observations.md` warns about. Caught by *rendering
    the page*. Never trust a score from this pipeline without running
    `transcribe/scaled/render_overlay.py` first.
  - Viewers: manifest is the contract, viewers are swappable clients.
    `preview/scaled/iiif/viewer.html` embeds Mirador (default, **the only
    one confirmed working**) and TIFY (**does NOT render overlays for
    these manifests — unresolved**; the `view:'fulltext'` fix was
    predicted to work and falsified on device). The stage-2 opt group is
    a single `columns` option now that pass 2 is archived. **Mirador's default
    `filteredMotivations` EXCLUDES `supplementing`** — the motivation all
    our annotations use — so an unconfigured/hosted Mirador silently
    shows nothing; that override is why it is embedded, not linked.
    Theseus removed (hosted-only, unpublished source, can't run locally
    or be configured). Clover and Universal Viewer ruled out — the IIIF
    matrix shows neither supports annotations at all. Manifests declare
    the **Text Granularity** extension (`block` for careas, `line` for
    line classes; omitted on separators/photos, which carry no text).
    Note the site is behind Cloudflare Access, so hosted third-party
    viewers cannot fetch these manifests at all.

**OCR+LLM route (1980s+ issues) is built and stable, entity extraction
split out 2026-08-09** — `transcribe/ocr_llm.py` (render/OCR/cleanup/
items pipeline, `--workers` thread pool on `render-issue`) now covers
Units 1-2 only (render+Tesseract, then `ocr-cleanup` + `ocr-items` —
segmentation/`item_type` only, no entity fields, no `entity_candidates.json`
— that ticket/schema/ingest code was removed, not just trimmed).
Entity/term extraction is Unit 3, a separate, independent, decoupled
pass: `transcribe/extract_terms.py` + `.claude/agents/term-extractor.md`
(Haiku, text-only, reads `items.full_text`, no image, no candidate
list, no dedup-matching attempt — same "batched, manually dispatched,
corpus-wide, decoupled in time" shape as `classify_terms.py`, not
wired into the per-page Workflow). Unit 4 (reconciliation) needed no
new matching logic: `ingest_item_result.py`'s existing
`upsert_entity`/`_insert_mentions` (normalised-key matching) is reused
as-is, called from `extract_terms.py:ingest_assignments()` instead of
inline from `ocr-items`'s own output — confirmed safe to call a second
time for an already-segmented item (idempotent by construction,
`INSERT OR IGNORE` keyed on `(item_id, entity_id, span_start)`).
`items.terms_extracted_at` (schema v12) is Unit 3's readiness signal,
scoped to `year >= 1980` so pre-1980 items (already handled by
`items-classifier.md`'s own inline extraction, unchanged) don't show
up as false positives. Verified end-to-end on a real batch (8 items,
1997-07-16 p4): extraction ran clean, ingest added mentions without
duplicating existing ones, re-ingest was a true no-op, and
`terminology_cleanup.py duplicates` picked up 36 genuine near-
duplicates from the batch (mostly organizations) — the expected,
accepted cost of dropping the candidate list: spelling-variant
reconciliation is now 100% that tier's job, not softened by
extraction-time hinting. Reason for the split (previous design just
trimmed the candidate list's wire format, which didn't hold up): see
git history same date. `.claude/skills/ocr-transcribe-issue/SKILL.md`,
`transcribe/workflows/ocr_llm_issue.js` (Units 1-2 only now),
`transcribe/workflows/extract_terms.js` (Unit 3), `transcribe/routing.py`
(1980 cutoff). Validated end-to-end pre-split on 2001-01-03 (12pp),
1994-01-05 (12pp), 1986-01-08 (18pp) — not yet re-validated as a full
issue post-split. Monitor at `transcribe/monitor.html`. Not yet done:
`items_ocr_ext.item_hocr`/`full_text_markdown` unpopulated (the
*page*-level `pages.hocr_path` is populated and durable, see the
2026-08-10 robustness paragraph below — this is a different, still-open
gap, the LLM-tidied item-scoped fragment); the 647 pre-existing
OCR+LLM-route items (from before this split) are all
`terms_extracted_at IS NULL` and will get picked up by the next
`extract_terms build` run, including the ~639 not yet touched by the
8-item verification batch.

**Robustness audit + fixes, 2026-08-10, ahead of a planned 2-issue
run** — a 3-agent audit (orchestration robustness / agent-prompt
clarity+token cost / reconciliation-at-scale risk) plus direct infra
checks found several real, confirmed-not-hypothesized problems; all
addressed same session:
  - `reconcile_terms.py` (Unit 4b, the LLM matching tier) had an
    unbounded dictionary (`entity_candidates.all_rows`, uncapped) and
    a dead `_chunk()` helper never called — the checkpoint
    (`schema_meta.reconcile_terms_last_run`) had also never
    initialized, so the very first real run would have been a
    full-corpus bootstrap sending ~1900-candidate tickets. Fixed:
    `dictionary()` now uses a new `entity_candidates.capped_rows()`
    (recency-sorted, capped at `DICTIONARY_CAP=150` — tighter than
    Unit 3's `MAX_CANDIDATES=500` since this tier is a second-pass
    safety net, not primary recall) and candidates are chunked at
    `CANDIDATE_CHUNK_SIZE=150` (deliberately close to the dictionary
    cap so a big backlog doesn't multiply the dictionary's cost many
    times over — verified live: 95 tickets at chunk=40 vs 28 at
    chunk=150 for the same 3723-candidate backlog). Also fixed a real
    checkpoint race: `_set_checkpoint` used to stamp `now()` at
    *ingest* time while candidates were read at *build* time, so
    anything created in between was silently, permanently skipped.
    Now `build_tickets` snapshots `as_of` once and stashes it as a
    pending checkpoint (`schema_meta.reconcile_terms_pending_as_of`),
    promoted to the real checkpoint only at ingest — verified with a
    synthetic race test (entity created "mid-cycle" correctly deferred
    to the next run, never lost).
  - `ocr-items` (Unit 2b) had a confirmed real silent-data-loss
    incident (4/188 blocks on 2001-01-03 p9) with no safety net besides
    a human remembering to run `verify-coverage` and hand-patch.
    `ocr_llm.recover_orphaned_blocks()` now runs automatically right
    after every page's items-ingest, bundling any still-unclaimed
    block's already-persisted OCR text (sourced from the page's saved
    `.hocr`/`page_ocr_blocks.raw_text`, zero LLM tokens) into an
    honest `repair_needed=1` catch-all item. Has a guard against the
    real false-positive this surfaced during testing: a page with zero
    items (items-pass never dispatched yet, e.g. 1997-01-08) must not
    be treated as "100% orphaned" — recovery only fires when the page
    already has at least one genuine item.
  - `ocr_llm_issue.js` had zero retry/timeout/content-filter handling;
    a failed page silently produced `items: []`, indistinguishable
    from "agent ran fine, found nothing." Now retries once on a thrown
    error and returns an explicit `failed`/`failure_reason` per page
    instead of ever silently dropping one. Schema v14 adds
    `pages.llm_status`/`llm_failure_count`/`llm_status_notes` (NOT a
    claim/lock table — this route runs one Workflow dispatch at a time
    from a single orchestrator session, no concurrent-worker race to
    guard against) — a page auto-escalates to `'damaged'` after
    `DAMAGED_THRESHOLD=2` separate failed runs and `render-issue` skips
    damaged pages by default (`--include-damaged` to override), so a
    consistently-failing page stops churning agents instead of being
    silently retried forever.
  - Stall-watcher ("last agent never ends") procedure documented in
    `ocr-transcribe-issue/SKILL.md` step 3, adapted from the older
    `transcribe-issue` pipeline's proven `Monitor`-based watcher —
    genuinely weaker here since `ocr-cleanup`/`ocr-items`/
    `term-extractor` are all `tools: Read` only (no incremental
    transcript-file byte-growth signal to watch, unlike
    `column-transcriber`), so it can only flag "unusually long"
    (floor 900s, not independently calibrated against real data the
    way the older pipeline's 300s floor was) rather than confirm
    "genuinely stuck." Not yet exercised against a real live dispatch.
  - `.claude/agents/term-extractor.md` gained explicit place-naming
    rules (street-suffix expansion; Ontario bare / elsewhere-Canada
    province / US state / other country context) and a stronger
    bare-first-name rule (actively infer a surname from item context,
    e.g. a birth announcement's named parents, before skipping).
  - **Not yet extended to `extract_terms.js`/`reconcile_terms.js`'s own
    dispatch** — the retry contingency above only covers
    `ocr_llm_issue.js`. Ad hoc DB backup taken 2026-08-10 (previous
    automated backup predated all of 2026-08-09's entity-curation
    work). No code has been run end-to-end against a real 2-issue
    dispatch yet with these fixes in place — verified via disposable DB
    copies and simulated results (zero LLM tokens spent on verification
    itself), not a live Workflow run.

**Entity registry + taxonomy cleanup, substantial work 2026-08-09** —
`people`/`organizations`/`places`/`products`/`events` carry
`first_seen_date`/`last_seen_date` (schema v6+). `transcribe/entities.html`
is the browsable table. `transcribe/merge_entity.py` is the one-line
merge CLI. Curation patterns baked into `items-classifier.md` (pre-1980
route, inline extraction, unchanged) and `term-extractor.md` (OCR+LLM
route, moved out of `ocr-items.md` in the 2026-08-09 split above):
period-abbreviation expansion (Wm.→William etc, original in
`mention_text`); products/events genericize `name` to the reusable
category (brand/instance goes in `manufacturer`/`mention_text`);
**"prefer names that will recur" and "picking the right altitude for
`name`"** are now explicit sections in both agent docs — a name should
sit one level more specific than its own `_type`, not repeat the
type's job (too generic) and not be a one-off brand/instance (too
specific) — distilled from real misses found today (Book/Movie/Play
had briefly landed in `gifts_and_novelties`, a bad reuse of an
existing bucket rather than a genuine fit).

**entities.html follow-up work, 2026-08-10** — added single-item
Rename and Delete alongside merge (`apply_terminology_decisions.py`
branches `_materialize_manual_review` on `review_kind`), a permanent
deletion blocklist (`upsert_entity()` refuses to recreate a
name matching an approved `'deletion'` rule, all 5 tables), a DB
metadata panel in the detail modal (real columns — `org_type`/
`place_type`/`product_type`/`event_type`, Nomenclature `external_*`,
notes/aliases — not just first/last-seen), and a sticky selection
toolbar. Found and fixed a real bug the same day: `apply_genericize()`
(the rename path) called `merge_entity(..., alias=False)`, silently
dropping the old name instead of recording it as an alias the way a
plain merge does — fixed (now `alias=True`) and the two already-
affected entities (Geneva, Arnprior) corrected by hand. New automatic
`street_abbrev_match` tier in `terminology_cleanup.py` auto-merges "X
St." into "X Street" for places directly (no per-name-pair rule
needed, trailing-position-only detection so it structurally can't fire
on a leading "St." Saint- name) — verified against the live corpus
before wiring in the auto-apply (1 real match, 0 false positives).

**Nomenclature for Museum Cataloging integration** — `transcribe/nomenclature.py`
is a SPARQL client (`https://nomenclature.info/sparql/rest/sparql/nom`,
no auth) that grounds `product_type` in an external, museum-curated
vocabulary where a term obviously matches (schema v9:
`products.external_terminology`/`external_category`/`external_uri`/
`external_reference` — vocabulary-agnostic naming, not Nomenclature-
specific, so a second future vocabulary reuses the same four columns).
`external_reference` (the bare catalog number, e.g. "13603") is what
lets the museum correlate a newspaper mention with objects in its own
collection — always derived from the URI in Python, never agent-
supplied. **Precision lesson, don't reintroduce:** an early version of
`nomenclature.search_terms()` had a CONTAINS-on-first-word fallback
that produced confident-looking wrong matches ("Coal Oil"→"charcoal",
"Comfort Soap"→"comfort station") when tested against the live corpus
in an unattended batch run — removed; exact-match-plus-singular-form
only now, precision over recall by design.

**Independent term-classification queue** —
`transcribe/classify_terms.py` + `transcribe/workflows/classify_terms.js`
+ `.claude/agents/term-classifier.md` (Haiku, text-only, no image)
backfill `org_type`/`place_type`/`product_type`/`event_type`
corpus-wide, decoupled from the render/cleanup/items pipeline —
doesn't matter which issue or when an entity was extracted. Run via
`python3 -m transcribe.classify_terms build` then dispatch the
Workflow, or fold into `terminology_cleanup.py run-all` (below).

**Terminology cleanup process (new 2026-08-09)** —
`transcribe/terminology_cleanup.py`, ad hoc today
(`python3 -m transcribe.terminology_cleanup run-all`), a LaunchAgent
template exists but isn't installed yet (`tools/com.mvtm.terminology_cleanup.plist`
— "if we can get productivity up" per the user, i.e. once the review
queue's signal-to-noise is trusted over a few ad hoc runs). Writes to
its own `terminology_reviews` table — **deliberately separate from
`repairs`**, which is the transcript/cutting-pipeline domain, not
entity/terminology. Review queue is `transcribe/terminology_review.html`
(own JSON store, refreshed by the same fast LaunchAgent loop as
`entities.html`/`monitor.html`; the cleanup *passes* themselves
are NOT in that fast loop — they make live external SPARQL calls and
take real time, so they're a separate, slower invocation). Three
passes, tested against the live corpus:
  - `duplicates` — cheap heuristic (stopword-strip + exact/substring
    match, bucketed by first char). Exact-normalized matches are
    reliable (0.9 confidence); substring-containment is **known
    noisy** (0.3 confidence, flagged as such in its own description)
    — a bare given name/surname/place-name prefix ("Elizabeth",
    "Naismith", "Lanark") is a substring of many real, unrelated
    longer entities, not a truncated alias of any one of them. No
    cheap mechanical filter found that separates that from real cases
    (Bell/Bell Canada) — this tier needs real human judgment, never
    auto-apply from it.
  - `nomenclature-gaps` — safe enrichment auto-applies (fills
    `external_*` when the match already equals the current
    `product_type`, changing nothing); a match that would actually
    change `product_type` raises a review instead.
  - `generic-names` — mechanical-only check (manufacturer still
    embedded in a product's own name, word-boundary match only, not a
    bare substring — a looser version flagged the patent-medicine
    exception "Dr. Pierce's Favorite Prescription" and would have
    mangled it). Currently finds nothing, which is correct — today's
    known cases were already fixed by hand.
  - Nothing in this module ever auto-merges or auto-renames; every
    non-enrichment finding gets a `suggested_cli` in `terminology_reviews`
    for a human to run after confirming it.

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

## CSS in pages that embed a third-party viewer

`instructions/css_standards.md` + `tools/check_css_scoping.py`. Short
version: on any page that mounts a third-party UI component (TIFY,
Mirador, a chart lib), **never write an unscoped selector** — no `*`, no
bare `select {}`/`button {}`, and nothing inheritable (font/color/
line-height) on `html`/`body`. They match the component's internal DOM.
This is not hypothetical: it silently broke TIFY's page-selector layout
on mobile 2026-08-15 (the viewer looked broken; the viewer was fine).
Self-contained pages like `entities.html` are exempt and the checker
skips them. Run `python3 tools/check_css_scoping.py` before committing
such a page.

## Think like a typesetter

`instructions/typesetting_practice.md` — **read this before designing any
layout detector.** These pages were assembled on a fixed, physical grid
(non-repro blue guides on a pasteboard; later QuarkXPress/PageMaker
master-page guides). The grid is an *input* to the page, not something to
be discovered from it: four numbers — margin, column width, gutter,
column count — measured in picas, with every photo, ad and story block
occupying an **integer** number of columns, never a fraction. Ads were
sold by the column inch, so they were quantised commercially before
anyone laid anything out; they are dummied first and editorial fills the
news hole around them. By the 1980s modular layout had won, so a page is
a packing of rectangles onto that grid.

The practical consequence: **fit a small number of parameters, don't
cluster freely and score each guess.** A deviation from the grid is far
more likely to be OCR noise, a photo, or scan distortion than a genuine
new column width. Most of the complexity in the scaled experiment's early
stages came from ignoring this.

## `instructions/` is the durable knowledge base — keep it current

Several files in `instructions/` document things that don't live in the
code, and that future agents (including future-me) will rely on as
context. Besides the three detailed below, see
`typesetting_practice.md` (how these pages were physically made — read
before designing any layout detector), `scaled_pipeline.md` (the
classic-first experiment's design record) and `css_standards.md`.

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
