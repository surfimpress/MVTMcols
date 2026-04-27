"""DB write facade for the per-issue pipeline.

All writes performed by `process_issue` go through a `DBWriter` instance.
This is the seam that lets the same pipeline run two ways:

- **Standalone** (`DirectDBWriter`): writes go straight to the local
  SQLite file, the shape we've always had — just behind a class.
- **Parallel batch** (`ProxyDBWriter`, added in the next commit):
  workers send write requests through a queue to a single coordinator
  that owns the only writing connection. Workers never block on the DB.

Reads stay direct from workers. SQLite is safe for concurrent readers
plus a single writer; only writes need routing.

This module deliberately knows nothing about how `DirectDBWriter` is
implemented internally — it composes the existing `store_ads` function
and the `LayoutDB` class. No detection logic, no coordinate handling,
no JSON construction lives here. The point is the seam, not the work.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing


class DBWriter(ABC):
    """All write operations the per-issue pipeline performs.

    Two implementations: `DirectDBWriter` (writes to local SQLite,
    used standalone) and `ProxyDBWriter` (sends to coordinator, used
    in batch). Reads stay direct from workers — only writes are routed.
    """

    @abstractmethod
    def delete_issue_ads(self, year, month, day):
        """Remove all detected_ads rows for the given issue."""

    @abstractmethod
    def delete_issue_layouts(self, year, month, day):
        """Remove page_layouts AND page_geometry rows for the issue.

        These are always cleaned together; the pair is treated as one
        operation so the coordinator can serialise them atomically.
        """

    @abstractmethod
    def store_ads(self, year, month, day, page, ads_with_uuids):
        """Insert a list of ad dicts (each carries its own uuid).

        Fire-and-forget. Workers never wait for IDs because each ad
        already knows its own identity (the uuid was assigned at
        detection time).
        """

    @abstractmethod
    def record_layout(self, year, month, day, page, num_columns,
                      boundary_positions, quality_flags, confidence):
        """Insert one page_layouts row."""

    @abstractmethod
    def record_geometry(self, year, month, day, page, profile):
        """Insert one page_geometry row."""


class DirectDBWriter(DBWriter):
    """Default writer: writes straight to local SQLite.

    The shape we have today, just behind the facade. Used when running
    standalone (no batch coordinator). Holds a `LayoutDB` instance for
    the layout/geometry methods (which already encapsulate their own
    SQL); other writes use a fresh short-lived connection per call,
    matching the existing pattern.
    """

    def __init__(self, db_path):
        # Imported here to avoid a top-level import cycle: layout_intelligence
        # imports nothing from us, but keeping the dep local makes the module
        # safe to import from process_issue early in startup.
        from layout_intelligence import LayoutDB

        self.db_path = db_path
        self._layout_db = LayoutDB(db_path)

    def delete_issue_ads(self, year, month, day):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "DELETE FROM detected_ads WHERE year=? AND month=? AND day=?",
                (year, month, day),
            )

    def delete_issue_layouts(self, year, month, day):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "DELETE FROM page_layouts WHERE year=? AND month=? AND day=?",
                (year, month, day),
            )
            conn.execute(
                "DELETE FROM page_geometry WHERE year=? AND month=? AND day=?",
                (year, month, day),
            )

    def store_ads(self, year, month, day, page, ads_with_uuids):
        # Delegate to the existing function in detect_ads — it already
        # knows the column shape and handles init_ads_table idempotently.
        from detect_ads import store_ads as _store_ads

        _store_ads(self.db_path, year, month, day, page, ads_with_uuids)

    def record_layout(self, year, month, day, page, num_columns,
                      boundary_positions, quality_flags, confidence):
        self._layout_db.record_layout(
            year, month, day, page, num_columns,
            boundary_positions, quality_flags, confidence,
        )

    def record_geometry(self, year, month, day, page, profile):
        self._layout_db.record_geometry(year, month, day, page, profile)
