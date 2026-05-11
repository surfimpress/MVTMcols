-- Migration 003: issue_backups — track which (year, month, day) issue
-- trees have been mirrored to a remote and md5-verified.
--
-- Why this exists:
--   Disk pressure on the 228 GB SSD forces us to treat Drive as the
--   canonical store for "cold" years and keep local `columns/<YYYY>-MM-DD/`
--   trees as a working cache. Before any process is allowed to delete a
--   local issue tree (`tools/archive_year.sh`), we need a hard interlock
--   that says "this exact issue is on the remote with matching md5s".
--   That interlock is a row in this table with md5_verified=1.
--
-- Grain: one row per (year, month, day, remote). One issue can in
-- principle live on more than one remote (e.g. Drive + S3); the unique
-- constraint preserves that flexibility without fanning the schema.
--
-- Populated by: tools/backup_year.sh (after the rclone check passes,
-- it walks each issue dir under the year, counts files+bytes, and
-- inserts/updates one row per issue with md5_verified=1).
--
-- Read by: tools/archive_year.sh (refuses to delete unless every
-- issue dir under the year has md5_verified=1 for the configured
-- remote); future restore/preflight tooling.
--
-- Run once. Additive — no existing data touched.

BEGIN;

CREATE TABLE issue_backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    day INTEGER NOT NULL,
    remote TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    bytes_local INTEGER NOT NULL,
    md5_verified INTEGER DEFAULT 0,
    backed_up_at TEXT DEFAULT (datetime('now')),
    verified_at TEXT,
    manifest_path TEXT,
    UNIQUE(year, month, day, remote)
);

CREATE INDEX idx_issue_backups_issue ON issue_backups(year, month, day);
CREATE INDEX idx_issue_backups_year ON issue_backups(year);

COMMIT;
