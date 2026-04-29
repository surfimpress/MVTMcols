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

    @abstractmethod
    def update_issue_data(self, year, month, day):
        """Refresh the on-disk viewer files for one issue.

        Writes `columns/issues/{date}.json` for the issue and rebuilds
        the lightweight `columns/index.json` + flat `columns/ads.json`.
        Workers send this as their last message per issue — by the time
        the coordinator dispatches it, every prior write for the issue
        has already been applied (FIFO queue).
        """


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

    # Hand-edit respect (introduced 2026-04-27, migration 002).
    #
    # Any DB row with `hand_edited = 1` represents a manual correction
    # made via the `mvtm` CLI. The pipeline must not overwrite it on a
    # subsequent process_issue run. The skip logic lives entirely here
    # in the seam, so process_issue itself stays unchanged: it always
    # asks the writer to delete-then-insert; the writer enforces the
    # preservation invariant.
    #
    # Ads: hand-edited rows are kept across the issue-level wipe; new
    # detections are still inserted (each has its own uuid, so no
    # primary-key collision). The natural duplicate-on-the-page risk
    # is accepted: when a real LLM-mutator pattern emerges (uuid-based
    # update vs full re-detection), we'll revisit. Until then,
    # preservation > coexistence-cost.
    #
    # Layouts/geometry: one row per (year, month, day, page). If that
    # row is hand-edited, we skip both the DELETE and the subsequent
    # INSERT for that exact page — leaving the manual values intact.

    def _hand_edited_pages(self, table_name, year, month, day):
        """Return the set of page numbers in this issue with
        hand_edited=1 in the named table."""
        with closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(
                f"SELECT page FROM {table_name} "
                f"WHERE year=? AND month=? AND day=? AND hand_edited=1",
                (year, month, day),
            ).fetchall()
        return {r[0] for r in rows}

    def delete_issue_ads(self, year, month, day):
        skipped = self._hand_edited_pages("detected_ads", year, month, day)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "DELETE FROM detected_ads "
                "WHERE year=? AND month=? AND day=? AND hand_edited=0",
                (year, month, day),
            )
        for page in sorted(skipped):
            print(f"  [skip P{page} ads delete: hand-edited rows preserved]")

    def delete_issue_layouts(self, year, month, day):
        layout_skip = self._hand_edited_pages("page_layouts", year, month, day)
        geom_skip = self._hand_edited_pages("page_geometry", year, month, day)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "DELETE FROM page_layouts "
                "WHERE year=? AND month=? AND day=? AND hand_edited=0",
                (year, month, day),
            )
            conn.execute(
                "DELETE FROM page_geometry "
                "WHERE year=? AND month=? AND day=? AND hand_edited=0",
                (year, month, day),
            )
        for page in sorted(layout_skip):
            print(f"  [skip P{page} layout delete: hand-edited]")
        for page in sorted(geom_skip):
            print(f"  [skip P{page} geometry delete: hand-edited]")

    def _is_hand_edited(self, table_name, year, month, day, page):
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                f"SELECT 1 FROM {table_name} "
                f"WHERE year=? AND month=? AND day=? AND page=? AND hand_edited=1",
                (year, month, day, page),
            ).fetchone()
        return row is not None

    def store_ads(self, year, month, day, page, ads_with_uuids):
        # Delegate to the existing function in detect_ads — it already
        # knows the column shape and handles init_ads_table idempotently.
        # Hand-edited ads have already survived delete_issue_ads; the
        # fresh inserts coexist with them (distinct uuids).
        from detect_ads import store_ads as _store_ads

        _store_ads(self.db_path, year, month, day, page, ads_with_uuids)

    def record_layout(self, year, month, day, page, num_columns,
                      boundary_positions, quality_flags, confidence):
        if self._is_hand_edited("page_layouts", year, month, day, page):
            print(f"  [skip P{page} layout insert: hand-edited row exists]")
            return
        self._layout_db.record_layout(
            year, month, day, page, num_columns,
            boundary_positions, quality_flags, confidence,
        )

    def record_geometry(self, year, month, day, page, profile):
        if self._is_hand_edited("page_geometry", year, month, day, page):
            print(f"  [skip P{page} geometry insert: hand-edited row exists]")
            return
        self._layout_db.record_geometry(year, month, day, page, profile)

    def update_issue_data(self, year, month, day):
        # Local import to keep db_writer free of process_issue's heavy
        # detection-module deps at top-of-file.
        from process_issue import update_issue_data as _update
        _update(self.db_path, "columns", year, month, day)


class ProxyDBWriter(DBWriter):
    """Worker-side writer: forwards every call to a coordinator queue.

    Used by workers in the parallel batch pipeline. Each method packages
    its arguments into a tuple `(method_name, args)` and puts it on the
    queue. A coordinator thread in the parent process drains the queue
    and dispatches each message to a `DirectDBWriter` — which is the
    sole owner of the writing connection.

    Pure fan-in. No reply queue; workers never block on the DB. The
    queue itself is unbounded so `put()` never blocks either.

    The method-name strings here are the wire format. They must match
    the public method names on `DirectDBWriter` exactly — the
    coordinator dispatches via `getattr(direct_writer, method_name)`.
    """

    def __init__(self, req_queue):
        self._q = req_queue

    def delete_issue_ads(self, year, month, day):
        self._q.put(("delete_issue_ads", (year, month, day)))

    def delete_issue_layouts(self, year, month, day):
        self._q.put(("delete_issue_layouts", (year, month, day)))

    def store_ads(self, year, month, day, page, ads_with_uuids):
        self._q.put(("store_ads", (year, month, day, page, ads_with_uuids)))

    def record_layout(self, year, month, day, page, num_columns,
                      boundary_positions, quality_flags, confidence):
        self._q.put((
            "record_layout",
            (year, month, day, page, num_columns,
             boundary_positions, quality_flags, confidence),
        ))

    def record_geometry(self, year, month, day, page, profile):
        self._q.put(("record_geometry", (year, month, day, page, profile)))

    def update_issue_data(self, year, month, day):
        # Sent as the final message for the issue. FIFO ordering means
        # all earlier writes for this issue (delete_*, store_ads,
        # record_layout, record_geometry) have already been processed
        # by the coordinator before it picks this up.
        self._q.put(("update_issue_data", (year, month, day)))
