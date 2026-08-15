"""Ingest one column-transcriber result back into transcribe.db.

The orchestrator (Claude Code) saves the agent's JSON envelope to
``transcribe/work/results/<row-id>.json`` and then runs::

    python3 -m transcribe.ingest_column_result <row-id>

This validates the envelope, marks the column row 'done' with the
transcript and quality flags, and — if the agent flagged
``repair_needed: true`` — inserts an open row in ``repairs``.

If the orchestrator dispatched an agent with a non-default model
(e.g. for the Haiku-vs-Sonnet comparison), pass ``--model NAME``;
otherwise the ingester reads the default from the agent file's
frontmatter.

Result file layout::

    transcribe/work/results/<row-id>.json

The file should contain the JSON envelope the agent returned, with
no surrounding prose and no markdown fence — but the ingester
tolerates a leading ```json fence and trailing ``` because that's a
common failure mode for chatty models.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import db as _db
from . import slice as _slice


RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
WORK_TICKETS_DIR = os.path.join(
    _db.REPO_ROOT, "transcribe", "work", "columns")
AGENT_FILE_REL = ".claude/agents/column-transcriber.md"

_REQUIRED_FLAGS = (
    "damage", "faded", "smudged", "low_legibility",
    "partial_cut", "adjacent_text_visible",
)


def strip_fence(s: str) -> str:
    """Strip a leading ```json (or ```) fence and trailing ``` if
    the agent wrapped its JSON despite being told not to.
    """
    s = s.strip()
    if s.startswith("```"):
        # Drop the first line (```json or just ```).
        nl = s.find("\n")
        if nl != -1:
            s = s[nl + 1:]
    if s.endswith("```"):
        s = s[: -3].rstrip()
    return s


def parse_envelope(raw: str) -> dict:
    """Parse an agent's JSON envelope. Supports two shapes:

    **Sliced** (the post-2026-05-02 default), keyed by ``slices``::

        {
          "slices": [
            {"idx": 0, "transcript_text": "...",
             "transcriber_notes": "...", "confidence": "high"},
            ...
          ],
          "quality_flags": {...}, "repair_needed": false,
          "repair_reason": ""
        }

    ``confidence`` (``"high"|"medium"|"low"``, optional) is not
    validated here -- a missing or unrecognized value is stored as
    ``None`` rather than rejecting the whole column, since it is an
    enrichment field, not a correctness-critical one. See
    ``_assemble_transcript``, which merges it into the
    ``slice_boundaries`` JSON.

    **Full-image** (legacy / fall-back), keyed by ``transcript_text``::

        {
          "transcript_text": "...", "transcriber_notes": "...",
          "quality_flags": {...}, "repair_needed": false,
          "repair_reason": ""
        }

    Returns the parsed dict on success. Raises ValueError with a
    helpful message on bad shape — caller decides whether to
    surface as a row failure or a hard error.
    """
    try:
        data = json.loads(strip_fence(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"result is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"result must be a JSON object, got {type(data).__name__}")

    sliced = "slices" in data
    if sliced:
        if not isinstance(data["slices"], list) or not data["slices"]:
            raise ValueError("'slices' must be a non-empty list")
        for i, s in enumerate(data["slices"]):
            if not isinstance(s, dict):
                raise ValueError(
                    f"slices[{i}] must be a JSON object")
            for k in ("idx", "transcript_text"):
                if k not in s:
                    raise ValueError(
                        f"slices[{i}] missing required field {k!r}")
        common_required = ("quality_flags", "repair_needed", "repair_reason")
    else:
        common_required = ("transcript_text", "transcriber_notes",
                           "quality_flags", "repair_needed", "repair_reason")

    missing = [k for k in common_required if k not in data]
    if missing:
        raise ValueError(f"missing required fields: {missing}")

    flags = data["quality_flags"]
    if not isinstance(flags, dict):
        raise ValueError("quality_flags must be a JSON object")

    missing_flags = [f for f in _REQUIRED_FLAGS if f not in flags]
    if missing_flags:
        raise ValueError(
            f"quality_flags missing required keys: {missing_flags}")

    for k, v in flags.items():
        if not isinstance(v, bool):
            raise ValueError(
                f"quality_flags.{k} must be boolean, got {type(v).__name__}")

    if not isinstance(data["repair_needed"], bool):
        raise ValueError("repair_needed must be boolean")

    return data


def load_ticket(row_id: str) -> dict:
    path = os.path.join(WORK_TICKETS_DIR, f"{row_id}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no ticket file at {path}")
    with open(path) as f:
        return json.load(f)


def load_result(row_id: str, result_path: str | None = None) -> str:
    if result_path is None:
        result_path = os.path.join(RESULTS_DIR, f"{row_id}.json")
    if not os.path.isfile(result_path):
        raise FileNotFoundError(
            f"no result file at {result_path}; the orchestrator should "
            f"save the agent's envelope there before running ingest")
    with open(result_path) as f:
        return f.read()


def _assemble_transcript(envelope: dict,
                         ticket: dict
                         ) -> tuple[str, str, list[dict] | None, str | None]:
    """Return ``(transcript_text, transcriber_notes, slice_boundaries,
    transcript_text_raw)`` from an envelope, joining per-slice
    transcripts when sliced.

    For the sliced shape, the manifest from the ticket supplies the
    rule-class metadata the joiner needs; per-slice transcriber notes
    are concatenated into a single notes block prefixed by the slice
    index. ``slice_boundaries`` is the manifest enriched with char
    offsets and each slice's ``confidence`` (``"high"|"medium"|"low"``
    or ``None``) — written to the schema column of the same name.
    ``transcript_text_raw`` holds the pre-dedup joined text, and is
    non-None only when the joiner actually collapsed a slice-overlap
    duplicate (see ``transcribe.slice.join_slice_transcripts``).

    For the legacy full-image shape, the joiner is a no-op:
    ``slice_boundaries`` and ``transcript_text_raw`` both return None.
    """
    if "slices" not in envelope:
        return (envelope["transcript_text"],
                envelope.get("transcriber_notes") or "",
                None, None)

    manifest = ticket.get("slices")
    if not manifest:
        raise ValueError(
            "envelope is sliced but ticket has no slice manifest; "
            "this means the column was claimed before slicing was "
            "wired in — re-run claim_columns to refresh the ticket")

    # Sort agent's per-slice records by idx, then verify the indices
    # cover the manifest 1:1. The agent is told to return one record
    # per slice; missing or duplicated indices are an error.
    slice_records = sorted(envelope["slices"], key=lambda s: s["idx"])
    expected = list(range(len(manifest)))
    actual = [s["idx"] for s in slice_records]
    if actual != expected:
        raise ValueError(
            f"sliced envelope idx mismatch: expected {expected}, "
            f"got {actual}")

    per_slice_text = [s["transcript_text"] for s in slice_records]
    joined, boundaries, dedup_events = _slice.join_slice_transcripts(
        manifest, per_slice_text)

    # Merge the agent's per-slice confidence (if present) into the
    # boundaries record written to slice_boundaries -- reuses the
    # existing per-slice JSON column instead of adding a new one.
    for b, s in zip(boundaries, slice_records):
        b["confidence"] = s.get("confidence")

    transcript_text_raw = None
    if dedup_events:
        transcript_text_raw, _, _ = _slice.join_slice_transcripts(
            manifest, per_slice_text, dedupe=False)

    # Per-slice notes → one combined block. Empty notes are dropped.
    notes_parts = []
    for s in slice_records:
        n = (s.get("transcriber_notes") or "").strip()
        if n:
            notes_parts.append(f"[slice {s['idx']:02d}] {n}")
    transcriber_notes = "\n".join(notes_parts)

    return joined, transcriber_notes, boundaries, transcript_text_raw


def ingest(row_id: str,
           *,
           result_path: str | None = None,
           model: str | None = None) -> dict:
    """Validate and ingest one result. Returns a small report dict."""
    ticket = load_ticket(row_id)
    raw = load_result(row_id, result_path)
    envelope = parse_envelope(raw)

    if model is None:
        agent_path = os.path.join(_db.REPO_ROOT, AGENT_FILE_REL)
        model = _db.read_agent_default_model(agent_path) or "unknown"

    transcript_text, transcriber_notes, slice_boundaries, \
        transcript_text_raw = _assemble_transcript(envelope, ticket)

    conn = _db.open_connection()
    try:
        # Sanity: the row should still exist and be in 'claimed'
        # state. Re-ingesting a 'done' row is allowed (the result
        # file changed) but worth flagging in the report.
        existing = conn.execute(
            "SELECT status FROM column_transcripts WHERE id=?",
            (row_id,)).fetchone()
        if existing is None:
            raise ValueError(
                f"no column_transcripts row for id {row_id}")
        prior_status = existing["status"]

        _db.mark_column_done(
            conn, row_id,
            transcript_text=transcript_text,
            transcriber_notes=transcriber_notes or None,
            quality_flags=envelope["quality_flags"],
            repair_needed=envelope["repair_needed"],
            repair_reason=envelope.get("repair_reason") or None,
            slice_boundaries=slice_boundaries,
            transcript_text_raw=transcript_text_raw,
            model=model,
            prompt_hash_value=ticket.get("prompt_hash", ""),
            raw_response_json=raw)

        repair_id = None
        if envelope["repair_needed"]:
            target_ref = {
                "year": ticket["issue"]["year"],
                "month": ticket["issue"]["month"],
                "day": ticket["issue"]["day"],
                "page": ticket["page"],
                "col_idx": ticket["col_idx"],
            }
            # The agent picks repair_kind itself (see
            # column-transcriber.md): "advert_identification" when the
            # transcript is fine and the only issue is an ad needing
            # registration, "other" for anything affecting transcript
            # accuracy. Envelopes from before this field existed won't
            # have it -- fall back to "other" rather than fail ingest.
            repair_id = _db.raise_repair(
                conn,
                target_kind="column",
                target_ref=target_ref,
                repair_kind=envelope.get("repair_kind") or "other",
                description=envelope.get("repair_reason") or
                            "(no reason given)",
                raised_by=model,
                related_column_id=row_id)
    finally:
        conn.close()

    return {
        "row_id": row_id,
        "prior_status": prior_status,
        "model": model,
        "repair_id": repair_id,
        "transcript_chars": len(transcript_text),
        "sliced": slice_boundaries is not None,
        "num_slices": len(slice_boundaries) if slice_boundaries else 0,
        "dedup_applied": transcript_text_raw is not None,
        "any_quality_flag":
            any(envelope["quality_flags"].values()),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest one column-transcriber result.")
    p.add_argument("row_id", help="The column_transcripts row id (UUID)")
    p.add_argument("--result-file", default=None,
                   help="Path to the agent's JSON envelope file "
                        "(default: transcribe/work/results/<row-id>.json)")
    p.add_argument("--model", default=None,
                   help="Model name the agent ran as (default: read from "
                        "the agent file's frontmatter)")
    args = p.parse_args(argv)

    try:
        report = ingest(args.row_id,
                        result_path=args.result_file,
                        model=args.model)
    except (FileNotFoundError, ValueError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 1

    print(f"ingested {report['row_id']}")
    print(f"  prior status:    {report['prior_status']}")
    print(f"  model:           {report['model']}")
    print(f"  transcript:      {report['transcript_chars']} chars" +
          (f" (joined from {report['num_slices']} slices)"
           if report["sliced"] else " (full image)"))
    print(f"  quality flag(s): "
          f"{'yes' if report['any_quality_flag'] else 'none'}")
    if report["dedup_applied"]:
        print("  slice overlap dedup: collapsed (see transcript_text_raw)")
    if report["repair_id"]:
        print(f"  repair raised:   {report['repair_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
