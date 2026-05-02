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


-- Entities -----------------------------------------------------------
-- Each entity has a normalised_key for first-pass loose dedup
-- (lowercased, punctuation-stripped). Cross-corpus disambiguation
-- is a later pass, not done at ingest.

CREATE TABLE IF NOT EXISTS people (
    id              TEXT PRIMARY KEY,
    first_name      TEXT,
    last_name       TEXT,
    full_name       TEXT NOT NULL,
    title           TEXT,                         -- Mr/Mrs/Dr/Rev/Hon/...
    suffix          TEXT,                         -- Jr/Sr/III/...
    normalised_key  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_people_norm ON people (normalised_key);
CREATE INDEX IF NOT EXISTS idx_people_last_first
    ON people (last_name, first_name);


CREATE TABLE IF NOT EXISTS organizations (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    org_type        TEXT,
    normalised_key  TEXT NOT NULL,
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
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('schema_version', '1');
INSERT OR IGNORE INTO schema_meta (key, value)
    VALUES ('created_at_iso', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
