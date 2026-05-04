"""Import an externally-sourced transcript into the transcription pipeline.

The standard path is ``column-transcriber`` agent → JSON envelope →
``ingest_column_result``. This module provides a parallel route that
bypasses the agent step and lands a transcript supplied from outside
the LLM pipeline. Use it when:

- The LLM tiers all blocked (Tier 1 Sonnet per-column → Tier 2 Sonnet
  per-slice → Tier 3 Opus per-slice; see SKILL.md). A human reads the
  PNG and types the transcript.
- An external OCR pipeline (Tesseract, Google Vision, custom) is used
  to seed transcripts that the LLM later reviews.
- A prior transcript is wrong and a corrected version replaces it.

Two modes mirror the LLM dispatch layout:

- ``slice <row_id> <slice_idx>`` writes a per-slice envelope to
  ``transcribe/work/results/<row_id>.slice<NN>.json``. The merge
  helper (``transcribe.merge_slice_results``) then assembles the full
  column from the available per-slice files and runs the column
  ingester. Use this when only some slices need replacement; the
  merge picks up agent-written slices alongside the human-written one.
- ``subslice <row_id> <slice_idx> <sub_idx>`` writes a per-sub-piece
  envelope to
  ``transcribe/work/results/<row_id>.slice<NN>.sub<MM>.json``. Used
  in the Tier-4 retry path: a single sub-piece of one slice was the
  only thing the LLM tiers couldn't transcribe, and the rest of the
  slice's sub-pieces have agent-written results on disk. After this
  writes, run ``transcribe.subdivide_slice assemble`` to concatenate
  the sub-pieces into a slice envelope, then
  ``transcribe.merge_slice_results`` to ingest the column.
- ``column <row_id>`` writes a full-image-mode envelope to
  ``transcribe/work/results/<row_id>.json``. The ingester treats it
  as a single transcript; ``slice_boundaries`` will be NULL for the
  row (no per-slice provenance). Use this when the whole column was
  transcribed externally and per-slice splitting isn't useful.

Sources are arbitrary short strings and recorded verbatim in
``transcriber_notes`` with a ``[source: ...]`` prefix. Common values:
``human``, ``ocr-tesseract``, ``ocr-googlevision``, ``correction``.
The pipeline doesn't enumerate them — keep the value short and
descriptive enough that a future reviewer can recognise it.

Usage::

  python3 -m transcribe.import_transcript slice <row_id> <slice_idx> \\
      --text-file path/to/text.txt [--source human] [--notes "..."] \\
      [--damage] [--faded] [--smudged] [--low-legibility] \\
      [--partial-cut] [--adjacent-text-visible] \\
      [--repair-needed --repair-reason "..."] \\
      [--no-merge]

  python3 -m transcribe.import_transcript subslice <row_id> <slice_idx> <sub_idx> \\
      --text-file path/to/text.txt [--source human] [--notes "..."] \\
      [flags...] [--no-assemble]

  python3 -m transcribe.import_transcript column <row_id> \\
      --text-file path/to/text.txt [--source human] [--notes "..."] \\
      [flags...] [--no-ingest]

Pass ``--text-file -`` to read transcript text from stdin.

The row's status is updated to ``done`` exactly as if the LLM path had
landed it; if the row was already ``done`` the prior transcript is
overwritten in place (the ingester reports the prior status). A line
is appended to ``transcribe/work/experiments.jsonl`` recording the
import for audit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from . import db as _db


RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
EXPERIMENTS_LOG = os.path.join(
    _db.REPO_ROOT, "transcribe", "work", "experiments.jsonl")
QUALITY_FLAGS = (
    "damage", "faded", "smudged", "low_legibility",
    "partial_cut", "adjacent_text_visible",
)


def read_text(text_file: str) -> str:
    if text_file == "-":
        return sys.stdin.read()
    with open(text_file) as f:
        return f.read()


def build_notes(source: str, user_notes: str) -> str:
    parts = [f"[source: {source}]"]
    if user_notes:
        parts.append(user_notes)
    return " ".join(parts)


def build_quality_flags(args: argparse.Namespace) -> dict:
    return {
        "damage":                getattr(args, "damage", False),
        "faded":                 getattr(args, "faded", False),
        "smudged":               getattr(args, "smudged", False),
        "low_legibility":        getattr(args, "low_legibility", False),
        "partial_cut":           getattr(args, "partial_cut", False),
        "adjacent_text_visible": getattr(args, "adjacent_text_visible", False),
    }


def add_quality_flag_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("quality flags (all default false)")
    g.add_argument("--damage", action="store_true")
    g.add_argument("--faded", action="store_true")
    g.add_argument("--smudged", action="store_true")
    g.add_argument("--low-legibility", action="store_true",
                   dest="low_legibility")
    g.add_argument("--partial-cut", action="store_true",
                   dest="partial_cut")
    g.add_argument("--adjacent-text-visible", action="store_true",
                   dest="adjacent_text_visible")


def add_repair_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("repair (optional)")
    g.add_argument("--repair-needed", action="store_true",
                   dest="repair_needed")
    g.add_argument("--repair-reason", default="",
                   dest="repair_reason",
                   help="One-sentence repair_reason (used only when "
                        "--repair-needed is also passed)")


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--text-file", required=True,
                   help="Path to a UTF-8 text file with the transcript "
                        "(use '-' for stdin)")
    p.add_argument("--source", default="human",
                   help="Short label identifying where the transcript "
                        "came from (default: 'human')")
    p.add_argument("--notes", default="",
                   help="Free-form note appended to transcriber_notes "
                        "after the [source: ...] prefix")
    add_quality_flag_args(p)
    add_repair_args(p)


def append_experiments_log(entry: dict) -> None:
    os.makedirs(os.path.dirname(EXPERIMENTS_LOG), exist_ok=True)
    with open(EXPERIMENTS_LOG, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def cmd_slice(args: argparse.Namespace) -> int:
    text = read_text(args.text_file)
    notes = build_notes(args.source, args.notes)
    flags = build_quality_flags(args)

    envelope = {
        "slices": [{
            "idx": args.slice_idx,
            "transcript_text": text,
            "transcriber_notes": notes,
        }],
        "quality_flags": flags,
        "repair_needed": args.repair_needed,
        "repair_reason": (args.repair_reason or "") if args.repair_needed
                         else "",
    }

    out = os.path.join(
        RESULTS_DIR, f"{args.row_id}.slice{args.slice_idx:02d}.json")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(out, "w") as f:
        json.dump(envelope, f, indent=2, ensure_ascii=False)
    print(f"wrote per-slice envelope: {out} ({len(text)} chars)")

    append_experiments_log({
        "ts": _ts(),
        "row_id": args.row_id,
        "import_kind": "slice",
        "slice_idx": args.slice_idx,
        "source": args.source,
        "transcript_chars": len(text),
        "repair_needed": bool(args.repair_needed),
        "notes": args.notes,
    })

    if args.no_merge:
        print("(--no-merge set; skipping merge_slice_results)")
        return 0

    cmd = [sys.executable, "-m", "transcribe.merge_slice_results",
           args.row_id]
    rc = subprocess.call(cmd, cwd=_db.REPO_ROOT)
    if rc != 0:
        print(f"merge returned non-zero ({rc})", file=sys.stderr)
    return rc


def cmd_subslice(args: argparse.Namespace) -> int:
    text = read_text(args.text_file)
    notes = build_notes(args.source, args.notes)
    flags = build_quality_flags(args)

    envelope = {
        "slices": [{
            "idx": args.sub_idx,
            "transcript_text": text,
            "transcriber_notes": notes,
        }],
        "quality_flags": flags,
        "repair_needed": args.repair_needed,
        "repair_reason": (args.repair_reason or "") if args.repair_needed
                         else "",
    }

    out = os.path.join(
        RESULTS_DIR,
        f"{args.row_id}.slice{args.slice_idx:02d}.sub{args.sub_idx:02d}.json")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(out, "w") as f:
        json.dump(envelope, f, indent=2, ensure_ascii=False)
    print(f"wrote per-sub-piece envelope: {out} ({len(text)} chars)")

    append_experiments_log({
        "ts": _ts(),
        "row_id": args.row_id,
        "import_kind": "subslice",
        "slice_idx": args.slice_idx,
        "sub_idx": args.sub_idx,
        "source": args.source,
        "transcript_chars": len(text),
        "repair_needed": bool(args.repair_needed),
        "notes": args.notes,
    })

    if args.no_assemble:
        print("(--no-assemble set; skipping subdivide_slice assemble)")
        return 0

    cmd = [sys.executable, "-m", "transcribe.subdivide_slice",
           "assemble", args.row_id, str(args.slice_idx)]
    rc = subprocess.call(cmd, cwd=_db.REPO_ROOT)
    if rc != 0:
        print(f"assemble returned non-zero ({rc}); the orchestrator "
              f"can re-run merge_slice_results once all sub-pieces "
              f"are on disk", file=sys.stderr)
    return rc


def cmd_column(args: argparse.Namespace) -> int:
    text = read_text(args.text_file)
    notes = build_notes(args.source, args.notes)
    flags = build_quality_flags(args)

    envelope = {
        "transcript_text": text,
        "transcriber_notes": notes,
        "quality_flags": flags,
        "repair_needed": args.repair_needed,
        "repair_reason": (args.repair_reason or "") if args.repair_needed
                         else "",
    }

    out = os.path.join(RESULTS_DIR, f"{args.row_id}.json")
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(out, "w") as f:
        json.dump(envelope, f, indent=2, ensure_ascii=False)
    print(f"wrote full-column envelope: {out} ({len(text)} chars)")

    append_experiments_log({
        "ts": _ts(),
        "row_id": args.row_id,
        "import_kind": "column",
        "source": args.source,
        "transcript_chars": len(text),
        "repair_needed": bool(args.repair_needed),
        "notes": args.notes,
    })

    if args.no_ingest:
        print("(--no-ingest set; skipping ingest_column_result)")
        return 0

    cmd = [sys.executable, "-m", "transcribe.ingest_column_result",
           args.row_id, "--model", f"external:{args.source}"]
    rc = subprocess.call(cmd, cwd=_db.REPO_ROOT)
    if rc != 0:
        print(f"ingest returned non-zero ({rc})", file=sys.stderr)
    return rc


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Import an externally-sourced transcript "
                    "(human / external OCR / correction) into "
                    "transcribe.db.")
    sub = p.add_subparsers(dest="kind", required=True)

    sp = sub.add_parser(
        "slice",
        help="Import one slice's transcript; merge with any other slices "
             "on disk and run the column ingester.")
    sp.add_argument("row_id", help="The column_transcripts row id (UUID)")
    sp.add_argument("slice_idx", type=int,
                    help="Slice index in the column manifest (0-based)")
    sp.add_argument("--no-merge", action="store_true",
                    help="Write the per-slice envelope but skip the "
                         "merge_slice_results invocation")
    add_common_args(sp)
    sp.set_defaults(func=cmd_slice)

    bp = sub.add_parser(
        "subslice",
        help="Import one sub-piece's transcript (Tier-4 retry residue); "
             "assemble with any other sub-pieces on disk into the slice "
             "envelope.")
    bp.add_argument("row_id", help="The column_transcripts row id (UUID)")
    bp.add_argument("slice_idx", type=int,
                    help="Parent slice index (0-based)")
    bp.add_argument("sub_idx", type=int,
                    help="Sub-piece index inside the parent slice (0-based)")
    bp.add_argument("--no-assemble", action="store_true",
                    help="Write the per-sub-piece envelope but skip the "
                         "subdivide_slice assemble invocation")
    add_common_args(bp)
    bp.set_defaults(func=cmd_subslice)

    cp = sub.add_parser(
        "column",
        help="Import a full-column transcript; bypasses slicing.")
    cp.add_argument("row_id", help="The column_transcripts row id (UUID)")
    cp.add_argument("--no-ingest", action="store_true",
                    help="Write the full-column envelope but skip the "
                         "ingest_column_result invocation")
    add_common_args(cp)
    cp.set_defaults(func=cmd_column)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
