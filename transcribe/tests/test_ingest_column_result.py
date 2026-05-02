"""Tests for transcribe.ingest_column_result.

Cover the parse-envelope path (good + common bad shapes), the
fence-stripping behaviour, and the full ingest cycle including the
repair-needed branch. The tests use a temporary transcribe.db and
a synthetic ticket file to avoid touching any real LLM pipeline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

import transcribe.db as tdb
import transcribe.ingest_column_result as ing


def _good_envelope(*, repair=False) -> dict:
    return {
        "transcript_text": "The quick brown fox.",
        "transcriber_notes": "",
        "quality_flags": {
            "damage": False,
            "faded": False,
            "smudged": False,
            "low_legibility": False,
            "partial_cut": False,
            "adjacent_text_visible": False,
        },
        "repair_needed": repair,
        "repair_reason": "left edge cut into next column" if repair else "",
    }


class ParseEnvelopeTest(unittest.TestCase):

    def test_strips_json_fence(self):
        s = "```json\n" + json.dumps(_good_envelope()) + "\n```"
        out = ing.parse_envelope(s)
        self.assertEqual(out["transcript_text"], "The quick brown fox.")

    def test_strips_bare_fence(self):
        s = "```\n" + json.dumps(_good_envelope()) + "\n```"
        out = ing.parse_envelope(s)
        self.assertEqual(out["transcript_text"], "The quick brown fox.")

    def test_rejects_non_object(self):
        with self.assertRaises(ValueError):
            ing.parse_envelope("[1, 2, 3]")

    def test_rejects_missing_field(self):
        env = _good_envelope()
        del env["transcript_text"]
        with self.assertRaises(ValueError) as ctx:
            ing.parse_envelope(json.dumps(env))
        self.assertIn("transcript_text", str(ctx.exception))

    def test_rejects_missing_flag(self):
        env = _good_envelope()
        del env["quality_flags"]["damage"]
        with self.assertRaises(ValueError) as ctx:
            ing.parse_envelope(json.dumps(env))
        self.assertIn("damage", str(ctx.exception))

    def test_rejects_non_bool_flag(self):
        env = _good_envelope()
        env["quality_flags"]["damage"] = "no"
        with self.assertRaises(ValueError):
            ing.parse_envelope(json.dumps(env))

    def test_rejects_invalid_json(self):
        with self.assertRaises(ValueError):
            ing.parse_envelope("not json at all")


class IngestRoundtripTest(unittest.TestCase):
    """Full cycle: claim a row, write a ticket file + result file,
    run ingest, confirm the row is 'done' and (if applicable) a
    repair was raised."""

    def setUp(self):
        # Fresh DB.
        self.tmp_db = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False)
        self.tmp_db.close()
        with open(tdb.SCHEMA_PATH) as f:
            schema = f.read()
        conn = sqlite3.connect(self.tmp_db.name)
        conn.executescript(schema)
        conn.commit()
        conn.close()

        # Redirect db module's TRANSCRIBE_DB_PATH for this test.
        self._orig_db_path = tdb.TRANSCRIBE_DB_PATH
        tdb.TRANSCRIBE_DB_PATH = self.tmp_db.name

        # Tempdir for tickets and results, redirected on the
        # ingest module so it reads from our paths.
        self.work_dir = tempfile.mkdtemp()
        self.tickets_dir = os.path.join(self.work_dir, "columns")
        self.results_dir = os.path.join(self.work_dir, "results")
        os.makedirs(self.tickets_dir)
        os.makedirs(self.results_dir)
        self._orig_tickets = ing.WORK_TICKETS_DIR
        self._orig_results = ing.RESULTS_DIR
        ing.WORK_TICKETS_DIR = self.tickets_dir
        ing.RESULTS_DIR = self.results_dir

    def tearDown(self):
        tdb.TRANSCRIBE_DB_PATH = self._orig_db_path
        ing.WORK_TICKETS_DIR = self._orig_tickets
        ing.RESULTS_DIR = self._orig_results
        os.unlink(self.tmp_db.name)
        # Best-effort tempdir cleanup.
        for sub in (self.tickets_dir, self.results_dir, self.work_dir):
            try:
                for f in os.listdir(sub):
                    os.unlink(os.path.join(sub, f))
                os.rmdir(sub)
            except OSError:
                pass

    def _claim_and_ticket(self):
        conn = tdb.open_connection(self.tmp_db.name, attach_mvtm=False)
        try:
            row_id = tdb.claim_column(
                conn,
                year=1892, month=1, day=1, page=1, col_idx=0,
                image_path="columns/1892-01-01/p1/test_col1.png",
                image_sha256="deadbeef" * 8)
        finally:
            conn.close()

        ticket = {
            "row_id": row_id,
            "issue": {"year": 1892, "month": 1, "day": 1},
            "page": 1,
            "col_idx": 0,
            "image_path": "columns/1892-01-01/p1/test_col1.png",
            "image_sha256": "deadbeef" * 8,
            "prompt_hash": "test-prompt-hash",
            "agent_file_path": ".claude/agents/column-transcriber.md",
        }
        with open(os.path.join(
                self.tickets_dir, f"{row_id}.json"), "w") as f:
            json.dump(ticket, f)
        return row_id

    def _write_result(self, row_id: str, envelope: dict):
        with open(os.path.join(
                self.results_dir, f"{row_id}.json"), "w") as f:
            json.dump(envelope, f)

    def test_clean_ingest(self):
        row_id = self._claim_and_ticket()
        self._write_result(row_id, _good_envelope())

        report = ing.ingest(row_id, model="claude-sonnet-4-6")
        self.assertEqual(report["prior_status"], "claimed")
        self.assertEqual(report["model"], "claude-sonnet-4-6")
        self.assertIsNone(report["repair_id"])
        self.assertFalse(report["any_quality_flag"])

        conn = sqlite3.connect(self.tmp_db.name)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, transcript_text, model, prompt_hash, "
                "quality_flags, repair_needed "
                "FROM column_transcripts WHERE id=?",
                (row_id,)).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["transcript_text"],
                             "The quick brown fox.")
            self.assertEqual(row["model"], "claude-sonnet-4-6")
            self.assertEqual(row["prompt_hash"], "test-prompt-hash")
            self.assertEqual(row["repair_needed"], 0)
            flags = json.loads(row["quality_flags"])
            self.assertFalse(flags["damage"])

            # No repairs raised.
            n_repairs = conn.execute(
                "SELECT COUNT(*) FROM repairs").fetchone()[0]
            self.assertEqual(n_repairs, 0)
        finally:
            conn.close()

    def test_repair_branch_raises_row(self):
        row_id = self._claim_and_ticket()
        self._write_result(row_id, _good_envelope(repair=True))

        report = ing.ingest(row_id, model="claude-haiku-4-5")
        self.assertIsNotNone(report["repair_id"])

        conn = sqlite3.connect(self.tmp_db.name)
        conn.row_factory = sqlite3.Row
        try:
            r = conn.execute(
                "SELECT target_kind, target_ref_json, repair_kind, "
                "description, status, raised_by, related_column_id "
                "FROM repairs WHERE id=?",
                (report["repair_id"],)).fetchone()
            self.assertEqual(r["target_kind"], "column")
            self.assertEqual(r["status"], "open")
            self.assertEqual(r["raised_by"], "claude-haiku-4-5")
            self.assertEqual(r["related_column_id"], row_id)
            ref = json.loads(r["target_ref_json"])
            self.assertEqual(ref, {"year": 1892, "month": 1,
                                   "day": 1, "page": 1, "col_idx": 0})
            self.assertIn("next column", r["description"])
        finally:
            conn.close()

    def test_missing_result_file_is_clean_failure(self):
        row_id = self._claim_and_ticket()
        # No result file written.
        with self.assertRaises(FileNotFoundError):
            ing.ingest(row_id, model="claude-sonnet-4-6")

    def test_missing_ticket_is_clean_failure(self):
        # No ticket written.
        with self.assertRaises(FileNotFoundError):
            ing.ingest("00000000-0000-0000-0000-000000000000",
                       model="claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
