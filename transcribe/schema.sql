-- transcribe/schema.sql
--
-- Canonical schema for transcribe.db. Read by bootstrap_db.py and by
-- test_schema_roundtrip.py. Edit this file to evolve the schema; the
-- migration story (when the time comes) layers on top.
--
-- Conventions:
--   - All primary keys are UUID strings (TEXT). uuid.uuid4() at insert.
--   - All bounding boxes are page-percentages, never pixels.
--     pct↔px conversions go through coordinates.py in the parent repo.
--   - Every table has created_at and notes (free-form curation field).
--   - Cross-DB joins use ATTACH DATABASE '<repo>/data/mvtm.db' AS mvtm.

PRAGMA foreign_keys = ON;

-- Pass-1A: per-column diplomatic transcripts -------------------------
-- One row per (issue, page, col_idx, image_sha256). Re-cuts of a
-- column produce a new row (because image_sha256 changes); the prior
-- row stays for history and is no longer the latest.

CREATE TABLE IF NOT EXISTS column_transcripts (
    id                TEXT PRIMARY KEY,
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    day               INTEGER NOT NULL,
    page              INTEGER NOT NULL,
    col_idx           INTEGER NOT NULL,           -- 0-based
    image_path        TEXT NOT NULL,              -- repo-relative
    image_sha256      TEXT NOT NULL,
    status            TEXT NOT NULL,              -- 'claimed'|'done'|'failed'
    transcript_text   TEXT,
    transcriber_notes TEXT,
    quality_flags     TEXT,                       -- JSON
    repair_needed     INTEGER NOT NULL DEFAULT 0,
    repair_reason     TEXT,
    model             TEXT,
    prompt_hash       TEXT,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    cost_usd          REAL,
    -- Wall-clock duration and tool-call count for the column-transcriber
    -- agent run that produced this row, as reported by the orchestrating
    -- Claude Code session's own completion notification. Not part of the
    -- agent's JSON envelope -- the agent has no visibility into its own
    -- overall duration or call count, so the orchestrator records these
    -- via record_agent_usage() in a follow-up UPDATE after ingest. NULL
    -- for rows transcribed before this field was added (2026-08-04) or
    -- ingested by a caller that doesn't report usage.
    agent_duration_ms INTEGER,
    agent_tool_calls  INTEGER,
    raw_response_json TEXT,
    -- JSON list of slice records produced by transcribe/slice.py:
    -- [{idx, y_top_pct, y_bottom_pct, image_path, char_offset_start,
    --   char_offset_end, top_rule_class, bottom_rule_class,
    --   subdivided, ...}, ...]
    -- Provenance for the joined transcript_text and the input to
    -- pass-2 item segmentation. Null for legacy rows transcribed
    -- without slicing (the full-PNG mode that pre-dates the
    -- 2026-05-02 refinement).
    slice_boundaries  TEXT,
    -- Set only when the page-level deduplicator agent edits
    -- transcript_text to remove a slice-boundary overlap duplicate
    -- (see column-transcriber.md "Sliced mode" for why the overlap
    -- exists in the first place). Holds the pre-edit joined text so
    -- a dedup mistake is always recoverable. NULL means either the
    -- dedup pass hasn't run yet, or it ran and found nothing to
    -- change.
    transcript_text_raw TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT,
    notes             TEXT,
    UNIQUE (year, month, day, page, col_idx, image_sha256)
);

CREATE INDEX IF NOT EXISTS idx_col_issue
    ON column_transcripts (year, month, day);
CREATE INDEX IF NOT EXISTS idx_col_status
    ON column_transcripts (status);


-- Pass-1B: per-ad diplomatic transcripts -----------------------------

CREATE TABLE IF NOT EXISTS ad_transcripts (
    id                TEXT PRIMARY KEY,
    ad_uuid           TEXT NOT NULL,              -- mvtm.detected_ads.uuid
    year              INTEGER NOT NULL,
    month             INTEGER NOT NULL,
    day               INTEGER NOT NULL,
    page              INTEGER NOT NULL,
    image_path        TEXT NOT NULL,
    image_sha256      TEXT NOT NULL,
    status            TEXT NOT NULL,
    transcript_text   TEXT,
    transcriber_notes TEXT,
    quality_flags     TEXT,
    repair_needed     INTEGER NOT NULL DEFAULT 0,
    repair_reason     TEXT,
    model             TEXT,
    prompt_hash       TEXT,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    cost_usd          REAL,
    raw_response_json TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT,
    notes             TEXT,
    UNIQUE (ad_uuid, image_sha256)
);

CREATE INDEX IF NOT EXISTS idx_ad_issue
    ON ad_transcripts (year, month, day);
CREATE INDEX IF NOT EXISTS idx_ad_status
    ON ad_transcripts (status);


-- Pass-2: items ------------------------------------------------------
-- An item is a discrete unit of newspaper content (article, ad,
-- notice, masthead, cartoon, ...). Items can span columns (an
-- article continuing into the next column) or be insets that cut
-- across columns (display ads).

CREATE TABLE IF NOT EXISTS items (
    id                       TEXT PRIMARY KEY,
    item_type                TEXT NOT NULL,
    year                     INTEGER NOT NULL,
    month                    INTEGER NOT NULL,
    day                      INTEGER NOT NULL,
    page                     INTEGER NOT NULL,
    bbox_left_pct            REAL NOT NULL,
    bbox_top_pct             REAL NOT NULL,
    bbox_right_pct           REAL NOT NULL,
    bbox_bottom_pct          REAL NOT NULL,
    column_span_json         TEXT,                -- JSON list of col_idx
    crosses_columns          INTEGER NOT NULL DEFAULT 0,
    is_inset                 INTEGER NOT NULL DEFAULT 0,
    crosses_pages            INTEGER NOT NULL DEFAULT 0,
    continued_to_item_id     TEXT,                -- nullable FK
    continued_from_item_id   TEXT,                -- nullable FK
    headline                 TEXT,
    byline                   TEXT,
    summary                  TEXT,                -- ≤500 chars
    full_text                TEXT,
    language                 TEXT NOT NULL DEFAULT 'en',
    classification_confidence REAL,
    model                    TEXT,
    prompt_hash              TEXT,
    tokens_in                INTEGER,
    tokens_out               INTEGER,
    cost_usd                 REAL,
    raw_response_json        TEXT,
    repair_needed            INTEGER NOT NULL DEFAULT 0,
    repair_reason            TEXT,
    created_at               TEXT NOT NULL,
    updated_at               TEXT,
    notes                    TEXT,
    -- Pass-3 (items-tidier) additions. NULL on pass-2 rows.
    -- geometry_polygon_json: JSON list of [x_pct, y_pct] vertices forming
    -- a closed polygon (first vertex repeated as last) for items that
    -- visually wrap an inset (e.g. an article around a display ad).
    -- When non-null, this is the truthful geometry; bbox_*_pct columns
    -- still hold the polygon's bounding rectangle for fast queries.
    -- derived_from_item_ids: JSON list of pass-2 items.id values that
    -- this row was derived from. Single-element for 1->1 corrections,
    -- multi-element for merges, single-element repeated across rows
    -- for splits.
    geometry_polygon_json    TEXT,
    derived_from_item_ids    TEXT,
    -- terms_extracted_at: set once transcribe.extract_terms has run its
    -- independent term-extraction pass on this item (Unit 3 of the
    -- OCR+LLM route's split pipeline). NULL means "not processed yet",
    -- the sole readiness signal extract_terms.pending_items() selects
    -- on -- there is no other column on this table that plays that role.
    terms_extracted_at       TEXT,
    FOREIGN KEY (continued_to_item_id)   REFERENCES items(id),
    FOREIGN KEY (continued_from_item_id) REFERENCES items(id)
);

CREATE INDEX IF NOT EXISTS idx_items_issue
    ON items (year, month, day, page);
CREATE INDEX IF NOT EXISTS idx_items_type
    ON items (item_type);


CREATE TABLE IF NOT EXISTS item_column_spans (
    item_id              TEXT NOT NULL,
    column_transcript_id TEXT NOT NULL,
    sequence             INTEGER NOT NULL,
    start_offset         INTEGER NOT NULL,
    end_offset           INTEGER NOT NULL,
    bbox_top_pct         REAL,
    bbox_bottom_pct      REAL,
    PRIMARY KEY (item_id, column_transcript_id, sequence),
    FOREIGN KEY (item_id) REFERENCES items(id),
    FOREIGN KEY (column_transcript_id)
        REFERENCES column_transcripts(id)
);


CREATE TABLE IF NOT EXISTS item_ad_associations (
    item_id  TEXT NOT NULL,
    ad_uuid  TEXT NOT NULL,                       -- mvtm.detected_ads.uuid
    PRIMARY KEY (item_id, ad_uuid),
    FOREIGN KEY (item_id) REFERENCES items(id)
);


-- OCR+LLM route: page-level OCR ---------------------------------------
-- An alternate route to pass-1/2, for eras where the classical column-
-- cut pipeline doesn't apply (1980s+ modular layouts that resisted
-- column detection -- see instructions/layout_observations.md). Starts
-- from a whole-page Tesseract hOCR pass instead of cut column images.
-- `pages` anchors page-level OCR/render facts (doesn't exist for the
-- classical route -- page is just an int column there). `page_ocr_blocks`
-- is the peer of column_transcripts: one row per Tesseract text block.
-- `items_ocr_ext` is a 1:1 extension of items, populated only for items
-- produced this way -- its mere existence for an item_id is the
-- provenance marker, so items itself needs no new column. `item_ocr_
-- block_spans` is the peer of item_column_spans: block membership
-- instead of char-offset spans into a column transcript.

CREATE TABLE IF NOT EXISTS pages (
    id                    TEXT PRIMARY KEY,
    year                  INTEGER NOT NULL,
    month                 INTEGER NOT NULL,
    day                   INTEGER NOT NULL,
    page                  INTEGER NOT NULL,
    pdf_path              TEXT,
    page_raw_path         TEXT,              -- full-res render at render_dpi, OCR's own coordinate space
    render_dpi            INTEGER,
    ocr_engine            TEXT,              -- e.g. 'tesseract 5.5.3'
    ocr_trained_data      TEXT,              -- 'tessdata_fast'|'tessdata'|'tessdata_best'
    thresholding_method   TEXT,              -- 'otsu'|'sauvola'
    hocr_path             TEXT,
    hocr_word_count       INTEGER,
    hocr_mean_confidence  REAL,
    layout_class          TEXT,              -- 'column_grid'|'modular' -- routing hint
    -- Downscaled PNG shown to the item-markup LLM pass (token/size
    -- reasons) -- a distinct raster from page_raw_path, so item bboxes
    -- (given by that pass in this image's pixel space) need their own
    -- dimensions to convert to page-pct. Without these an item bbox is
    -- ambiguous: which of two differently-sized rasters is it measured
    -- against.
    display_image_path   TEXT,
    display_width_px     INTEGER,
    display_height_px    INTEGER,
    -- Lightweight outcome tracking for the LLM stages (cleanup+items),
    -- not a claim/lock -- this route dispatches one Workflow call at a
    -- time from a single orchestrator session, so there's no concurrent-
    -- worker race to guard against the way the older column-cut
    -- pipeline's claim_columns.py needs. NULL means "not attempted yet"
    -- (the render/OCR step alone doesn't set this). 'done' on a
    -- successful ingest; 'failed' when a page's cleanup or items call
    -- errors even after the workflow's own one-shot retry;
    -- llm_failure_count increments on every 'failed' outcome and
    -- render-issue auto-flips a page to 'damaged' once it crosses
    -- DAMAGED_THRESHOLD (see ocr_llm.py) -- a damaged page is skipped
    -- by default on future runs (no more agent churn on a page that
    -- keeps failing) until a human clears it by hand.
    llm_status            TEXT,              -- NULL|'done'|'failed'|'damaged'
    llm_failure_count      INTEGER NOT NULL DEFAULT 0,
    llm_status_notes       TEXT,
    -- Scaled track (schema v15). hocr_parsed_at is the readiness
    -- signal for transcribe/scaled/hocr_parse.py, exactly the same "own column,
    -- own cadence" shape as items.terms_extracted_at -- each stage runs
    -- independently and never blocks another. scan_res_* is Tesseract's
    -- own reported scanner resolution from the hOCR ocr_page element
    -- (note it is the *source image's* dpi, not RENDER_DPI, because
    -- render_page() prefers a native embedded bitmap at its own dpi).
    hocr_parsed_at        TEXT,
    scan_res_x            INTEGER,
    scan_res_y            INTEGER,
    created_at            TEXT NOT NULL,
    notes                 TEXT,
    UNIQUE (year, month, day, page)
);

CREATE INDEX IF NOT EXISTS idx_pages_issue ON pages (year, month, day);


CREATE TABLE IF NOT EXISTS page_ocr_blocks (
    id                TEXT PRIMARY KEY,
    page_id           TEXT NOT NULL,
    block_idx         INTEGER NOT NULL,     -- 0-based, Tesseract's own block order
    bbox_left_pct     REAL NOT NULL,
    bbox_top_pct      REAL NOT NULL,
    bbox_right_pct    REAL NOT NULL,
    bbox_bottom_pct   REAL NOT NULL,
    conf              REAL,                 -- Tesseract's own avg block confidence, 0-100
    n_words           INTEGER,
    raw_text          TEXT,
    cleaned_text      TEXT,
    cleanup_status    TEXT,                 -- 'clean'|'corrected'|'noise'|NULL (untriaged, trusted as-is)
    triaged           INTEGER NOT NULL DEFAULT 0,
    model             TEXT,
    prompt_hash       TEXT,
    tokens_in         INTEGER,
    tokens_out        INTEGER,
    cost_usd          REAL,
    -- Scaled track (schema v15), filled by transcribe/scaled/hocr_parse.py.
    -- block_class is always 'ocr_carea' for rows the existing route
    -- creates (it only ever selects careas); it exists so the column is
    -- explicit rather than implied. x_size_median is the median
    -- Tesseract x-height of this block's lines -- a font-size proxy, the
    -- single strongest signal the original parser discarded (body ~35px,
    -- headlines to ~320px on a 1990 page).
    block_class       TEXT,
    x_size_median     REAL,
    created_at        TEXT NOT NULL,
    notes             TEXT,
    FOREIGN KEY (page_id) REFERENCES pages(id),
    UNIQUE (page_id, block_idx)
);

CREATE INDEX IF NOT EXISTS idx_ocr_blocks_page ON page_ocr_blocks (page_id);


-- Scaled track (schema v15) ------------------------------------
-- Tesseract emits far more layout signal than ocr_llm.parse_hocr()
-- keeps. These two tables recover it from the .hocr files already on
-- disk -- no re-OCR, no LLM. See transcribe/scaled/hocr_parse.py's docstring
-- and instructions/scaled_pipeline.md for the measured evidence.

-- The line level, which the original parser skipped entirely (it jumps
-- carea -> word). line_class is Tesseract's OWN layout judgement:
-- 'ocr_line' is ordinary body text, but 'ocr_header' / 'ocr_caption' /
-- 'ocr_textfloat' are free heading/caption/float detection. ocr_caption
-- maps directly onto item_ocr_block_spans.role='caption', which today
-- is populated only by an LLM.
CREATE TABLE IF NOT EXISTS page_hocr_lines (
    id                TEXT PRIMARY KEY,
    page_id           TEXT NOT NULL,
    page_ocr_block_id TEXT,                 -- nullable: block may predate this parse
    block_idx         INTEGER NOT NULL,
    line_class        TEXT NOT NULL,        -- ocr_line|ocr_header|ocr_caption|ocr_textfloat
    left_pct          REAL NOT NULL,
    top_pct           REAL NOT NULL,
    right_pct         REAL NOT NULL,
    bottom_pct        REAL NOT NULL,
    x_size            REAL,                 -- Tesseract x-height in px (font-size proxy)
    x_ascenders       REAL,
    x_descenders      REAL,
    baseline_slope    REAL,                 -- per-line skew; deskew/rotation QA
    par_top_pct       REAL,                 -- owning ocr_par, for paragraph grouping
    n_words           INTEGER,
    text              TEXT,
    FOREIGN KEY (page_id) REFERENCES pages(id),
    FOREIGN KEY (page_ocr_block_id) REFERENCES page_ocr_blocks(id)
);

CREATE INDEX IF NOT EXISTS idx_hocr_lines_page ON page_hocr_lines (page_id);
CREATE INDEX IF NOT EXISTS idx_hocr_lines_class ON page_hocr_lines (line_class);


-- Non-text blocks: ocr_separator (printed rules) and ocr_photo (image
-- regions). These are SIBLINGS of ocr_carea, direct children of
-- ocr_page, which is exactly why the carea-only XPath never saw them.
-- A vertical separator is a literal column boundary -- the primary
-- signal for transcribe/scaled/detect_columns.py. orientation is derived from the
-- bbox aspect ratio (see hocr_parse._orientation), so it needs no page
-- dimensions and no image read.
CREATE TABLE IF NOT EXISTS page_hocr_regions (
    id                TEXT PRIMARY KEY,
    page_id           TEXT NOT NULL,
    region_class      TEXT NOT NULL,        -- 'ocr_separator'|'ocr_photo'
    orientation       TEXT NOT NULL,        -- 'vertical'|'horizontal'|'block'
    left_pct          REAL NOT NULL,
    top_pct           REAL NOT NULL,
    right_pct         REAL NOT NULL,
    bottom_pct        REAL NOT NULL,
    width_px          INTEGER,
    height_px         INTEGER,
    FOREIGN KEY (page_id) REFERENCES pages(id)
);

CREATE INDEX IF NOT EXISTS idx_hocr_regions_page ON page_hocr_regions (page_id);


-- Column boundaries derived from hOCR geometry alone (no pixels, no
-- LLM). Deliberately separate from the pre-1980 route's
-- mvtm.page_layouts -- that is the classical pixel cutter's output and
-- is not touched. `method` records which signal produced the boundary
-- set so disagreement between signals stays inspectable rather than
-- being averaged away. `confidence` drives the LLM-escalation gate.
CREATE TABLE IF NOT EXISTS page_columns (
    id                TEXT PRIMARY KEY,
    page_id           TEXT NOT NULL,
    col_idx           INTEGER NOT NULL,     -- 0-based, left to right
    left_pct          REAL NOT NULL,
    right_pct         REAL NOT NULL,
    method            TEXT NOT NULL,        -- 'separator'|'leftedge'|'valley'|'combined'
    confidence        REAL,                 -- 0-1; below the gate -> escalate to LLM
    created_at        TEXT NOT NULL,
    notes             TEXT,
    FOREIGN KEY (page_id) REFERENCES pages(id),
    UNIQUE (page_id, method, col_idx)
);

CREATE INDEX IF NOT EXISTS idx_page_columns_page ON page_columns (page_id);

-- Scaled track (schema v16). For 1980+ the layout unit is a BAND (a
-- horizontal strip bounded by a wide rule or a whitespace gap), and
-- columns exist only WITHIN a band -- full-height columns are the wrong
-- model for that era (97.8% escalation; see instructions/scaled_pipeline.md).
CREATE TABLE IF NOT EXISTS page_bands (
    id                TEXT PRIMARY KEY,
    page_id           TEXT NOT NULL,
    band_idx          INTEGER NOT NULL,     -- 0-based, top to bottom
    top_pct           REAL NOT NULL,
    bottom_pct        REAL NOT NULL,
    n_columns         INTEGER NOT NULL,
    column_edges_json TEXT,                 -- JSON list of x-edges, left..right
    regularity        REAL,                 -- 1 - CV of column widths in this band
    n_lines           INTEGER,
    confidence        REAL,                 -- page-level score, repeated per band
    created_at        TEXT NOT NULL,
    notes             TEXT,
    FOREIGN KEY (page_id) REFERENCES pages(id),
    UNIQUE (page_id, band_idx)
);
CREATE INDEX IF NOT EXISTS idx_page_bands_page ON page_bands (page_id);


CREATE TABLE IF NOT EXISTS items_ocr_ext (
    item_id             TEXT PRIMARY KEY,
    item_hocr           TEXT,              -- LLM-tidied, item-scoped hOCR fragment
    full_text_markdown  TEXT,              -- markdown companion to items.full_text
    media_paths_json    TEXT,              -- escape valve for item-specific derivatives
    created_at          TEXT NOT NULL,
    notes               TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id)
);


CREATE TABLE IF NOT EXISTS item_ocr_block_spans (
    item_id           TEXT NOT NULL,
    page_ocr_block_id TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'body',   -- 'body'|'caption'
    sequence          INTEGER NOT NULL,
    PRIMARY KEY (item_id, page_ocr_block_id, role),
    FOREIGN KEY (item_id)           REFERENCES items(id),
    FOREIGN KEY (page_ocr_block_id) REFERENCES page_ocr_blocks(id)
);


-- Usage telemetry for the OCR+LLM route's two LLM passes.
-- `ocr_llm_runs` is one row per Workflow invocation (or per manual
-- single-agent dispatch, run_id NULL on the calling side in that
-- case) -- total_tokens here is the harness's own reported aggregate
-- for the run, trusted as exact.
-- `page_llm_calls` is the per-page/per-kind breakdown, recovered by
-- parsing each agent's raw transcript after the fact (see
-- transcribe/workflow_usage.py) -- useful for relative
-- page-to-page comparison, but does NOT reconcile exactly to the
-- parent run's total_tokens (~70-80% of it in the two runs checked
-- 2026-08-09; the harness's aggregation formula isn't fully
-- understood). Don't sum this table and expect it to match
-- ocr_llm_runs.total_tokens -- display both, don't paper over the gap.

CREATE TABLE IF NOT EXISTS ocr_llm_runs (
    id              TEXT PRIMARY KEY,
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    day             INTEGER NOT NULL,
    pages_json      TEXT,              -- JSON list of page numbers this run covered
    agent_count     INTEGER,
    total_tokens    INTEGER,           -- harness-reported aggregate, trusted exact
    total_tool_calls INTEGER,
    duration_ms     INTEGER,           -- wall-clock for the whole run
    started_at      TEXT,
    ended_at        TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ocr_llm_runs_issue ON ocr_llm_runs (year, month, day);


CREATE TABLE IF NOT EXISTS page_llm_calls (
    id            TEXT PRIMARY KEY,
    run_id        TEXT,               -- nullable FK; NULL for manual single-agent dispatches
    page_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,      -- 'cleanup'|'items'
    model         TEXT,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    tool_calls    INTEGER,
    duration_ms   INTEGER,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (run_id)  REFERENCES ocr_llm_runs(id),
    FOREIGN KEY (page_id) REFERENCES pages(id)
);
CREATE INDEX IF NOT EXISTS idx_page_llm_calls_page ON page_llm_calls (page_id);


-- Entities -----------------------------------------------------------
-- Each entity has a normalised_key for first-pass loose dedup
-- (lowercased, punctuation-stripped). Cross-corpus disambiguation
-- is a later pass, not done at ingest.

-- first_seen_date/last_seen_date (ISO date of the earliest/latest
-- *mention* ingested, not a biographical fact) let a candidate-list
-- prefetch filter to entities plausible for a given issue's date
-- without a per-mention DB lookup -- see transcribe/entity_candidates.py.
-- Maintained by upsert_entity() (MIN/MAX on every mention, not just
-- first-write). The decade index buckets by the first 3 digits of the
-- year (e.g. '193' = 1930s) -- coarse on purpose, a candidate-list
-- prefilter, not a precise range query.

CREATE TABLE IF NOT EXISTS people (
    id              TEXT PRIMARY KEY,
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    title           TEXT,                         -- Mr/Mrs/Dr/Rev/Hon/...
    suffix          TEXT,                         -- Jr/Sr/III/...
    normalised_key  TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_people_norm ON people (normalised_key);
CREATE INDEX IF NOT EXISTS idx_people_last_first
    ON people (last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_people_decade
    ON people (substr(first_seen_date, 1, 3));


CREATE TABLE IF NOT EXISTS organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    org_type        TEXT,
    normalised_key  TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_orgs_norm ON organizations (normalised_key);


CREATE TABLE IF NOT EXISTS places (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    place_type       TEXT,                        -- city/town/region/...
    parent_place_id  TEXT,                        -- nullable FK
    normalised_key   TEXT NOT NULL,
    first_seen_date  TEXT,
    last_seen_date   TEXT,
    created_at       TEXT NOT NULL,
    notes            TEXT,
    FOREIGN KEY (parent_place_id) REFERENCES places(id)
);
CREATE INDEX IF NOT EXISTS idx_places_norm ON places (normalised_key);


CREATE TABLE IF NOT EXISTS products (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    manufacturer         TEXT,
    product_type         TEXT,
    -- External controlled-vocabulary cross-reference -- lets a museum
    -- correlate a newspaper mention with objects it holds in its own
    -- collection. Generic on purpose: today this only ever gets
    -- populated from Nomenclature for Museum Cataloging
    -- (nomenclature.info, see transcribe/nomenclature.py), but the
    -- fields aren't named after it -- a future second vocabulary
    -- (Getty AAT, ICONCLASS, whatever) reuses the same four columns
    -- rather than getting its own set. external_terminology names
    -- which vocabulary+edition was used (e.g. "Nomenclature 4.0" --
    -- confirmed 2026-08-09 against nomenclature.info's own about page;
    -- the live site is a continuously-updated superset of the 2015 4.0
    -- print edition), external_category is the matched concept's own
    -- label (e.g. "Documentary Objects"), external_uri its concept URI
    -- for automated lookups, external_reference the bare catalog
    -- number a museum registrar would actually cite (e.g. "13603" --
    -- the URI's own last path segment, derived, never agent-supplied).
    -- All four NULL means product_type is our own organic term, not
    -- sourced externally -- expected for things Nomenclature doesn't
    -- catalog (perishables/groceries; it's a museum-object vocabulary,
    -- not a retail one).
    external_terminology  TEXT,
    external_category     TEXT,
    external_uri          TEXT,
    external_reference    TEXT,
    normalised_key  TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_norm ON products (normalised_key);


CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    year_known      INTEGER,                      -- nullable
    date_known      TEXT,                         -- nullable, ISO date
    event_type      TEXT,
    normalised_key  TEXT NOT NULL,
    first_seen_date TEXT,
    last_seen_date  TEXT,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_norm ON events (normalised_key);


-- Mention junction tables --------------------------------------------
-- Capture the role and the exact original mention text + char offsets
-- so genealogy queries can show the entity in its original context.

CREATE TABLE IF NOT EXISTS item_people_mentions (
    item_id       TEXT NOT NULL,
    person_id     TEXT NOT NULL,
    role          TEXT,                           -- 'subject'|'byline'|'mentioned'|...
    mention_text  TEXT,
    span_start    INTEGER,                        -- offset into items.full_text
    span_end      INTEGER,
    confidence    REAL,
    PRIMARY KEY (item_id, person_id, span_start),
    FOREIGN KEY (item_id)   REFERENCES items(id),
    FOREIGN KEY (person_id) REFERENCES people(id)
);

CREATE TABLE IF NOT EXISTS item_organizations_mentions (
    item_id       TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    role          TEXT,
    mention_text  TEXT,
    span_start    INTEGER,
    span_end      INTEGER,
    confidence    REAL,
    PRIMARY KEY (item_id, organization_id, span_start),
    FOREIGN KEY (item_id)         REFERENCES items(id),
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS item_places_mentions (
    item_id       TEXT NOT NULL,
    place_id      TEXT NOT NULL,
    role          TEXT,
    mention_text  TEXT,
    span_start    INTEGER,
    span_end      INTEGER,
    confidence    REAL,
    PRIMARY KEY (item_id, place_id, span_start),
    FOREIGN KEY (item_id)  REFERENCES items(id),
    FOREIGN KEY (place_id) REFERENCES places(id)
);

CREATE TABLE IF NOT EXISTS item_products_mentions (
    item_id       TEXT NOT NULL,
    product_id    TEXT NOT NULL,
    role          TEXT,
    mention_text  TEXT,
    span_start    INTEGER,
    span_end      INTEGER,
    confidence    REAL,
    PRIMARY KEY (item_id, product_id, span_start),
    FOREIGN KEY (item_id)    REFERENCES items(id),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

CREATE TABLE IF NOT EXISTS item_events_mentions (
    item_id       TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    role          TEXT,
    mention_text  TEXT,
    span_start    INTEGER,
    span_end      INTEGER,
    confidence    REAL,
    PRIMARY KEY (item_id, event_id, span_start),
    FOREIGN KEY (item_id)  REFERENCES items(id),
    FOREIGN KEY (event_id) REFERENCES events(id)
);


-- Repairs ------------------------------------------------------------
-- Either pass can raise a repair ticket. Repairs link back to the
-- existing mvtm_cli.py mutators by emitting a suggested invocation
-- string; running the invocation is a manual step (the repair table
-- never auto-mutates mvtm.db).

CREATE TABLE IF NOT EXISTS repairs (
    id                TEXT PRIMARY KEY,
    target_kind       TEXT NOT NULL,              -- 'column'|'ad'|'page'|...
    target_ref_json   TEXT NOT NULL,              -- {year,month,day,page,col_idx} etc.
    repair_kind       TEXT NOT NULL,
    description       TEXT,
    proposed_fix_json TEXT,
    suggested_cli     TEXT,
    status            TEXT NOT NULL DEFAULT 'open',
    raised_by         TEXT,                       -- model name or 'human'
    raised_at         TEXT NOT NULL,
    acted_at          TEXT,
    resolved_at       TEXT,
    related_item_id   TEXT,
    related_column_id TEXT,
    related_ad_uuid   TEXT,
    notes             TEXT
);
CREATE INDEX IF NOT EXISTS idx_repairs_status ON repairs (status);


-- Terminology reviews -------------------------------------------------
-- Deliberately separate from `repairs`: repairs are about transcript/
-- cutting-pipeline problems tied to a page/column/ad; this table is
-- about the entity registry (people/organizations/places/products/
-- events) and its taxonomies -- a different domain, a different
-- lifecycle, raised by transcribe/terminology_cleanup.py rather than
-- the transcription passes. Same non-mutating philosophy as repairs
-- though: this table only ever proposes (via suggested_cli or
-- proposed_fix_json), a human or an explicit apply step executes.

CREATE TABLE IF NOT EXISTS terminology_reviews (
    id                TEXT PRIMARY KEY,
    entity_type       TEXT NOT NULL,              -- 'people'|'organizations'|'places'|'products'|'events'
    entity_id         TEXT,                       -- primary/only entity this review is about
    other_entity_id   TEXT,                       -- second entity, for duplicate_candidate reviews
    review_kind       TEXT NOT NULL,              -- 'duplicate_candidate'|'nomenclature_gap'|'name_too_specific'|'type_near_duplicate'
    description       TEXT,
    confidence        REAL,                       -- 0-1, heuristic or agent-assigned
    proposed_fix_json TEXT,
    suggested_cli     TEXT,
    status            TEXT NOT NULL DEFAULT 'open', -- 'open'|'applied'|'dismissed'
    raised_by         TEXT,                       -- 'terminology_cleanup' pass name, or model name
    raised_at         TEXT NOT NULL,
    resolved_at       TEXT,
    notes             TEXT,
    -- provenance: 'python' (deterministic heuristic, terminology_cleanup.py),
    -- 'llm' (term-reconciler.md via reconcile_terms.py), or 'human' (a
    -- person manually flagging a pair via entities.html, materialized
    -- on the fly by apply_terminology_decisions._materialize_manual_review
    -- -- no auto-raise pass involved at all). Drives both the UI's
    -- provenance chip and reconcile_terms.py's own context-feed query
    -- (see terminology_rules.provenance below).
    provenance        TEXT NOT NULL DEFAULT 'python'
);
CREATE INDEX IF NOT EXISTS idx_terminology_reviews_status ON terminology_reviews (status);
CREATE INDEX IF NOT EXISTS idx_terminology_reviews_kind ON terminology_reviews (review_kind);


-- Terminology rules -----------------------------------------------------
-- "Always" decisions from terminology_review.html -- permanent, name-
-- keyed (not entity-id-keyed like terminology_reviews.status='dismissed'
-- already is) so a rule survives an entity getting deleted/recreated
-- (a merge, a data rebuild) and so a future *different* entity pair
-- that happens to match the same names is covered too, not just the
-- exact row pair that existed when the human decided.
--
-- 'ignore' rules make terminology_cleanup.py skip raising a review at
-- all for a matching case. 'approve' rules make it apply the fix
-- directly and record the review as already 'applied' -- no human
-- click needed for something already decided once. Because 'approve
-- always' auto-applies future matches unattended, terminology_review
-- .html gates creating one behind a confirm() -- see its Save handler.

CREATE TABLE IF NOT EXISTS terminology_rules (
    id                TEXT PRIMARY KEY,
    entity_type       TEXT NOT NULL,
    review_kind       TEXT NOT NULL,
    match_key         TEXT NOT NULL,              -- normalized name (single-entity kinds) or
                                                    -- sorted "name_a|name_b" (duplicate_candidate)
    decision          TEXT NOT NULL,              -- 'approve'|'ignore'
    proposed_fix_json TEXT,                       -- for 'approve': what to apply (mirrors terminology_reviews)
    created_at        TEXT NOT NULL,
    notes             TEXT,
    -- provenance: the source review's provenance at the moment this rule
    -- was created (threaded through by apply_terminology_decisions.py) --
    -- 'python', 'llm', or 'human' (see terminology_reviews.provenance
    -- above). reconcile_terms.py's confirmed_examples() reads
    -- provenance IN ('llm','human') approve-rules as few-shot context
    -- for its next run -- an 'approve always' on a python-tier review
    -- stays a simple mechanical rule with no further effect; the same
    -- decision on an llm- or human-sourced review also feeds the
    -- matcher's own future context.
    provenance        TEXT NOT NULL DEFAULT 'python',
    UNIQUE (entity_type, review_kind, match_key)
);


-- Telemetry ----------------------------------------------------------
-- One row per orchestrator invocation. Optional but useful for
-- throughput tuning and tracking which models were used in initial
-- comparison runs.

CREATE TABLE IF NOT EXISTS transcribe_runs (
    id                  TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    run_kind            TEXT NOT NULL,            -- 'column_pass'|'ad_pass'|'item_pass'
    scope_json          TEXT,
    model               TEXT,
    status              TEXT,
    error_message       TEXT,
    columns_attempted   INTEGER DEFAULT 0,
    columns_succeeded   INTEGER DEFAULT 0,
    ads_attempted       INTEGER DEFAULT 0,
    ads_succeeded       INTEGER DEFAULT 0,
    items_created       INTEGER DEFAULT 0,
    repairs_raised      INTEGER DEFAULT 0,
    total_tokens_in     INTEGER,
    total_tokens_out    INTEGER,
    total_cost_usd      REAL,
    notes               TEXT
);


-- Schema metadata ----------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Schema versions:
--   1 — initial (2026-05-02): columns/ads/items + entity-mention tables
--   2 — pass-3 additions (2026-05-06): items.geometry_polygon_json,
--                                      items.derived_from_item_ids
--   3 — dedup audit trail (2026-07-30): column_transcripts.transcript_text_raw
--   4 — OCR+LLM route (2026-08-08): pages, page_ocr_blocks, items_ocr_ext,
--                                    item_ocr_block_spans
--   5 — OCR+LLM display raster (2026-08-08): pages.display_image_path,
--                                    display_width_px, display_height_px
--   6 — entity temporal index (2026-08-08): first_seen_date/last_seen_date
--                                    on people/organizations/places/
--                                    products/events + decade index
--   7 — OCR+LLM usage telemetry (2026-08-09): ocr_llm_runs, page_llm_calls
--   8 — external-vocabulary cross-reference, v1 (2026-08-09):
--                                    products.nomenclature_category,
--                                    products.nomenclature_uri --
--                                    superseded by v9's generic naming,
--                                    see below; don't re-add these names
--   9 — external-vocabulary cross-reference, generalized (2026-08-09):
--                                    products.external_terminology,
--                                    external_category, external_uri,
--                                    external_reference -- vocabulary-
--                                    agnostic naming so a future second
--                                    external vocabulary (today only
--                                    Nomenclature for Museum Cataloging
--                                    populates these) reuses the same
--                                    four columns instead of getting
--                                    its own set
--  10 — terminology_reviews (2026-08-09): entity-registry cleanup
--                                    review queue, deliberately
--                                    separate from repairs (transcript/
--                                    cutting domain) -- raised by
--                                    transcribe/terminology_cleanup.py
--  11 — terminology_rules (2026-08-09): permanent, name-keyed
--                                    "always" decisions from
--                                    terminology_review.html -- survive
--                                    entity id changes, unlike
--                                    terminology_reviews.status alone
--  12 — items.terms_extracted_at (2026-08-09): readiness/completion
--                                    signal for the OCR+LLM route's
--                                    independent term-extraction pass
--                                    (transcribe/extract_terms.py) --
--                                    entity extraction moved out of
--                                    ocr-items (page segmentation) into
--                                    its own decoupled Unit 3, this is
--                                    how it finds items it hasn't
--                                    processed yet
--  13 — terminology provenance (2026-08-09): terminology_reviews.provenance
--                                    and terminology_rules.provenance
--                                    ('python'|'llm') -- Unit 4 gained a
--                                    second, LLM-based matching tier
--                                    (term-reconciler.md via
--                                    transcribe/reconcile_terms.py)
--                                    alongside the original Python
--                                    heuristics in terminology_cleanup.py;
--                                    an 'approve always' decision on an
--                                    llm-provenance review feeds the
--                                    confirmed pair forward as context
--                                    into the matcher's next run, a
--                                    python-provenance review's rule
--                                    stays purely mechanical
--  14 — OCR+LLM lightweight page status (2026-08-10): pages.llm_status,
--                                    llm_failure_count, llm_status_notes --
--                                    tracks cleanup+items outcome per page
--                                    (NULL/'done'/'failed'/'damaged') so a
--                                    page that keeps failing gets skipped
--                                    on future runs instead of churning
--                                    agents against it forever. Not a
--                                    claim/lock table -- this route runs
--                                    one Workflow dispatch at a time from a
--                                    single orchestrator session, so there's
--                                    no concurrent-worker race to guard
--                                    against
--  15 — scaled track (2026-08-15): pages.hocr_parsed_at/
--                                    scan_res_x/scan_res_y,
--                                    page_ocr_blocks.block_class/
--                                    x_size_median, and three new
--                                    tables page_hocr_lines,
--                                    page_hocr_regions, page_columns.
--                                    Recovers the layout signal
--                                    ocr_llm.parse_hocr() discards
--                                    (separators, photos, x_size, line
--                                    classes) so columns and items can
--                                    be derived without an LLM. Purely
--                                    additive — the existing OCR+LLM
--                                    route is untouched and keeps
--                                    running. See
--                                    instructions/scaled_pipeline.md
--  16 — scaled track, bands (2026-08-15): page_bands. For 1980+
--                                    the layout unit is a horizontal
--                                    band and columns exist only
--                                    within one; full-height columns
--                                    escalated 97.8% of pages
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('schema_version', '16');
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('created_at_iso', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- v17: stage 3 of the `scaled` experiment -- horizontal alignments.
-- A post-1980 page is a mosaic of rectangles, so a horizontal edge is
-- LOCAL: it spans a run of columns, not the whole page. col_lo/col_hi
-- record that span. `n_columns` is how many distinct columns agreed on
-- the alignment -- evidence count, NOT a confidence score (see
-- transcribe/scaled/archive/README.md for why this distinction matters).
CREATE TABLE IF NOT EXISTS page_hlines (
    id          TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    y_pct       REAL NOT NULL,
    col_lo      INTEGER NOT NULL,
    col_hi      INTEGER NOT NULL,
    n_columns   INTEGER NOT NULL,
    n_edges     INTEGER NOT NULL,
    weight      REAL,
    kinds       TEXT,
    has_rule    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_hlines_page ON page_hlines(page_id, y_pct);
