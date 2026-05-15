-- Migration 004: file_assets — per-file index of locally-cut artefacts
-- and their Drive (or other remote) copies.
--
-- Why this exists:
--   `issue_backups` only records that an issue was rclone'd to a remote; it
--   doesn't say anything about individual files. We have no per-file Drive
--   URLs for column slices, ad crops, or page rasters. Without those, the
--   viewer can't deep-link cold-storage artefacts, and we can't answer
--   "where is THIS column on Drive?" without re-walking the remote tree.
--
--   Adding a row-per-file table is the missing index. It complements:
--     - `page_layouts` / `page_geometry`  (page-level, column boundaries as JSON)
--     - `detected_ads`                    (per-ad rows, no remote ref)
--     - `issue_backups`                   (per-issue sync record, no per-file)
--
-- Grain: one row per (remote, local_path). The same local file backed up
-- to two remotes (e.g. Drive + S3 future) gets two rows. Most queries hit
-- (year, month, day, page) — denormalised onto the row for index efficiency.
--
-- `kind` is a free-text tag rather than an enum (see project memory:
-- taxonomies are iterative, not locked enums). Expected values:
--     'page_raw'      → columns/<date>/p<N>/page_raw.png
--     'page_display'  → columns/<date>/p<N>/page_display.avif
--     'column'        → columns/<date>/p<N>/p<N>_colM.png
--     'ad'            → columns/<date>/ads/p<N>/<uuid>.png
-- New kinds can be added without schema changes.
--
-- `ad_uuid` is denormalised onto rows where `kind='ad'` so callers can join
-- to `detected_ads.uuid` directly. NULL for non-ad rows.
--
-- Populated by: a future post-rclone step (will run `rclone lsjson` on the
-- issue tree, then upsert one row per file). Not yet wired in — this
-- migration is the schema only.
--
-- Run once. Additive — no existing data touched.

BEGIN;

CREATE TABLE file_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Issue / page coordinates, denormalised for index efficiency.
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    day INTEGER NOT NULL,
    page INTEGER,                       -- NULL for issue-level files (none yet, but allowed)

    kind TEXT NOT NULL,                 -- 'page_raw' | 'page_display' | 'column' | 'ad' | ...
    filename TEXT NOT NULL,             -- basename, e.g. 'p3_col2.png' or '<uuid>.png'

    -- Optional cross-link to detected_ads.uuid when kind='ad'.
    ad_uuid TEXT,

    -- Local-side facts at sync time.
    local_path TEXT NOT NULL,           -- repo-relative, e.g. 'columns/1924-05-30/p3/p3_col2.png'
    bytes INTEGER,
    md5_local TEXT,

    -- Remote-side facts. `remote` matches the convention used in
    -- issue_backups ('mvtm:' for the Drive rclone remote).
    remote TEXT NOT NULL,
    drive_id TEXT,                      -- Google Drive file ID
    drive_url TEXT,                     -- https://drive.google.com/file/d/<id>/view
    drive_md5 TEXT,                     -- MD5 reported by Drive (for verification)

    synced_at TEXT DEFAULT (datetime('now')),
    verified_at TEXT,

    UNIQUE(remote, local_path)
);

CREATE INDEX idx_file_assets_issue    ON file_assets(year, month, day);
CREATE INDEX idx_file_assets_page     ON file_assets(year, month, day, page);
CREATE INDEX idx_file_assets_kind     ON file_assets(kind);
CREATE INDEX idx_file_assets_ad_uuid  ON file_assets(ad_uuid) WHERE ad_uuid IS NOT NULL;

COMMIT;
