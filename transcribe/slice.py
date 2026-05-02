"""H-rule-bounded column slicing for pass-1A.

Phase A established that the Read tool downsamples large images
(a 911 × 8313 column rendered at ~250px wide), making body text
unreadable while headlines stay visible. The model can then
fabricate plausible body text from the headline because what it
"sees" doesn't include the actual prose. The structural fix is
to cut the column into smaller pieces before handing it to the
agent, so each piece displays at native resolution.

This module is the slicer. It is pure: inputs are an image path,
the column's pct extents, the upstream-detected h-rules and
some thresholds; outputs are slice PNGs on disk plus a manifest
list. No DB, no agent, no LLM.

Cuts happen at the upstream h-rules (treated as item separators)
plus the column-top and column-bottom edges. Each slice carries a
small overlap on top and bottom so a slightly-misplaced rule still
leaves the item whole. Slices taller than ``MAX_SLICE_HEIGHT_PX``
are sub-divided with a fixed-pixel chunk + overlap pattern; the
sub-slices are tagged so the joiner doesn't insert a rule marker
between them.

H-rules are classified as "full" (rule width / column width ≥
``FULL_WIDTH_RATIO``) or "narrow". The classification feeds the
joiner: full rules join with markdown HR ``---`` (downstream
segmentation treats these as item boundaries); narrow rules join
with ``--`` which markdown does *not* render as HR (item-internal
section break, e.g. heading-rule-body).

Usage::

    from transcribe.slice import slice_column

    slices = slice_column(
        image_path="<repo>/columns/1892-01-01/p1/p1_col0.png",
        column_position={"left_pct": 0.0, "right_pct": 13.49,
                         "width_pct": 13.49},
        h_rules=[{"y_pct": 6.74, "x1_pct": 0.0,
                  "x2_pct": 13.34, "strength": 0.95}, ...],
        out_dir="<repo>/transcribe/work/slices/<row-id>")

The ``out_dir`` is created if it doesn't exist; existing slice
files in it are overwritten on each call (the slice file names
are deterministic in slice index, so re-running on the same
inputs produces the same files).
"""

from __future__ import annotations

import json
import os
from typing import Any

from PIL import Image

import coordinates as _coords


# Thresholds. Tunable; live here next to the slicing logic so a
# change is visible in one place. The defaults are the values
# Phase A validated on 1892-01-01 col0.
OVERLAP_PX = 20
MAX_SLICE_HEIGHT_PX = 2500
SUB_OVERLAP_PX = 100
FULL_WIDTH_RATIO = 0.85


def _classify_rule_class(rule: dict, col_width_pct: float) -> str:
    """Return ``'full'`` or ``'narrow'`` for an h-rule.

    A full-width rule extends across most of the column and acts as
    an item separator. A narrow rule (typically half-width or less)
    is an item-internal divider — heading-rule-body, ad sub-section
    breaks, decorative dingbats. The threshold is set such that
    rules whose width is ≥ ``FULL_WIDTH_RATIO`` of the column's
    width count as full.

    The h-rule x-extents are page-percentages from the upstream
    detector, so dividing by the column's width-pct yields a
    dimensionless ratio (no need to convert to pixels).
    """
    width_pct = rule["x2_pct"] - rule["x1_pct"]
    if col_width_pct <= 0:
        return "narrow"
    return "full" if (width_pct / col_width_pct) >= FULL_WIDTH_RATIO \
        else "narrow"


def _build_cuts(h_rules: list[dict],
                col_width_pct: float) -> list[dict]:
    """Return the ordered list of cut points: column-top, every
    h-rule, column-bottom. Each cut carries its y-percent and a
    rule-class label.

    The column-top and column-bottom cuts are tagged
    ``column_edge`` (not full / not narrow) so the joiner knows
    not to emit a rule marker before the first slice or after the
    last one.
    """
    cuts: list[dict] = [
        {"y_pct": 0.0, "rule": None, "rule_class": "column_edge"}]
    for r in sorted(h_rules, key=lambda x: x["y_pct"]):
        cuts.append({
            "y_pct": r["y_pct"],
            "rule": r,
            "rule_class": _classify_rule_class(r, col_width_pct),
        })
    cuts.append(
        {"y_pct": 100.0, "rule": None, "rule_class": "column_edge"})
    return cuts


def _subdivide(top_px: int,
               bot_px: int) -> list[tuple[int, int, bool, int]]:
    """Split a vertical span into sub-slices ≤ ``MAX_SLICE_HEIGHT_PX``.

    Returns a list of ``(sub_top, sub_bot, is_subdivided, sub_idx)``
    tuples. If the span fits in a single slice, returns one tuple
    with ``is_subdivided=False``. Otherwise, walks the span with a
    step of ``MAX_SLICE_HEIGHT_PX - SUB_OVERLAP_PX`` so consecutive
    sub-slices overlap by ``SUB_OVERLAP_PX``.
    """
    height = bot_px - top_px
    if height <= MAX_SLICE_HEIGHT_PX:
        return [(top_px, bot_px, False, 0)]

    step = MAX_SLICE_HEIGHT_PX - SUB_OVERLAP_PX
    out: list[tuple[int, int, bool, int]] = []
    cursor = top_px
    sub_i = 0
    while cursor < bot_px:
        sub_top = cursor
        sub_bot = min(bot_px, cursor + MAX_SLICE_HEIGHT_PX)
        out.append((sub_top, sub_bot, True, sub_i))
        if sub_bot >= bot_px:
            break
        cursor += step
        sub_i += 1
    return out


def slice_column(*,
                 image_path: str,
                 column_position: dict,
                 h_rules: list[dict],
                 out_dir: str,
                 repo_root: str | None = None) -> list[dict]:
    """Slice a column PNG at h-rules, write slice files, return manifest.

    Args:
      image_path:       absolute path to the column PNG.
      column_position:  ``{"left_pct", "right_pct", "width_pct"}`` —
                        the pct extents from the column ticket.
      h_rules:          list of ``{"y_pct", "x1_pct", "x2_pct",
                        "strength"}`` for h-rules in this column.
      out_dir:          directory to write slice PNGs into. Created
                        if missing. Existing files are overwritten.
      repo_root:        if provided, the manifest's ``image_path``
                        fields are stored as repo-relative paths.
                        Otherwise they are stored as absolute paths.

    Returns:
      A list of slice records, one per slice. Each record has::

        {
          "idx": int,
          "y_top_pct": float, "y_bottom_pct": float,
          "y_top_px":  int,   "y_bottom_px":  int,
          "height_px": int,
          "image_path": str,         # repo-relative path to slice PNG
          "top_rule_class":    "full" | "narrow" | "column_edge",
          "bottom_rule_class": "full" | "narrow" | "column_edge",
          "top_rule_y_pct":    float | None,
          "bottom_rule_y_pct": float | None,
          "subdivided": bool,
          "sub_idx":    int,         # 0 unless subdivided
        }

      The manifest is also written to ``out_dir/manifest.json``.
    """
    img = Image.open(image_path)
    img_w, img_h = img.size
    col_width_pct = column_position["right_pct"] - column_position["left_pct"]
    cuts = _build_cuts(h_rules, col_width_pct)

    os.makedirs(out_dir, exist_ok=True)

    manifest: list[dict] = []
    slice_idx = 0

    for i in range(len(cuts) - 1):
        top_pct = cuts[i]["y_pct"]
        bot_pct = cuts[i + 1]["y_pct"]

        # pct → px against image height, then pad with overlap.
        # Clamp to the image extents so the topmost / bottommost
        # slices don't try to crop above 0 or below img_h.
        top_px = _coords.clamp_px(
            _coords.pct_to_px(top_pct, img_h) - OVERLAP_PX, img_h)
        bot_px = _coords.clamp_px(
            _coords.pct_to_px(bot_pct, img_h) + OVERLAP_PX, img_h)
        if bot_px <= top_px:
            continue

        for sub_top, sub_bot, is_sub, sub_i in _subdivide(top_px, bot_px):
            crop = img.crop((0, sub_top, img_w, sub_bot))
            fname = f"slice{slice_idx:02d}.png"
            fpath = os.path.join(out_dir, fname)
            crop.save(fpath)

            if repo_root is not None:
                manifest_image_path = os.path.relpath(fpath, repo_root)
            else:
                manifest_image_path = fpath

            manifest.append({
                "idx": slice_idx,
                "y_top_pct": _coords.px_to_pct(sub_top, img_h),
                "y_bottom_pct": _coords.px_to_pct(sub_bot, img_h),
                "y_top_px": sub_top,
                "y_bottom_px": sub_bot,
                "height_px": sub_bot - sub_top,
                "image_path": manifest_image_path,
                "top_rule_class": cuts[i]["rule_class"],
                "bottom_rule_class": cuts[i + 1]["rule_class"],
                "top_rule_y_pct": cuts[i]["y_pct"]
                                  if cuts[i]["rule"] is not None else None,
                "bottom_rule_y_pct": cuts[i + 1]["y_pct"]
                                  if cuts[i + 1]["rule"] is not None else None,
                "subdivided": is_sub,
                "sub_idx": sub_i,
            })
            slice_idx += 1

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest


def join_slice_transcripts(slice_records: list[dict],
                           per_slice_text: list[str]) -> tuple[str, list[dict]]:
    """Join per-slice transcripts into one column transcript.

    Inserts a markdown rule between consecutive slices based on the
    rule class of the boundary between them:

    - ``full`` rule  → ``\\n\\n---\\n\\n`` (markdown HR; item separator)
    - ``narrow`` rule → ``\\n\\n--\\n\\n`` (not a markdown HR; item-internal)
    - sub-slice continuation (no rule between) → ``\\n``

    Returns:
      ``(joined_text, slice_boundaries)`` where
      ``slice_boundaries`` is the manifest enriched with per-slice
      ``char_offset_start`` and ``char_offset_end`` into
      ``joined_text``. This is the JSON written to
      ``column_transcripts.slice_boundaries``.

    The first slice never has a rule marker before it. The
    ``column_edge`` rule class is also treated as "no marker"
    because column edges aren't h-rules — they're the ends of the
    column, and there's nothing on the other side to separate.
    """
    if len(slice_records) != len(per_slice_text):
        raise ValueError(
            f"slice_records ({len(slice_records)}) and per_slice_text "
            f"({len(per_slice_text)}) must be the same length")

    parts: list[str] = []
    boundaries: list[dict] = []
    cursor = 0

    for i, (rec, text) in enumerate(zip(slice_records, per_slice_text)):
        if i > 0:
            prev = slice_records[i - 1]
            # Same h-rule-bounded item, sub-divisions: no rule.
            if prev["subdivided"] and rec["subdivided"]:
                sep = "\n"
            else:
                rule_class = rec["top_rule_class"]
                if rule_class == "full":
                    sep = "\n\n---\n\n"
                elif rule_class == "narrow":
                    sep = "\n\n--\n\n"
                else:  # column_edge — shouldn't happen mid-list, but safe
                    sep = "\n\n"
            parts.append(sep)
            cursor += len(sep)

        start = cursor
        parts.append(text)
        cursor += len(text)
        end = cursor

        boundaries.append({
            **rec,
            "char_offset_start": start,
            "char_offset_end": end,
        })

    return "".join(parts), boundaries


__all__ = [
    "OVERLAP_PX",
    "MAX_SLICE_HEIGHT_PX",
    "SUB_OVERLAP_PX",
    "FULL_WIDTH_RATIO",
    "slice_column",
    "join_slice_transcripts",
]
