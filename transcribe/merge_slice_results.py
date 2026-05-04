"""Merge per-slice column-transcriber envelopes into one canonical envelope.

Used when the per-column dispatch was blocked by Anthropic's content-filter
classifier and the orchestrator fell back to per-slice dispatch (Tier 2 /
Tier 3 retries — see ``.claude/skills/transcribe-issue/SKILL.md``).

Reads per-slice envelopes from
``transcribe/work/results/<row_id>.slice<NN>.json``, merges them into the
canonical column envelope at ``transcribe/work/results/<row_id>.json``,
and runs ``transcribe.ingest_column_result`` so the column row lands in
``column_transcripts`` the same way it would have under the default path.

Each per-slice envelope is the same Sliced-mode envelope the
column-transcriber emits, but with a single-element ``slices`` list. The
column-level ``quality_flags``, ``repair_needed`` and ``repair_reason``
fields are still present in each per-slice envelope; the merge OR's the
flags, ORs the repair_needed booleans, and concatenates non-empty
repair_reasons.

Per-slice files that are missing or malformed cause the merge to abort
with a non-zero exit code — the orchestrator surfaces that to the user.
A common failure case is "tier 2 retry succeeded for some slices but one
slice still triggers the filter"; in that situation the orchestrator
either dispatches Tier 3 (Opus) for the remaining slice and re-runs the
merge, or surfaces the slice for human transcription.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys

from . import db as _db
from . import ingest_column_result as _ingest


RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
SLICE_FILE_RE = re.compile(r"^(?P<row>.+)\.slice(?P<idx>\d+)\.json$")
QUALITY_FLAGS = (
    "damage", "faded", "smudged", "low_legibility",
    "partial_cut", "adjacent_text_visible",
)


def find_slice_files(row_id: str) -> list[tuple[int, str]]:
    """Return ``[(idx, path), ...]`` sorted by idx for per-slice files
    matching ``<RESULTS>/<row_id>.slice<NN>.json``.
    """
    pattern = os.path.join(RESULTS_DIR, f"{row_id}.slice*.json")
    found: list[tuple[int, str]] = []
    for path in glob.glob(pattern):
        m = SLICE_FILE_RE.match(os.path.basename(path))
        if m and m.group("row") == row_id:
            found.append((int(m.group("idx")), path))
    return sorted(found)


def load_per_slice(path: str) -> dict:
    with open(path) as f:
        raw = f.read()
    data = json.loads(_ingest.strip_fence(raw))
    if "slices" not in data or not isinstance(data["slices"], list) \
            or not data["slices"]:
        raise ValueError(f"{path}: no non-empty 'slices' array")
    if len(data["slices"]) != 1:
        raise ValueError(
            f"{path}: per-slice envelope must contain exactly one "
            f"slice record, got {len(data['slices'])}")
    rec = data["slices"][0]
    if "transcript_text" not in rec:
        raise ValueError(
            f"{path}: slice record missing 'transcript_text'")
    return data


def merge(per_slice_envelopes: list[tuple[int, dict]]) -> dict:
    """Combine per-slice envelopes into one canonical column envelope.
    Caller passes ``(idx, envelope)`` tuples sorted by idx.
    """
    merged_slices: list[dict] = []
    flags = {f: False for f in QUALITY_FLAGS}
    repair_needed = False
    repair_reasons: list[str] = []

    for idx, env in per_slice_envelopes:
        rec = dict(env["slices"][0])
        rec["idx"] = idx  # force agreement with the file's idx
        merged_slices.append(rec)

        env_flags = env.get("quality_flags") or {}
        for f in QUALITY_FLAGS:
            if env_flags.get(f) is True:
                flags[f] = True

        if env.get("repair_needed"):
            repair_needed = True
        rr = (env.get("repair_reason") or "").strip()
        if rr:
            repair_reasons.append(f"[slice {idx:02d}] {rr}")

    return {
        "slices": merged_slices,
        "quality_flags": flags,
        "repair_needed": repair_needed,
        "repair_reason": " | ".join(repair_reasons),
    }


def run_ingester(row_id: str) -> int:
    cmd = [sys.executable, "-m", "transcribe.ingest_column_result", row_id]
    return subprocess.call(cmd, cwd=_db.REPO_ROOT)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Merge per-slice envelopes and run the column ingester.")
    p.add_argument("row_id", help="The column_transcripts row id (UUID)")
    p.add_argument("--no-ingest", action="store_true",
                   help="Write the merged envelope but skip the ingester run")
    args = p.parse_args(argv)

    files = find_slice_files(args.row_id)
    if not files:
        print(f"merge failed: no per-slice result files found at "
              f"{RESULTS_DIR}/{args.row_id}.slice*.json", file=sys.stderr)
        return 1

    indices = [idx for idx, _ in files]
    expected = list(range(len(indices)))
    if indices != expected:
        print(f"merge failed: slice indices not contiguous from 0: "
              f"got {indices}, expected {expected}", file=sys.stderr)
        return 1

    envelopes: list[tuple[int, dict]] = []
    for idx, path in files:
        try:
            envelopes.append((idx, load_per_slice(path)))
        except (ValueError, json.JSONDecodeError) as e:
            print(f"merge failed: {e}", file=sys.stderr)
            return 1

    merged = merge(envelopes)

    out_path = os.path.join(RESULTS_DIR, f"{args.row_id}.json")
    with open(out_path, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"merged {len(envelopes)} per-slice envelopes -> {out_path}")

    if args.no_ingest:
        return 0

    rc = run_ingester(args.row_id)
    if rc != 0:
        print(f"ingester returned non-zero ({rc})", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
