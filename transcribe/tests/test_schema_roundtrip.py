"""Tests for transcribe.db schema bootstrap and ATTACH.

These tests run against a temporary DB so they don't touch the
real ``transcribe/data/transcribe.db``. They verify:

1. The schema script applies cleanly to a fresh DB.
2. All expected tables exist after bootstrap.
3. ATTACH-mode read-only flag actually prevents writes to mvtm.db.
4. The claim/done ticket-state transitions work end to end.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

# Run via: python3 -m unittest transcribe.tests.test_schema_roundtrip

import transcribe.db as tdb


_EXPECTED_TABLES = {
    "ad_transcripts",
    "column_transcripts",
    "events",
    "item_ad_associations",
    "item_column_spans",
    "item_events_mentions",
    "item_organizations_mentions",
    "item_people_mentions",
    "item_places_mentions",
    "item_products_mentions",
    "items",
    "organizations",
    "people",
    "places",
    "products",
    "repairs",
    "schema_meta",
    "transcribe_runs",
}


class SchemaRoundtripTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        # Apply schema fresh.
        with open(tdb.SCHEMA_PATH) as f:
            schema = f.read()
        conn = sqlite3.connect(self.db_path)
        conn.executescript(schema)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db_path)

    def test_all_tables_present(self):
        conn = tdb.open_connection(self.db_path)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in rows}
        missing = _EXPECTED_TABLES - names
        self.assertFalse(
            missing, f"Missing expected tables: {missing}")
        conn.close()

    def test_attach_mvtm_readonly(self):
        # If the parent's mvtm.db isn't around, skip — we don't want
        # this test to be flaky for someone running the suite without
        # the full repo data.
        if not os.path.isfile(tdb.MVTM_DB_PATH):
            self.skipTest("parent mvtm.db not present")
        conn = tdb.open_connection(self.db_path, attach_mvtm=True)
        try:
            # Read should work.
            conn.execute(
                "SELECT name FROM mvtm.sqlite_master "
                "WHERE type='table' LIMIT 1").fetchone()
            # Write should fail because we attached with mode=ro.
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO mvtm.schema_meta (key, value) "
                    "VALUES ('test', 'test')")
        finally:
            conn.close()

    def test_claim_then_done_roundtrip(self):
        conn = tdb.open_connection(self.db_path)
        try:
            row_id = tdb.claim_column(
                conn,
                year=1892, month=1, day=1, page=1, col_idx=0,
                image_path="columns/1892-01-01/p1/test_col1.png",
                image_sha256="deadbeef" * 8)

            self.assertIsInstance(row_id, str)
            self.assertEqual(len(row_id), 36)  # uuid4 string length

            # Idempotent re-claim returns the same id.
            row_id_again = tdb.claim_column(
                conn,
                year=1892, month=1, day=1, page=1, col_idx=0,
                image_path="columns/1892-01-01/p1/test_col1.png",
                image_sha256="deadbeef" * 8)
            self.assertEqual(row_id, row_id_again)

            tdb.mark_column_done(
                conn, row_id,
                transcript_text="The quick brown fox.",
                transcriber_notes="clean cut",
                quality_flags={"adjacent_text_visible": False},
                repair_needed=False,
                repair_reason=None,
                model="claude-sonnet-4-6",
                prompt_hash_value="prompthash" * 4,
                raw_response_json=json.dumps({"transcript_text": "x"}))

            row = conn.execute(
                "SELECT status, transcript_text, model "
                "FROM column_transcripts WHERE id=?",
                (row_id,)).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["transcript_text"],
                             "The quick brown fox.")
            self.assertEqual(row["model"], "claude-sonnet-4-6")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
