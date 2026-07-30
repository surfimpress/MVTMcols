"""
python3 -m transcribe.query <YYYY-MM-DD> [status|claimed]

Lightweight DB query helper so the orchestrator and agents can check
issue progress without shelling out to the sqlite3 CLI.

Usage
-----
  python3 -m transcribe.query 1891-11-13           # status counts
  python3 -m transcribe.query 1891-11-13 claimed   # list claimed rows
  python3 -m transcribe.query 1891-11-13 status    # same as default

Output: tab-separated, one row per line.
"""
from __future__ import annotations
import sqlite3
import sys
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DB = os.path.join(_THIS_DIR, "data", "transcribe.db")


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_DB)


def issue_status(year: int, month: int, day: int) -> list[tuple]:
    """Return [(status, count), ...] for the given issue."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM column_transcripts "
            "WHERE year=? AND month=? AND day=? GROUP BY status ORDER BY status",
            (year, month, day),
        ).fetchall()
    return rows


def claimed_columns(year: int, month: int, day: int) -> list[tuple]:
    """Return [(id, page, col_idx), ...] for all claimed columns."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, page, col_idx FROM column_transcripts "
            "WHERE year=? AND month=? AND day=? AND status='claimed' "
            "ORDER BY page, col_idx",
            (year, month, day),
        ).fetchall()
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    date_str = sys.argv[1]
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
    except ValueError:
        print(f"Expected YYYY-MM-DD, got {date_str!r}", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[2] if len(sys.argv) > 2 else "status"

    if mode == "claimed":
        for row in claimed_columns(y, m, d):
            print("\t".join(str(c) for c in row))
    else:
        for row in issue_status(y, m, d):
            print("\t".join(str(c) for c in row))
