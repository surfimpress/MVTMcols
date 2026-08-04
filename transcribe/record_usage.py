"""CLI: record orchestrator-observed agent timing for a column row.

Usage:
    python3 -m transcribe.record_usage ROW_ID DURATION_MS TOOL_CALLS

Called by the orchestrating Claude Code session after it receives a
column-transcriber agent's completion notification -- see
db.record_agent_usage for why this can't be captured inside the
agent's own ingest step.
"""

from __future__ import annotations

import sys

from . import db


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    row_id, duration_ms, tool_calls = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    conn = db.open_connection()
    db.record_agent_usage(conn, row_id, duration_ms=duration_ms, tool_calls=tool_calls)
    print(f"recorded row={row_id} duration_ms={duration_ms} tool_calls={tool_calls}")


if __name__ == "__main__":
    main()
