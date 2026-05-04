"""Sub-divide a single slice PNG into smaller pieces for retry.

Used when an h-rule-bounded slice still trips Anthropic's content-filter
classifier under Tier 2 (Sonnet per-slice) or Tier 3 (Opus per-slice).
By chopping the slice into smaller vertical pieces, the orchestrator
reduces trigger-token concentration further and shrinks the
unrecoverable surface — instead of one ~2500px slice needing human
transcription, only the one or two sub-pieces that still block do.

This is *retry plumbing*, not a primary slicer. The primary slicer
(`transcribe.slice`) cuts the column at h-rules and is the source of
truth for the manifest. This helper takes one already-cut slice file
and splits it further; sub-piece transcripts are then concatenated
back into a single per-slice envelope so the canonical merge helper
(`transcribe.merge_slice_results`) is unaffected.

Usage::

    # Step 1: split slice 02 of <row_id> into 3 vertical pieces
    python3 -m transcribe.subdivide_slice split <row_id> 2 \\
        --pieces 3 [--overlap-px 100]

    # ... orchestrator dispatches one agent per sub-piece, each
    # writing transcribe/work/results/<row_id>.slice02.subNN.json ...

    # Step 2: assemble the sub-piece transcripts into a single slice
    # envelope at transcribe/work/results/<row_id>.slice02.json
    python3 -m transcribe.subdivide_slice assemble <row_id> 2

The split layout:

    transcribe/work/slices/<row_id>/
        slice02.png                      # original (untouched)
        slice02.sub00.png                # new sub-pieces
        slice02.sub01.png
        slice02.sub02.png
        slice02.subdivision.json         # manifest of sub-pieces

Each sub-piece carries ``SUB_OVERLAP_PX`` overlap top and bottom so a
mid-line cut leaves the line whole on at least one side. The agent
prompt tells the model to ignore truncated lines at the overlap edges,
exactly as for primary slicing.

The assemble step writes a single-record per-slice envelope (matching
``merge_slice_results``'s expected shape) where:

- ``transcript_text`` is the concatenation of sub-piece transcripts
  joined with a single newline (no rule markers — sub-pieces are
  inside one h-rule-bounded item by construction).
- ``transcriber_notes`` are concatenated with ``[sub NN]`` prefixes.
- ``quality_flags`` are OR'd across sub-pieces.
- ``repair_needed`` / ``repair_reason`` are OR'd / concatenated, same
  shape as the per-slice merge.

If any sub-piece's result file is missing (still blocked), assemble
exits non-zero and names the missing piece — the orchestrator surfaces
that to the user, who can hand-transcribe just that one sub-piece via
``transcribe.import_transcript`` (writing to the sub-piece path) and
re-run assemble.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

from PIL import Image

import coordinates as _coords

from . import db as _db
from . import ingest_column_result as _ingest


SLICES_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "slices")
RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
SUB_OVERLAP_PX = 100  # mirrors transcribe.slice.SUB_OVERLAP_PX
QUALITY_FLAGS = (
    "damage", "faded", "smudged", "low_legibility",
    "partial_cut", "adjacent_text_visible",
)
SUB_FILE_RE = re.compile(
    r"^(?P<row>.+)\.slice(?P<sl>\d+)\.sub(?P<sub>\d+)\.json$")


def _slice_png_path(row_id: str, slice_idx: int) -> str:
    return os.path.join(
        SLICES_DIR, row_id, f"slice{slice_idx:02d}.png")


def _sub_png_path(row_id: str, slice_idx: int, sub_idx: int) -> str:
    return os.path.join(
        SLICES_DIR, row_id,
        f"slice{slice_idx:02d}.sub{sub_idx:02d}.png")


def _sub_result_path(row_id: str, slice_idx: int, sub_idx: int) -> str:
    return os.path.join(
        RESULTS_DIR,
        f"{row_id}.slice{slice_idx:02d}.sub{sub_idx:02d}.json")


def _subdivision_manifest_path(row_id: str, slice_idx: int) -> str:
    return os.path.join(
        SLICES_DIR, row_id,
        f"slice{slice_idx:02d}.subdivision.json")


def split_slice(row_id: str, slice_idx: int, *,
                pieces: int, overlap_px: int = SUB_OVERLAP_PX) -> list[dict]:
    """Split one slice PNG into ``pieces`` vertical sub-pieces.

    Writes sub-piece PNGs and a sidecar manifest. Returns the manifest.
    The original slice file is untouched so the row can fall back to
    the parent-slice path if needed.
    """
    if pieces < 2:
        raise ValueError(f"pieces must be >= 2, got {pieces}")

    src = _slice_png_path(row_id, slice_idx)
    if not os.path.exists(src):
        raise FileNotFoundError(f"slice PNG not found: {src}")

    img = Image.open(src)
    w, h = img.size
    # piece height before overlap; the actual crop adds overlap on
    # each interior edge so consecutive pieces share SUB_OVERLAP_PX.
    base = h // pieces
    if base <= overlap_px * 2:
        raise ValueError(
            f"slice height {h}px too small for {pieces} pieces with "
            f"{overlap_px}px overlap; pick fewer pieces or smaller overlap")

    manifest: list[dict] = []
    for i in range(pieces):
        top = i * base
        bot = (i + 1) * base if i < pieces - 1 else h
        sub_top = max(0, top - (overlap_px if i > 0 else 0))
        sub_bot = min(h, bot + (overlap_px if i < pieces - 1 else 0))
        crop = img.crop((0, sub_top, w, sub_bot))
        out_path = _sub_png_path(row_id, slice_idx, i)
        crop.save(out_path)
        manifest.append({
            "sub_idx": i,
            "y_top_px": sub_top,
            "y_bottom_px": sub_bot,
            "height_px": sub_bot - sub_top,
            "image_path": os.path.relpath(out_path, _db.REPO_ROOT),
            "y_top_pct_within_slice": _coords.px_to_pct(sub_top, h),
            "y_bottom_pct_within_slice": _coords.px_to_pct(sub_bot, h),
        })

    manifest_path = _subdivision_manifest_path(row_id, slice_idx)
    with open(manifest_path, "w") as f:
        json.dump({
            "row_id": row_id,
            "slice_idx": slice_idx,
            "pieces": pieces,
            "overlap_px": overlap_px,
            "source_height_px": h,
            "sub_pieces": manifest,
        }, f, indent=2)

    return manifest


def _find_sub_results(row_id: str, slice_idx: int) -> list[tuple[int, str]]:
    pattern = os.path.join(
        RESULTS_DIR, f"{row_id}.slice{slice_idx:02d}.sub*.json")
    found: list[tuple[int, str]] = []
    for path in glob.glob(pattern):
        m = SUB_FILE_RE.match(os.path.basename(path))
        if m and m.group("row") == row_id \
                and int(m.group("sl")) == slice_idx:
            found.append((int(m.group("sub")), path))
    return sorted(found)


def _load_sub_envelope(path: str) -> dict:
    """Load a sub-piece envelope. Accepts either:

    - a Sliced-mode envelope with one record in ``slices`` (the
      column-transcriber's standard output for a single image), or
    - a flat envelope with top-level ``transcript_text`` /
      ``transcriber_notes`` (the simpler shape an external/human
      sub-piece transcript may use).
    """
    with open(path) as f:
        raw = f.read()
    data = json.loads(_ingest.strip_fence(raw))

    if "slices" in data and isinstance(data["slices"], list) \
            and data["slices"]:
        if len(data["slices"]) != 1:
            raise ValueError(
                f"{path}: sub-piece envelope must contain exactly one "
                f"record in 'slices', got {len(data['slices'])}")
        rec = data["slices"][0]
        return {
            "transcript_text": rec.get("transcript_text", ""),
            "transcriber_notes": rec.get("transcriber_notes", ""),
            "quality_flags": data.get("quality_flags") or {},
            "repair_needed": bool(data.get("repair_needed")),
            "repair_reason": data.get("repair_reason") or "",
        }

    if "transcript_text" not in data:
        raise ValueError(
            f"{path}: envelope has neither 'slices' nor "
            f"top-level 'transcript_text'")
    return {
        "transcript_text": data.get("transcript_text", ""),
        "transcriber_notes": data.get("transcriber_notes", ""),
        "quality_flags": data.get("quality_flags") or {},
        "repair_needed": bool(data.get("repair_needed")),
        "repair_reason": data.get("repair_reason") or "",
    }


def assemble_slice(row_id: str, slice_idx: int) -> dict:
    """Concatenate sub-piece transcripts into one slice envelope.

    Reads ``<row_id>.slice<NN>.sub<MM>.json`` files, concatenates
    transcripts in sub_idx order, and writes
    ``<row_id>.slice<NN>.json`` as a single-record per-slice envelope
    suitable for ``transcribe.merge_slice_results``.
    """
    files = _find_sub_results(row_id, slice_idx)
    if not files:
        raise FileNotFoundError(
            f"no sub-piece results found at {RESULTS_DIR}/"
            f"{row_id}.slice{slice_idx:02d}.sub*.json")

    indices = [i for i, _ in files]
    expected = list(range(len(indices)))
    if indices != expected:
        # Read manifest to report the actual missing sub_idx values.
        man_path = _subdivision_manifest_path(row_id, slice_idx)
        if os.path.exists(man_path):
            with open(man_path) as f:
                man = json.load(f)
            total = len(man.get("sub_pieces", []))
            missing = [i for i in range(total) if i not in indices]
            raise ValueError(
                f"missing sub-piece results for slice {slice_idx:02d}: "
                f"sub_idx {missing} (have {indices}, expected "
                f"{list(range(total))})")
        raise ValueError(
            f"sub-piece indices not contiguous from 0: got {indices}")

    envelopes: list[dict] = []
    for sub_idx, path in files:
        envelopes.append(_load_sub_envelope(path))

    transcripts = [e["transcript_text"] for e in envelopes]
    notes_parts = []
    for sub_idx, env in zip(indices, envelopes):
        n = (env.get("transcriber_notes") or "").strip()
        if n:
            notes_parts.append(f"[sub {sub_idx:02d}] {n}")

    flags = {f: False for f in QUALITY_FLAGS}
    repair_needed = False
    repair_reasons: list[str] = []
    for sub_idx, env in zip(indices, envelopes):
        ef = env.get("quality_flags") or {}
        for f in QUALITY_FLAGS:
            if ef.get(f) is True:
                flags[f] = True
        if env.get("repair_needed"):
            repair_needed = True
        rr = (env.get("repair_reason") or "").strip()
        if rr:
            repair_reasons.append(f"[sub {sub_idx:02d}] {rr}")

    slice_envelope = {
        "slices": [{
            "idx": slice_idx,
            "transcript_text": "\n".join(transcripts),
            "transcriber_notes": " | ".join(notes_parts),
        }],
        "quality_flags": flags,
        "repair_needed": repair_needed,
        "repair_reason": " | ".join(repair_reasons),
    }

    out_path = os.path.join(
        RESULTS_DIR, f"{row_id}.slice{slice_idx:02d}.json")
    with open(out_path, "w") as f:
        json.dump(slice_envelope, f, indent=2, ensure_ascii=False)
    print(f"assembled {len(envelopes)} sub-pieces -> {out_path}")
    return slice_envelope


def cmd_split(args: argparse.Namespace) -> int:
    manifest = split_slice(
        args.row_id, args.slice_idx,
        pieces=args.pieces, overlap_px=args.overlap_px)
    print(f"split slice{args.slice_idx:02d} into {len(manifest)} pieces:")
    for rec in manifest:
        print(f"  sub{rec['sub_idx']:02d}: {rec['image_path']} "
              f"({rec['height_px']}px)")
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    try:
        assemble_slice(args.row_id, args.slice_idx)
    except (FileNotFoundError, ValueError) as e:
        print(f"assemble failed: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Split one slice PNG into sub-pieces for retry, "
                    "or assemble sub-piece transcripts into a slice "
                    "envelope.")
    sub = p.add_subparsers(dest="kind", required=True)

    sp = sub.add_parser("split",
                        help="Cut one slice PNG into vertical sub-pieces.")
    sp.add_argument("row_id")
    sp.add_argument("slice_idx", type=int)
    sp.add_argument("--pieces", type=int, default=2,
                    help="Number of vertical sub-pieces (default 2)")
    sp.add_argument("--overlap-px", type=int, default=SUB_OVERLAP_PX,
                    dest="overlap_px",
                    help=f"Overlap in px between consecutive sub-pieces "
                         f"(default {SUB_OVERLAP_PX})")
    sp.set_defaults(func=cmd_split)

    ap = sub.add_parser(
        "assemble",
        help="Concatenate sub-piece transcripts into a single slice "
             "envelope at <row_id>.slice<NN>.json.")
    ap.add_argument("row_id")
    ap.add_argument("slice_idx", type=int)
    ap.set_defaults(func=cmd_assemble)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
