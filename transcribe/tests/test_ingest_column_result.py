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


def _good_sliced_envelope(*, repair=False) -> dict:
    """Three-slice envelope: column-edge / full / full / column-edge.
    Joiner inserts '---' between slices."""
    return {
        "slices": [
            {"idx": 0,
             "transcript_text": "FIRST",
             "transcriber_notes": ""},
            {"idx": 1,
             "transcript_text": "MIDDLE BODY",
             "transcriber_notes": "saw a manicule"},
            {"idx": 2,
             "transcript_text": "LAST",
             "transcriber_notes": ""},
        ],
        "quality_flags": {
            "damage": False,
            "faded": False,
            "smudged": False,
            "low_legibility": False,
            "partial_cut": False,
            "adjacent_text_visible": False,
        },
        "repair_needed": repair,
        "repair_reason": "ad mask not applied" if repair else "",
    }


def _three_slice_manifest() -> list[dict]:
    """Manifest matching the shape produced by transcribe.slice for a
    column with 2 full-width h-rules (3 slices)."""
    return [
        {"idx": 0, "y_top_pct": 0.0, "y_bottom_pct": 33.0,
         "y_top_px": 0, "y_bottom_px": 264, "height_px": 264,
         "image_path": "transcribe/work/slices/x/slice00.png",
         "top_rule_class": "column_edge", "bottom_rule_class": "full",
         "top_rule_y_pct": None, "bottom_rule_y_pct": 33.0,
         "subdivided": False, "sub_idx": 0},
        {"idx": 1, "y_top_pct": 33.0, "y_bottom_pct": 67.0,
         "y_top_px": 264, "y_bottom_px": 536, "height_px": 272,
         "image_path": "transcribe/work/slices/x/slice01.png",
         "top_rule_class": "full", "bottom_rule_class": "full",
         "top_rule_y_pct": 33.0, "bottom_rule_y_pct": 67.0,
         "subdivided": False, "sub_idx": 0},
        {"idx": 2, "y_top_pct": 67.0, "y_bottom_pct": 100.0,
         "y_top_px": 536, "y_bottom_px": 800, "height_px": 264,
         "image_path": "transcribe/work/slices/x/slice02.png",
         "top_rule_class": "full", "bottom_rule_class": "column_edge",
         "top_rule_y_pct": 67.0, "bottom_rule_y_pct": None,
         "subdivided": False, "sub_idx": 0},
    ]


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

    def test_accepts_sliced_envelope(self):
        out = ing.parse_envelope(json.dumps(_good_sliced_envelope()))
        self.assertEqual(len(out["slices"]), 3)
        self.assertEqual(out["slices"][0]["transcript_text"], "FIRST")

    def test_rejects_sliced_envelope_missing_idx(self):
        env = _good_sliced_envelope()
        del env["slices"][1]["idx"]
        with self.assertRaises(ValueError) as ctx:
            ing.parse_envelope(json.dumps(env))
        self.assertIn("idx", str(ctx.exception))

    def test_rejects_sliced_envelope_missing_transcript(self):
        env = _good_sliced_envelope()
        del env["slices"][1]["transcript_text"]
        with self.assertRaises(ValueError):
            ing.parse_envelope(json.dumps(env))

    def test_rejects_empty_slices_list(self):
        env = _good_sliced_envelope()
        env["slices"] = []
        with self.assertRaises(ValueError):
            ing.parse_envelope(json.dumps(env))


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

    def _claim_and_ticket(self, *, slices: list | None = None):
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
        if slices is not None:
            ticket["slices"] = slices
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

    def test_sliced_ingest_joins_and_persists_boundaries(self):
        manifest = _three_slice_manifest()
        row_id = self._claim_and_ticket(slices=manifest)
        self._write_result(row_id, _good_sliced_envelope())

        report = ing.ingest(row_id, model="claude-sonnet-4-6")
        self.assertTrue(report["sliced"])
        self.assertEqual(report["num_slices"], 3)

        conn = sqlite3.connect(self.tmp_db.name)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT status, transcript_text, transcriber_notes, "
                "slice_boundaries "
                "FROM column_transcripts WHERE id=?",
                (row_id,)).fetchone()
            self.assertEqual(row["status"], "done")
            # Joined: 'FIRST\n\n---\n\nMIDDLE BODY\n\n---\n\nLAST'
            self.assertEqual(
                row["transcript_text"],
                "FIRST\n\n---\n\nMIDDLE BODY\n\n---\n\nLAST")
            # Notes from slice 1 only (others are empty).
            self.assertIn("[slice 01]", row["transcriber_notes"])
            self.assertIn("manicule", row["transcriber_notes"])
            # Boundaries persisted as JSON with char offsets.
            bnd = json.loads(row["slice_boundaries"])
            self.assertEqual(len(bnd), 3)
            self.assertEqual(bnd[0]["char_offset_start"], 0)
            self.assertEqual(bnd[0]["char_offset_end"], 5)  # 'FIRST'
            # Second slice starts after 'FIRST\n\n---\n\n' (12 chars).
            self.assertEqual(bnd[1]["char_offset_start"], 12)
        finally:
            conn.close()

    def test_sliced_envelope_without_manifest_raises(self):
        # Ticket lacks 'slices' but envelope is sliced — error.
        row_id = self._claim_and_ticket()  # no slices=
        self._write_result(row_id, _good_sliced_envelope())

        with self.assertRaises(ValueError) as ctx:
            ing.ingest(row_id, model="claude-sonnet-4-6")
        self.assertIn("slice", str(ctx.exception).lower())

    def test_sliced_idx_mismatch_raises(self):
        manifest = _three_slice_manifest()
        row_id = self._claim_and_ticket(slices=manifest)
        env = _good_sliced_envelope()
        # Drop one slice.
        env["slices"].pop()
        self._write_result(row_id, env)
        with self.assertRaises(ValueError) as ctx:
            ing.ingest(row_id, model="claude-sonnet-4-6")
        self.assertIn("idx mismatch", str(ctx.exception))

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
