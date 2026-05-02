"""Create transcribe.db from schema.sql.

Idempotent: re-running against an existing DB is a no-op (the schema
uses CREATE TABLE IF NOT EXISTS throughout). To wipe and rebuild,
delete the .db file first — but the right path for that is to use
the migration story when the schema evolves, not bootstrap.

Usage::

    python3 -m transcribe.bootstrap_db
"""

from __future__ import annotations

import os
import sqlite3
import sys

from . import db as _db


def main() -> int:
    with open(_db.SCHEMA_PATH) as f:
        schema_sql = f.read()
    db_path = _db.TRANSCRIBE_DB_PATH

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if os.path.exists(db_path):
        print(f"transcribe.db already exists at {db_path}")
        print("Applying schema (CREATE TABLE IF NOT EXISTS) so any "
              "missing tables are added; nothing destructive.")
    else:
        print(f"Creating {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    # Verify we can re-open it through db.open_connection so the
    # bootstrap doubles as a check that the file is readable.
    test_conn = _db.open_connection(db_path, attach_mvtm=False)
    tables = [r[0] for r in test_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "ORDER BY name").fetchall()]
    test_conn.close()
    print(f"Tables present ({len(tables)}): {', '.join(tables)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
