"""Database helpers for the transcribe pipeline.

Owns the connection to ``transcribe/data/transcribe.db`` and exposes
the convenience to ATTACH the parent project's ``data/mvtm.db`` as a
read-only secondary database so cross-database joins work in plain
SQL.

Nothing in this module calls an LLM. Per the design, LLM work is
done by Claude Code spawning subagents; this module's job is the
state machine around those calls — claim a unit of work, write a
ticket, ingest a result.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
import uuid

# Repo layout assumptions:
#   <repo>/transcribe/db.py            (this file)
#   <repo>/transcribe/schema.sql       (canonical schema)
#   <repo>/transcribe/data/transcribe.db
#   <repo>/data/mvtm.db                (the parent's DB; read-only here)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
SCHEMA_PATH = os.path.join(_THIS_DIR, "schema.sql")
TRANSCRIBE_DB_PATH = os.path.join(_THIS_DIR, "data", "transcribe.db")
MVTM_DB_PATH = os.path.join(REPO_ROOT, "data", "mvtm.db")


def now_iso() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    """Return a new UUID4 as a string. Mirrors detect_ads.py usage."""
    return str(uuid.uuid4())


def open_connection(db_path: str = TRANSCRIBE_DB_PATH,
                    *,
                    attach_mvtm: bool = False,
                    mvtm_path: str = MVTM_DB_PATH) -> sqlite3.Connection:
    """Open a connection to ``transcribe.db`` with sane pragmas.

    If ``attach_mvtm`` is true, also ATTACH the parent project's
    ``mvtm.db`` as schema name ``mvtm`` for read-only joining. The
    attached connection is opened with ``mode=ro`` to make the
    "this layer never writes mvtm.db" rule structural.
    """
    if not os.path.isfile(db_path):
        raise FileNotFoundError(
            f"transcribe.db not found at {db_path}. "
            f"Run `python3 -m transcribe.bootstrap_db` first.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    if attach_mvtm:
        if not os.path.isfile(mvtm_path):
            raise FileNotFoundError(
                f"mvtm.db not found at {mvtm_path}")
        # Attach with file URI in read-only mode so this connection
        # cannot mutate the parent DB even by accident.
        uri = f"file:{mvtm_path}?mode=ro"
        conn.execute(f"ATTACH DATABASE '{uri}' AS mvtm")

    return conn


def sha256_file(path: str) -> str:
    """Return SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def prompt_hash(template_text: str, context: dict) -> str:
    """SHA-256 over the prompt template plus the JSON-serialised
    non-image context. Image bytes are intentionally excluded so the
    same prompt against the same context but a slightly different
    image (e.g. re-cut PNG) still yields the same prompt_hash —
    it's the *prompt design* identifier, not a per-call digest.
    """
    payload = template_text + "\n---\n" + json.dumps(
        context, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -------- ticket-state transitions ---------------------------------

def claim_column(conn: sqlite3.Connection,
                 *,
                 year: int, month: int, day: int,
                 page: int, col_idx: int,
                 image_path: str,
                 image_sha256: str) -> str:
    """Insert a stub row in ``column_transcripts`` with
    ``status='claimed'``. Idempotent on the unique key
    ``(year, month, day, page, col_idx, image_sha256)`` — if a row
    already exists for this image content, returns its id without
    a new insert.
    """
    existing = conn.execute(
        """SELECT id, status FROM column_transcripts
           WHERE year=? AND month=? AND day=? AND page=?
             AND col_idx=? AND image_sha256=?""",
        (year, month, day, page, col_idx, image_sha256)).fetchone()
    if existing is not None:
        return existing["id"]

    new_id = new_uuid()
    conn.execute(
        """INSERT INTO column_transcripts
           (id, year, month, day, page, col_idx, image_path,
            image_sha256, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?)""",
        (new_id, year, month, day, page, col_idx, image_path,
         image_sha256, now_iso()))
    conn.commit()
    return new_id


def mark_column_done(conn: sqlite3.Connection,
                     row_id: str,
                     *,
                     transcript_text: str,
                     transcriber_notes: str | None,
                     quality_flags: dict | None,
                     repair_needed: bool,
                     repair_reason: str | None,
                     model: str,
                     prompt_hash_value: str,
                     raw_response_json: str,
                     tokens_in: int | None = None,
                     tokens_out: int | None = None,
                     cost_usd: float | None = None) -> None:
    """Update a claimed column row with the LLM result."""
    conn.execute(
        """UPDATE column_transcripts SET
              status='done',
              transcript_text=?,
              transcriber_notes=?,
              quality_flags=?,
              repair_needed=?,
              repair_reason=?,
              model=?,
              prompt_hash=?,
              raw_response_json=?,
              tokens_in=?,
              tokens_out=?,
              cost_usd=?,
              updated_at=?
            WHERE id=?""",
        (transcript_text, transcriber_notes,
         json.dumps(quality_flags) if quality_flags is not None else None,
         1 if repair_needed else 0, repair_reason,
         model, prompt_hash_value, raw_response_json,
         tokens_in, tokens_out, cost_usd, now_iso(), row_id))
    conn.commit()


def mark_column_failed(conn: sqlite3.Connection,
                       row_id: str,
                       error_message: str) -> None:
    conn.execute(
        """UPDATE column_transcripts SET
              status='failed',
              transcriber_notes=?,
              updated_at=?
            WHERE id=?""",
        (error_message, now_iso(), row_id))
    conn.commit()
