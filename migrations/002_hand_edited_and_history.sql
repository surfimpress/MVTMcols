-- Migration 002: hand_edited columns on mutable tables + cli_history audit table.
--
-- The LLM-facing CLI surface (mvtm) lets an operator correct one row
-- at a time. We need:
--   1. A per-row flag so the pipeline knows "do not overwrite this on
--      the next process_issue run" (preserves the manual correction).
--   2. An audit table recording every mutating CLI call so an operator
--      can audit, undo, or replay corrections.
--
-- Both additive. No data is destroyed; existing rows get hand_edited=0
-- by default. cli_history is empty until the first mutator lands.
--
-- Why row_key_json (vs typed columns): the affected row is identified
-- by uuid for ads, by (year,month,day,page) for layouts/geometry. One
-- text column with a JSON-encoded composite key keeps the schema flat
-- across all three target tables.
--
-- Run once. Not idempotent; re-applying on a partially-migrated DB
-- requires editing by hand.
--
-- See: /Users/peter/.claude/plans/cli-walking-skeleton.md

BEGIN;

ALTER TABLE page_layouts  ADD COLUMN hand_edited INTEGER DEFAULT 0;
ALTER TABLE detected_ads  ADD COLUMN hand_edited INTEGER DEFAULT 0;
ALTER TABLE page_geometry ADD COLUMN hand_edited INTEGER DEFAULT 0;

CREATE TABLE cli_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    command TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_key_json TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT
);

CREATE INDEX idx_cli_history_ts ON cli_history(ts);

COMMIT;
