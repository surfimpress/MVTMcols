-- Migration 001: Add uuid column to detected_ads.
--
-- Adds a UUID column alongside the integer id. UUIDs become the
-- worker-side identifier for ads, allowing parallel issue processing
-- with a single-writer DB coordinator (the workers no longer have to
-- wait for SQLite to assign auto-increment ids before continuing).
--
-- Existing rows get random backfill UUIDs; they're regenerated when
-- their issue is reprocessed. The integer id column is kept indefinitely
-- as a debug/log handle.
--
-- Run once. Idempotent re-runs are not supported; use the IF NOT EXISTS
-- pattern by hand if you need to re-apply on a partially-migrated DB.
--
-- See: /Users/peter/.claude/plans/issue-parallel-coordinator.md

BEGIN;

ALTER TABLE detected_ads ADD COLUMN uuid TEXT;

UPDATE detected_ads
   SET uuid = lower(hex(randomblob(16)))
 WHERE uuid IS NULL;

CREATE UNIQUE INDEX idx_detected_ads_uuid ON detected_ads(uuid);

COMMIT;
