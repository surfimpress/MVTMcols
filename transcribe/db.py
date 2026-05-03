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


def open_connection(db_path: str | None = None,
                    *,
                    attach_mvtm: bool = False,
                    mvtm_path: str | None = None) -> sqlite3.Connection:
    """Open a connection to ``transcribe.db`` with sane pragmas.

    If ``attach_mvtm`` is true, also ATTACH the parent project's
    ``mvtm.db`` as schema name ``mvtm`` for read-only joining. The
    attached connection is opened with ``mode=ro`` to make the
    "this layer never writes mvtm.db" rule structural.

    Path defaults are read from the module attributes at call time
    (not as Python default-argument values), so tests can rebind
    ``TRANSCRIBE_DB_PATH`` to a temporary file and have the change
    take effect on subsequent calls.
    """
    if db_path is None:
        db_path = TRANSCRIBE_DB_PATH
    if mvtm_path is None:
        mvtm_path = MVTM_DB_PATH

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
                     slice_boundaries: list | None = None,
                     tokens_in: int | None = None,
                     tokens_out: int | None = None,
                     cost_usd: float | None = None) -> None:
    """Update a claimed column row with the LLM result.

    ``slice_boundaries`` is the manifest-with-char-offsets returned by
    ``transcribe.slice.join_slice_transcripts``. Stored as JSON; null
    on legacy full-image transcripts.
    """
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
              slice_boundaries=?,
              tokens_in=?,
              tokens_out=?,
              cost_usd=?,
              updated_at=?
            WHERE id=?""",
        (transcript_text, transcriber_notes,
         json.dumps(quality_flags) if quality_flags is not None else None,
         1 if repair_needed else 0, repair_reason,
         model, prompt_hash_value, raw_response_json,
         json.dumps(slice_boundaries)
             if slice_boundaries is not None else None,
         tokens_in, tokens_out, cost_usd, now_iso(), row_id))
    conn.commit()


def claim_ad(conn: sqlite3.Connection,
             *,
             ad_uuid: str,
             year: int, month: int, day: int, page: int,
             image_path: str,
             image_sha256: str) -> str:
    """Insert a stub row in ``ad_transcripts`` with
    ``status='claimed'``. Idempotent on the unique key
    ``(ad_uuid, image_sha256)`` — if a row already exists for this
    image content, returns its id without a new insert.
    """
    existing = conn.execute(
        """SELECT id, status FROM ad_transcripts
           WHERE ad_uuid=? AND image_sha256=?""",
        (ad_uuid, image_sha256)).fetchone()
    if existing is not None:
        return existing["id"]

    new_id = new_uuid()
    conn.execute(
        """INSERT INTO ad_transcripts
           (id, ad_uuid, year, month, day, page, image_path,
            image_sha256, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'claimed', ?)""",
        (new_id, ad_uuid, year, month, day, page, image_path,
         image_sha256, now_iso()))
    conn.commit()
    return new_id


def mark_ad_done(conn: sqlite3.Connection,
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
    """Update a claimed ad row with the LLM result."""
    conn.execute(
        """UPDATE ad_transcripts SET
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


def mark_ad_failed(conn: sqlite3.Connection,
                   row_id: str,
                   error_message: str) -> None:
    conn.execute(
        """UPDATE ad_transcripts SET
              status='failed',
              transcriber_notes=?,
              updated_at=?
            WHERE id=?""",
        (error_message, now_iso(), row_id))
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


# -------- repairs --------------------------------------------------

def raise_repair(conn: sqlite3.Connection,
                 *,
                 target_kind: str,
                 target_ref: dict,
                 repair_kind: str,
                 description: str,
                 raised_by: str,
                 related_column_id: str | None = None,
                 related_ad_uuid: str | None = None,
                 related_item_id: str | None = None,
                 proposed_fix: dict | None = None,
                 suggested_cli: str | None = None,
                 notes: str | None = None) -> str:
    """Insert a row in ``repairs`` and return the new id.

    ``target_ref`` is the structured pointer to what the repair is
    about — e.g. ``{"year": 1892, "month": 1, "day": 1, "page": 1,
    "col_idx": 0}`` for a column repair, or ``{"ad_uuid": "..."}``
    for an ad repair. Stored as JSON because the shape varies by
    target_kind.

    No state is mutated in mvtm.db. The repair is surfaced to the
    user via ``transcribe repairs list``; if it carries a
    ``suggested_cli`` field, the user runs that ``mvtm_cli.py``
    invocation themselves.
    """
    new_id = new_uuid()
    conn.execute(
        """INSERT INTO repairs
           (id, target_kind, target_ref_json, repair_kind,
            description, proposed_fix_json, suggested_cli,
            status, raised_by, raised_at,
            related_column_id, related_ad_uuid, related_item_id,
            notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
        (new_id, target_kind,
         json.dumps(target_ref, sort_keys=True),
         repair_kind, description,
         json.dumps(proposed_fix, sort_keys=True)
             if proposed_fix is not None else None,
         suggested_cli, raised_by, now_iso(),
         related_column_id, related_ad_uuid, related_item_id,
         notes))
    conn.commit()
    return new_id


# -------- agent file helpers ---------------------------------------

def read_agent_default_model(agent_file_path: str) -> str | None:
    """Return the default model from an agent file's YAML
    frontmatter, or None if not set.

    Minimal parser — we only need ``model:`` from the frontmatter,
    and the file format is fixed (``---`` fences, simple key:
    value lines). Pulling in PyYAML for one line would be overkill.
    """
    with open(agent_file_path) as f:
        text = f.read()
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip()
    return None
