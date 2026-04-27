"""Audit log for mutating CLI commands.

Every mutating subcommand of `mvtm_cli.py` (none yet — this skeleton is
read-only) records its change here. The returned id is the
`transaction_id` field of the JSON envelope, so an LLM agent can:

- correlate stdout output with this audit row,
- ask `mvtm` for an undo (future), referring to a transaction id,
- replay corrections by reading the `after_json` payloads.

Schema (see `migrations/002_hand_edited_and_history.sql`):

    cli_history(id, ts, command, table_name, row_key_json,
                before_json, after_json)

`row_key_json` is a JSON-encoded dict identifying the affected row.
The shape varies by table — uuid for ads, composite (year, month,
day, page) for layouts/geometry — which is why this is text, not a
typed column.

This module is groundwork: no caller exists in the walking skeleton
because no mutating subcommand has shipped yet. It lands now so the
plumbing is ready for `adjust-ad`, `recut-page`, etc.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing


def record_change(db_path: str, command: str, table_name: str,
                  row_key: dict, before: dict | None,
                  after: dict | None) -> int:
    """Insert one cli_history row and return its id (the transaction_id).

    `before` and `after` may each be None (e.g. a delete has no after,
    a create has no before). They are JSON-encoded with sort_keys so
    a future diff-by-text comparison is stable.
    """
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cur = conn.execute(
            "INSERT INTO cli_history (command, table_name, row_key_json, "
            "before_json, after_json) VALUES (?, ?, ?, ?, ?)",
            (
                command,
                table_name,
                json.dumps(row_key, sort_keys=True),
                json.dumps(before, sort_keys=True) if before is not None else None,
                json.dumps(after, sort_keys=True) if after is not None else None,
            ),
        )
        return cur.lastrowid
