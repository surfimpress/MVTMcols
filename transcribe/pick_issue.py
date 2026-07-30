"""Pick a random issue ready for column transcription.

An issue is "ready" when:
  - it has column entries in mvtm.file_assets, AND
  - it has at least one page in mvtm.page_layouts (boundary_positions
    must exist to build column tickets), AND
  - it has no column_transcripts rows with status='done'.

Issues with only 'claimed' or 'failed' rows are treated as not done,
so a crashed run can be resumed by simply re-claiming the same issue.

Usage::

    python3 -m transcribe.pick_issue              # print one YYYY-MM-DD
    python3 -m transcribe.pick_issue --max-year 1880
    python3 -m transcribe.pick_issue --count 5    # print 5 candidates
    python3 -m transcribe.pick_issue --stats      # count remaining issues

The default year range is 1861–1979 — spanning the full run rather
than just the earliest decades, so issues from diverse periods (not
just the 19th century) get picked up during background transcription.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys

from . import db as _db


def _eligible_issues(conn: sqlite3.Connection,
                     min_year: int,
                     max_year: int) -> list[tuple[int, int, int]]:
    """Return (year, month, day) tuples for issues that are ready.

    An issue must:
    1. Have at least one column in file_assets within the year range.
    2. Have at least one page in page_layouts (so we can build tickets).
    3. Have no 'done' rows in column_transcripts.
    """
    # Issues with columns in file_assets within the date range.
    asset_rows = conn.execute(
        """SELECT DISTINCT fa.year, fa.month, fa.day
             FROM mvtm.file_assets AS fa
            WHERE fa.kind = 'column'
              AND fa.year >= ? AND fa.year <= ?
              AND EXISTS (
                  SELECT 1 FROM mvtm.page_layouts AS pl
                   WHERE pl.year = fa.year
                     AND pl.month = fa.month
                     AND pl.day = fa.day)
            ORDER BY fa.year, fa.month, fa.day""",
        (min_year, max_year)).fetchall()

    # Issues that already have at least one 'done' transcript.
    done_rows = conn.execute(
        """SELECT DISTINCT year, month, day
             FROM column_transcripts
            WHERE status = 'done'""").fetchall()
    done_set = {(r["year"], r["month"], r["day"]) for r in done_rows}

    eligible = []
    for r in asset_rows:
        key = (r["year"], r["month"], r["day"])
        if key not in done_set:
            eligible.append(key)

    return eligible


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pick a random issue for transcription.")
    p.add_argument("--min-year", type=int, default=1861,
                   help="Earliest year to include (default 1861)")
    p.add_argument("--max-year", type=int, default=1979,
                   help="Latest year to include (default 1979)")
    p.add_argument("--count", type=int, default=1,
                   help="How many issues to print (default 1)")
    p.add_argument("--stats", action="store_true",
                   help="Print count of remaining issues and exit")
    args = p.parse_args(argv)

    conn = _db.open_connection(attach_mvtm=True)
    try:
        eligible = _eligible_issues(conn, args.min_year, args.max_year)
    finally:
        conn.close()

    if args.stats:
        print(f"{len(eligible)} issues remaining in "
              f"{args.min_year}–{args.max_year} (not yet done)")
        return 0

    if not eligible:
        print("No eligible issues found — all done or no data in range.",
              file=sys.stderr)
        return 1

    random.shuffle(eligible)
    picks = eligible[: args.count]
    for year, month, day in picks:
        print(f"{year:04d}-{month:02d}-{day:02d}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
