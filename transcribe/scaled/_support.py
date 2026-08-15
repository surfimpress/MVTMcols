"""Self-contained support for the scaled experiment.

**Deliberately duplicated, not imported.** By explicit direction
(2026-08-15) this experiment stays as standalone as feasible: it copies
the functions and concepts it needs and leaves the originals alone. That
means a change here cannot perturb the production pipeline, and the
experiment can be deleted wholesale without unpicking shared imports.

Sources these were copied from, for future reconciliation:
  - `pct_to_px` / `px_to_pct`  <- repo-root `coordinates.py`
  - `open_connection` / `new_uuid` / `now_iso` <- `transcribe/db.py`

The one thing NOT duplicated is the database file itself: the experiment
writes to the same `transcribe.db` on purpose (parallel track, same DB),
so its output can be compared against production output in one query.
Only additive schema-v15 tables and columns are ever written.

If the originals change materially, reconcile by hand and note it in
instructions/scaled_pipeline.md -- do not silently re-point at them.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import uuid

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# transcribe/scaled/ -> transcribe/ -> repo root
TRANSCRIBE_DIR = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
REPO_ROOT = os.path.abspath(os.path.join(TRANSCRIBE_DIR, os.pardir))
TRANSCRIBE_DB_PATH = os.path.join(TRANSCRIBE_DIR, "data", "transcribe.db")


# --- coordinates (copied from repo-root coordinates.py) --------------
# The pipeline's convention: every position is a percentage of the FULL
# page, origin top-left. The recurring bug class this guards against is
# passing a percentage measured against one reference into a conversion
# using another. Rounding precision below matches the original exactly:
# pct_to_px rounds to nearest int, px_to_pct rounds to 2 decimals.

def pct_to_px(pct, dim):
    """Page percentage -> integer pixel position on an image of size `dim`."""
    return round(pct / 100.0 * dim)


def px_to_pct(px, dim):
    """Pixel position -> page percentage, 2dp (the pipeline's canonical
    precision)."""
    if not dim:
        return 0.0
    return round(px / dim * 100.0, 2)


# --- db (copied from transcribe/db.py) -------------------------------

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    return str(uuid.uuid4())


def open_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open the shared transcribe.db with the same pragmas production
    uses. Read/write: this experiment writes only to schema-v15 additive
    tables (page_hocr_lines, page_hocr_regions, page_columns) and the
    additive columns on pages/page_ocr_blocks."""
    path = db_path or TRANSCRIBE_DB_PATH
    if not os.path.isfile(path):
        raise FileNotFoundError(f"transcribe.db not found at {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn
