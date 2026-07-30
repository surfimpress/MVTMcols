"""Backfill the slice-overlap dedup fix onto already-ingested columns.

Context: transcribe.slice.join_slice_transcripts previously joined
subdivided sub-slices with a bare '\\n', with no check for the ~20px
image overlap between adjacent slices duplicating a line of text (see
transcribe/quality_review.md, 2026-07-30). The joiner is now fixed
(resolve_slice_overlap), but that only takes effect for *new* ingests.
This script re-processes rows that were already ingested under the
old behaviour.

For each 'done' row with slice_boundaries set, the stored
transcript_text is sliced back into its original per-slice pieces
using the recorded char_offset_start/end (those offsets partition the
joined text losslessly into exactly what was written at ingest time),
then re-joined with the fixed joiner. If that changes anything, the
old joined text is preserved in transcript_text_raw and
transcript_text/slice_boundaries are updated to the deduped result.
Rows where nothing changes are left alone (transcript_text_raw stays
NULL — this script does not touch them at all, so re-running is safe
and only look at rows not yet backfilled).

Rows with no slice_boundaries (legacy full-image transcripts, or
columns that were never sliced) are skipped entirely.

Usage:
    python3 -m transcribe.dedup_backfill --dry-run   # preview
    python3 -m transcribe.dedup_backfill             # apply
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db as _db
from . import slice as _slice


def find_candidate_rows(conn) -> list:
    """Rows eligible for backfill: done, sliced, not yet processed."""
    return conn.execute(
        "SELECT id, transcript_text, slice_boundaries "
        "FROM column_transcripts "
        "WHERE status='done' AND slice_boundaries IS NOT NULL "
        "AND transcript_text_raw IS NULL"
    ).fetchall()


def backfill_row(conn, row_id: str, old_text: str,
                 boundaries: list[dict]) -> dict | None:
    """Re-join one row's stored per-slice text with the fixed joiner.

    Returns a report dict if the row changed (and, unless dry_run,
    writes the update), or None if the fixed joiner found nothing to
    collapse.
    """
    per_slice_text = [old_text[b["char_offset_start"]:b["char_offset_end"]]
                      for b in boundaries]
    new_text, new_boundaries, events = _slice.join_slice_transcripts(
        boundaries, per_slice_text)
    if not events:
        return None

    conn.execute(
        "UPDATE column_transcripts SET "
        "transcript_text=?, transcript_text_raw=?, slice_boundaries=?, "
        "updated_at=? WHERE id=?",
        (new_text, old_text, json.dumps(new_boundaries),
         _db.now_iso(), row_id))

    return {
        "row_id": row_id,
        "old_len": len(old_text),
        "new_len": len(new_text),
        "events": events,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill slice-overlap dedup onto already-"
                    "ingested column transcripts.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change without writing to the DB")
    args = p.parse_args(argv)

    conn = _db.open_connection()
    try:
        rows = find_candidate_rows(conn)
        print(f"scanning {len(rows)} candidate row(s) "
              f"(status=done, sliced, not yet backfilled)")

        changed = []
        for row in rows:
            boundaries = json.loads(row["slice_boundaries"])
            result = backfill_row(
                conn, row["id"], row["transcript_text"], boundaries)
            if result:
                changed.append(result)

        if args.dry_run:
            conn.rollback()
            print(f"[dry run] would update {len(changed)} of "
                  f"{len(rows)} row(s); no changes written")
        else:
            conn.commit()
            print(f"updated {len(changed)} of {len(rows)} row(s)")

        for c in changed:
            print(f"  {c['row_id']}: {c['old_len']} -> {c['new_len']} chars, "
                  f"{len(c['events'])} overlap(s) collapsed")
            for e in c["events"]:
                print(f"    - {e['prev_line'][:60]!r} / "
                      f"{e['curr_line'][:60]!r}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
