"""Tests for transcribe.slice.

Covers the rule-class threshold, the column-edge sentinels, the
sub-divide behaviour for tall slices, and the joiner's separator
logic. Uses a synthetic 100x800 PNG so no Phase A artefacts or
real column images are required.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from PIL import Image

import transcribe.slice as ts


def _write_test_png(path: str, width: int = 100, height: int = 800) -> None:
    """Write a flat white PNG at ``path``. Content is irrelevant — we
    only ever look at sizes and slice positions."""
    img = Image.new("L", (width, height), 255)
    img.save(path)


class ClassifyRuleClassTests(unittest.TestCase):

    def test_full_rule_at_threshold(self):
        # 0.85 ratio exactly → full
        rule = {"x1_pct": 0.0, "x2_pct": 8.5}
        self.assertEqual(ts._classify_rule_class(rule, 10.0), "full")

    def test_full_rule_above_threshold(self):
        rule = {"x1_pct": 0.0, "x2_pct": 9.5}
        self.assertEqual(ts._classify_rule_class(rule, 10.0), "full")

    def test_narrow_rule_below_threshold(self):
        # 0.5 ratio → narrow
        rule = {"x1_pct": 0.0, "x2_pct": 5.0}
        self.assertEqual(ts._classify_rule_class(rule, 10.0), "narrow")

    def test_zero_width_column_returns_narrow(self):
        rule = {"x1_pct": 0.0, "x2_pct": 5.0}
        self.assertEqual(ts._classify_rule_class(rule, 0.0), "narrow")


class BuildCutsTests(unittest.TestCase):

    def test_column_top_and_bottom_added(self):
        cuts = ts._build_cuts([], col_width_pct=10.0)
        self.assertEqual(len(cuts), 2)
        self.assertEqual(cuts[0]["rule_class"], "column_edge")
        self.assertEqual(cuts[-1]["rule_class"], "column_edge")
        self.assertEqual(cuts[0]["y_pct"], 0.0)
        self.assertEqual(cuts[-1]["y_pct"], 100.0)

    def test_rules_sorted_by_y(self):
        rules = [
            {"y_pct": 75.0, "x1_pct": 0, "x2_pct": 10},
            {"y_pct": 25.0, "x1_pct": 0, "x2_pct": 10},
            {"y_pct": 50.0, "x1_pct": 0, "x2_pct": 5},
        ]
        cuts = ts._build_cuts(rules, col_width_pct=10.0)
        self.assertEqual([c["y_pct"] for c in cuts],
                         [0.0, 25.0, 50.0, 75.0, 100.0])
        # Middle rule was narrow; the others full.
        self.assertEqual(cuts[1]["rule_class"], "full")
        self.assertEqual(cuts[2]["rule_class"], "narrow")
        self.assertEqual(cuts[3]["rule_class"], "full")


class SubdivideTests(unittest.TestCase):

    def test_short_span_not_subdivided(self):
        out = ts._subdivide(0, ts.MAX_SLICE_HEIGHT_PX)
        self.assertEqual(len(out), 1)
        sub_top, sub_bot, is_sub, sub_i = out[0]
        self.assertFalse(is_sub)
        self.assertEqual(sub_i, 0)
        self.assertEqual((sub_top, sub_bot), (0, ts.MAX_SLICE_HEIGHT_PX))

    def test_long_span_subdivided_with_overlap(self):
        # span = 2 * MAX, expect 2-3 sub-slices with SUB_OVERLAP_PX
        out = ts._subdivide(0, 2 * ts.MAX_SLICE_HEIGHT_PX)
        self.assertGreater(len(out), 1)
        # All sub-slices flagged
        self.assertTrue(all(rec[2] for rec in out))
        # Sub-indices increase from 0
        self.assertEqual([rec[3] for rec in out],
                         list(range(len(out))))
        # Consecutive sub-slices overlap by SUB_OVERLAP_PX (except
        # the last which clips to the span end).
        for prev, curr in zip(out, out[1:]):
            overlap = prev[1] - curr[0]
            # The only allowance is when the final slice clips short.
            if curr[1] < 2 * ts.MAX_SLICE_HEIGHT_PX:
                self.assertEqual(overlap, ts.SUB_OVERLAP_PX)


class SliceColumnTests(unittest.TestCase):

    def test_writes_slices_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            png_path = os.path.join(td, "col.png")
            _write_test_png(png_path, width=100, height=800)

            out_dir = os.path.join(td, "out")
            manifest = ts.slice_column(
                image_path=png_path,
                column_position={"left_pct": 0.0, "right_pct": 10.0,
                                 "width_pct": 10.0},
                h_rules=[
                    {"y_pct": 25.0, "x1_pct": 0.0, "x2_pct": 10.0,
                     "strength": 1.0},
                    {"y_pct": 50.0, "x1_pct": 0.0, "x2_pct": 5.0,
                     "strength": 1.0},
                    {"y_pct": 75.0, "x1_pct": 0.0, "x2_pct": 10.0,
                     "strength": 1.0},
                ],
                out_dir=out_dir,
                repo_root=td)

            self.assertEqual(len(manifest), 4)
            # Manifest file written.
            man_path = os.path.join(out_dir, "manifest.json")
            self.assertTrue(os.path.isfile(man_path))
            # Slice files written.
            for rec in manifest:
                self.assertTrue(os.path.isfile(
                    os.path.join(td, rec["image_path"])))
                # image_path is repo-relative
                self.assertFalse(os.path.isabs(rec["image_path"]))

            # Rule classes: edge / full / narrow / full / edge
            self.assertEqual(manifest[0]["top_rule_class"], "column_edge")
            self.assertEqual(manifest[0]["bottom_rule_class"], "full")
            self.assertEqual(manifest[1]["top_rule_class"], "full")
            self.assertEqual(manifest[1]["bottom_rule_class"], "narrow")
            self.assertEqual(manifest[2]["top_rule_class"], "narrow")
            self.assertEqual(manifest[2]["bottom_rule_class"], "full")
            self.assertEqual(manifest[3]["bottom_rule_class"], "column_edge")

    def test_overlap_clamps_at_image_edges(self):
        with tempfile.TemporaryDirectory() as td:
            png_path = os.path.join(td, "col.png")
            _write_test_png(png_path, width=100, height=400)

            out_dir = os.path.join(td, "out")
            manifest = ts.slice_column(
                image_path=png_path,
                column_position={"left_pct": 0.0, "right_pct": 10.0,
                                 "width_pct": 10.0},
                h_rules=[],
                out_dir=out_dir,
                repo_root=td)

            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["y_top_px"], 0)
            self.assertEqual(manifest[0]["y_bottom_px"], 400)


class JoinSliceTranscriptsTests(unittest.TestCase):

    def _rec(self, idx, top_class, bot_class, *, subdivided=False, sub_idx=0):
        return {"idx": idx, "top_rule_class": top_class,
                "bottom_rule_class": bot_class,
                "subdivided": subdivided, "sub_idx": sub_idx,
                "y_top_pct": 0.0, "y_bottom_pct": 0.0,
                "y_top_px": 0, "y_bottom_px": 0, "height_px": 0,
                "image_path": f"slice{idx:02d}.png",
                "top_rule_y_pct": None, "bottom_rule_y_pct": None}

    def test_full_rules_join_with_hr(self):
        recs = [
            self._rec(0, "column_edge", "full"),
            self._rec(1, "full", "column_edge"),
        ]
        joined, bnd = ts.join_slice_transcripts(recs, ["A", "B"])
        self.assertEqual(joined, "A\n\n---\n\nB")
        self.assertEqual(bnd[0]["char_offset_start"], 0)
        self.assertEqual(bnd[0]["char_offset_end"], 1)
        self.assertEqual(bnd[1]["char_offset_start"],
                         len("A\n\n---\n\n"))

    def test_narrow_rules_join_with_double_dash(self):
        recs = [
            self._rec(0, "column_edge", "narrow"),
            self._rec(1, "narrow", "column_edge"),
        ]
        joined, _ = ts.join_slice_transcripts(recs, ["A", "B"])
        self.assertEqual(joined, "A\n\n--\n\nB")

    def test_sub_slice_continuation_uses_blank_line(self):
        recs = [
            self._rec(0, "column_edge", "column_edge",
                      subdivided=True, sub_idx=0),
            self._rec(1, "column_edge", "column_edge",
                      subdivided=True, sub_idx=1),
        ]
        joined, _ = ts.join_slice_transcripts(recs, ["A", "B"])
        self.assertEqual(joined, "A\nB")

    def test_length_mismatch_raises(self):
        recs = [self._rec(0, "column_edge", "column_edge")]
        with self.assertRaises(ValueError):
            ts.join_slice_transcripts(recs, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
