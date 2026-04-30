"""Connection helpers for recurrence_lab/recurrence.db.

The lab's own SQLite store. Authoritative for cluster membership,
applied labels, and (later phases) page-level appearances + proposals.
The main MVTM database (`../data/mvtm.db`) is opened read-only for
joins to `detected_ads`.

DDL is idempotent (`CREATE TABLE IF NOT EXISTS`) — running on a fresh
or existing DB is safe. Schema is extended phase by phase per
~/.claude/plans/stateless-frolicking-moth.md.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
DB_PATH = LAB_DIR / "recurrence.db"
MAIN_DB_PATH = LAB_DIR.parent / "data" / "mvtm.db"


# Phase 1 schema — clusters and per-ad membership.
# Extend with new CREATE TABLE blocks when Phase 2/3/4 land.
SCHEMA_PHASE1 = """
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id    INTEGER PRIMARY KEY,
    size          INTEGER NOT NULL,
    n_issues      INTEGER NOT NULL,
    first_date    TEXT NOT NULL,
    last_date     TEXT NOT NULL,
    exemplar_path TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL DEFAULT 'unclassified'
        CHECK (category IN ('unclassified', 'ad', 'body_text_fp', 'furniture')),
    name          TEXT,
    notes         TEXT,
    labelled_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_clusters_category ON clusters(category);
CREATE INDEX IF NOT EXISTS idx_clusters_exemplar ON clusters(exemplar_path);
CREATE INDEX IF NOT EXISTS idx_clusters_size ON clusters(size DESC);

CREATE TABLE IF NOT EXISTS cluster_membership (
    image_filename TEXT PRIMARY KEY,
    issue_dir      TEXT NOT NULL,
    page           INTEGER NOT NULL,
    cluster_id     INTEGER NOT NULL REFERENCES clusters(cluster_id),
    similarity     REAL NOT NULL,
    rejected       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_membership_cluster ON cluster_membership(cluster_id);
CREATE INDEX IF NOT EXISTS idx_membership_issue ON cluster_membership(issue_dir);

-- Operational view: clusters that share a category + name are
-- treated as one logical cluster (per plan: name-as-merge-key, strict
-- equality). Cross-category collisions are excluded — apply_labels
-- rejects them up-front, so they shouldn't reach the DB, but the
-- view is conservative anyway.
CREATE VIEW IF NOT EXISTS merged_clusters AS
SELECT
    name,
    category,
    SUM(size)               AS total_size,
    COUNT(*)                AS n_subclusters,
    MIN(first_date)         AS first_date,
    MAX(last_date)          AS last_date,
    GROUP_CONCAT(cluster_id) AS member_cluster_ids,
    GROUP_CONCAT(exemplar_path, '|') AS exemplar_paths
FROM clusters
WHERE name IS NOT NULL AND TRIM(name) != ''
GROUP BY name, category;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column adds for existing DBs that pre-date a schema bump.

    sqlite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so we
    introspect via PRAGMA. Each block here is paired with the matching
    column in the `CREATE TABLE` above; both must agree.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cluster_membership)")}
    if "rejected" not in cols:
        conn.execute(
            "ALTER TABLE cluster_membership "
            "ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0"
        )


def open_db(create: bool = True) -> sqlite3.Connection:
    """Open recurrence.db for read+write.

    Applies WAL mode and a sane busy timeout. Runs the schema DDL
    when `create=True` so callers don't need a separate "init" step.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if create:
        conn.executescript(SCHEMA_PHASE1)
        _migrate(conn)
        conn.commit()
    return conn


def open_main_readonly() -> sqlite3.Connection:
    """Open ../data/mvtm.db in read-only mode for joins.

    Uses URI mode + `?mode=ro` so accidental writes raise rather than
    silently mutating the production DB.
    """
    if not MAIN_DB_PATH.exists():
        raise FileNotFoundError(f"main DB not found: {MAIN_DB_PATH}")
    uri = f"file:{MAIN_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
