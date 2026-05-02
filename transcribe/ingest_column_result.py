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
    """Parse an agent's JSON envelope.

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

    required = ("transcript_text", "transcriber_notes",
                "quality_flags", "repair_needed", "repair_reason")
    missing = [k for k in required if k not in data]
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
            transcript_text=envelope["transcript_text"],
            transcriber_notes=envelope.get("transcriber_notes") or None,
            quality_flags=envelope["quality_flags"],
            repair_needed=envelope["repair_needed"],
            repair_reason=envelope.get("repair_reason") or None,
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
            # The agent gives a free-form repair_reason; we don't
            # ask it to classify into our enum (that would force
            # misclassification when the case is novel). Pass-1
            # repairs land as 'other'; later curation can re-bucket.
            repair_id = _db.raise_repair(
                conn,
                target_kind="column",
                target_ref=target_ref,
                repair_kind="other",
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
        "transcript_chars": len(envelope["transcript_text"]),
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
    print(f"  transcript:      {report['transcript_chars']} chars")
    print(f"  quality flag(s): "
          f"{'yes' if report['any_quality_flag'] else 'none'}")
    if report["repair_id"]:
        print(f"  repair raised:   {report['repair_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
