# MVTM project notes

## Current status — read this first, keep it current

**Last updated: 2026-08-10.** This section is a live pointer, not a
durable rule — overwrite it (don't append to it) whenever the active
work changes materially: a campaign finishes, a new one starts, or a
session pauses mid-task for a reason a fresh session needs to know
(subagent cap, content-filter block, waiting on the user). The goal is
that a session starting cold can read this and `transcribe/work/experiments.jsonl`'s
tail and reconstruct state without replaying a whole prior transcript.

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
