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
    created_at        TEXT NOT NULL,
    notes             TEXT,
    FOREIGN KEY (page_id) REFERENCES pages(id),
    UNIQUE (page_id, block_idx)
);

CREATE INDEX IF NOT EXISTS idx_ocr_blocks_page ON page_ocr_blocks (page_id);


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
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    manufacturer    TEXT,
    product_type    TEXT,
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
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('schema_version', '6');
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('created_at_iso', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
