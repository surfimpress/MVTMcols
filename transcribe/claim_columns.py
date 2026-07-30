"""Claim pending column transcripts for one issue.

For a given YYYY-MM-DD, walk every page's columns (sourced from
file_assets in mvtm.db), download each column PNG from Google Drive,
hash it, insert a stub row into ``column_transcripts`` (status='claimed')
and write a ticket file under ``transcribe/work/columns/<id>.json``
that contains everything an agent needs to produce the transcript.

Column PNGs are downloaded to:
    transcribe/work/downloads/<YYYY-MM-DD>/p<N>/<filename>

and cached: re-running claim on the same issue skips the network fetch
if the PNG already exists at that path.

Idempotent: a column whose PNG SHA-256 matches an existing row is
skipped — re-running picks up only what's new (or has been re-cut).

Usage::

    python3 -m transcribe.claim_columns 1892-01-01
    python3 -m transcribe.claim_columns 1892-01-01 --page 1
    python3 -m transcribe.claim_columns 1892-01-01 --limit 6

The output to stdout summarises how many tickets were written, how
many were skipped (already done), and where the ticket files live.
The agent loop is the next stage; this script does no LLM work.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

from . import db as _db
from . import download as _dl
from . import slice as _slice


WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "columns")
SLICES_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "slices")

AGENT_FILE_REL = ".claude/agents/column-transcriber.md"


def read_agent_instructions(agent_file_path: str) -> str:
    """Return the agent body with YAML frontmatter stripped."""
    with open(agent_file_path) as f:
        text = f.read()
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    body_start = text.find("\n", end + 4)
    if body_start == -1:
        return ""
    return text[body_start + 1:]


def parse_date(s: str) -> tuple[int, int, int]:
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(f"Expected YYYY-MM-DD, got {s!r}")
    y, m, d = (int(p) for p in parts)
    return y, m, d


def col_idx_from_filename(filename: str) -> int | None:
    """Extract 0-based col_idx from a filename like '1892-01-01-01_col3.png'.

    The filename convention is ``…_colN.png`` where N is 1-based.
    Returns None if the pattern is not found.
    """
    m = re.search(r"_col(\d+)\.png$", filename, re.IGNORECASE)
    if m is None:
        return None
    return int(m.group(1)) - 1  # convert to 0-based


def overlap_width(a_left: float, a_right: float,
                  b_left: float, b_right: float) -> float:
    """Width of horizontal overlap between two intervals (0 if none)."""
    return max(0.0, min(a_right, b_right) - max(a_left, b_left))


_AD_COLUMN_COVERAGE_MIN = 0.5


def build_ticket(*,
                 row_id: str,
                 year: int, month: int, day: int, page: int,
                 col_idx: int, num_columns: int,
                 boundary_positions: list[float],
                 ads_on_page: list[dict],
                 image_path_rel: str,
                 drive_id: str,
                 drive_url: str,
                 image_sha256: str,
                 prompt_template_text: str) -> dict:
    """Assemble the per-column ticket dict.

    H-rules are not currently stored in mvtm.db (they lived in the
    now-removed local page_analysis.json). The slicer falls back to
    height-based subdivision only, which is correct behaviour.
    """
    col_left = boundary_positions[col_idx]
    col_right = boundary_positions[col_idx + 1]

    col_width = col_right - col_left
    ads_in_col = []
    for ad in ads_on_page:
        if col_width <= 0:
            continue
        ow = overlap_width(
            ad["x_pct"], ad["x_end_pct"], col_left, col_right)
        coverage = ow / col_width
        if coverage >= _AD_COLUMN_COVERAGE_MIN:
            ads_in_col.append({
                "uuid": ad["uuid"],
                "x_pct": ad["x_pct"],
                "x_end_pct": ad["x_end_pct"],
                "y_pct": ad["y_pct"],
                "y_end_pct": ad["y_end_pct"],
                "cols": ad["cols"],
                "column_coverage": round(coverage, 3),
            })

    context = {
        "issue": {"year": year, "month": month, "day": day},
        "page": page,
        "col_idx": col_idx,
        "num_columns": num_columns,
        "column_position": {
            "left_pct": col_left,
            "right_pct": col_right,
            "width_pct": round(col_right - col_left, 2),
        },
        "h_rules_in_column": [],
        "ads_in_column": ads_in_col,
    }

    image_path_abs = os.path.join(_db.REPO_ROOT, image_path_rel)
    slice_out_dir = os.path.join(SLICES_DIR, row_id)
    slices = _slice.slice_column(
        image_path=image_path_abs,
        column_position=context["column_position"],
        h_rules=[],
        out_dir=slice_out_dir,
        repo_root=_db.REPO_ROOT)

    ticket = dict(context)
    ticket.update({
        "row_id": row_id,
        "image_path": image_path_rel,
        "drive_id": drive_id,
        "drive_url": drive_url,
        "image_sha256": image_sha256,
        "prompt_hash": _db.prompt_hash(prompt_template_text, context),
        "slices": slices,
    })
    return ticket


def load_columns_from_db(conn: sqlite3.Connection,
                         year: int, month: int, day: int,
                         page: int) -> list[dict]:
    """Return all column assets for a page from file_assets, sorted by col_idx.

    Each record is a dict with: filename, local_path, drive_id, drive_url.
    Records whose filename doesn't match the colN pattern are skipped.
    """
    rows = conn.execute(
        """SELECT filename, local_path, drive_id, drive_url
             FROM mvtm.file_assets
            WHERE kind='column'
              AND year=? AND month=? AND day=? AND page=?
            ORDER BY filename""",
        (year, month, day, page)).fetchall()

    columns = []
    for r in rows:
        idx = col_idx_from_filename(r["filename"])
        if idx is None:
            continue
        columns.append({
            "col_idx": idx,
            "filename": r["filename"],
            "local_path": r["local_path"],
            "drive_id": r["drive_id"],
            "drive_url": r["drive_url"],
        })

    columns.sort(key=lambda c: c["col_idx"])
    return columns


def claim_for_page(conn: sqlite3.Connection,
                   *,
                   year: int, month: int, day: int, page: int,
                   prompt_template_text: str) -> tuple[int, int]:
    """Claim every column on one page; returns (written, skipped)."""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    layout = conn.execute(
        """SELECT num_columns, boundary_positions
             FROM mvtm.page_layouts
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchone()
    if layout is None:
        print(f"  p{page}: no page_layouts row, skipping")
        return 0, 0

    num_columns = layout["num_columns"]
    boundary_positions = json.loads(layout["boundary_positions"])

    ads_rows = conn.execute(
        """SELECT uuid, x_pct, y_pct, w_pct, h_pct,
                  x_end_pct, y_end_pct, cols
             FROM mvtm.detected_ads
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchall()
    ads_on_page = [dict(r) for r in ads_rows]

    columns = load_columns_from_db(conn, year, month, day, page)
    if not columns:
        print(f"  p{page}: no columns in file_assets, skipping")
        return 0, 0

    written = 0
    skipped = 0

    for col in columns:
        col_idx = col["col_idx"]
        filename = col["filename"]
        drive_id = col["drive_id"]
        drive_url = col["drive_url"]
        local_path_rel = col["local_path"]  # e.g. columns/1861-06-28/p1/...

        # Download from Drive (cached; no network hit if already present).
        dest_path = _dl.local_cache_path(
            _db.REPO_ROOT, date_str, page, filename)
        try:
            already_cached = os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0
            print(f"    col{col_idx}: downloading … ", end="", flush=True)
            sha = _dl.download_column(drive_id, dest_path)
            print("cached" if already_cached else "ok")
        except Exception as exc:
            print(f"FAILED ({exc})")
            continue

        existing = conn.execute(
            """SELECT id, status FROM column_transcripts
                WHERE year=? AND month=? AND day=? AND page=?
                  AND col_idx=? AND image_sha256=?""",
            (year, month, day, page, col_idx, sha)).fetchone()
        if existing is not None and existing["status"] == "done":
            skipped += 1
            continue

        # image_path_rel is the local download cache path so agents and
        # the slicer can read the file via the filesystem.
        image_path_rel = os.path.relpath(dest_path, _db.REPO_ROOT)

        row_id = _db.claim_column(
            conn,
            year=year, month=month, day=day, page=page,
            col_idx=col_idx,
            image_path=local_path_rel,  # canonical path in DB (mirrors archive)
            image_sha256=sha)

        ticket = build_ticket(
            row_id=row_id,
            year=year, month=month, day=day, page=page,
            col_idx=col_idx, num_columns=num_columns,
            boundary_positions=boundary_positions,
            ads_on_page=ads_on_page,
            image_path_rel=image_path_rel,
            drive_id=drive_id,
            drive_url=drive_url,
            image_sha256=sha,
            prompt_template_text=prompt_template_text)

        ticket_path = os.path.join(WORK_DIR, f"{row_id}.json")
        os.makedirs(WORK_DIR, exist_ok=True)
        with open(ticket_path, "w") as f:
            json.dump(ticket, f, indent=2, ensure_ascii=False)

        written += 1

    return written, skipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Claim pending column transcripts for one issue.")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--page", type=int, default=None,
                   help="Only claim this page (default: all pages)")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after writing this many tickets")
    args = p.parse_args(argv)

    year, month, day = parse_date(args.date)

    agent_path = os.path.join(_db.REPO_ROOT, AGENT_FILE_REL)
    prompt_template_text = read_agent_instructions(agent_path)

    conn = _db.open_connection(attach_mvtm=True)
    try:
        if args.page is not None:
            pages = [args.page]
        else:
            rows = conn.execute(
                """SELECT DISTINCT page FROM mvtm.file_assets
                    WHERE kind='column' AND year=? AND month=? AND day=?
                 ORDER BY page""",
                (year, month, day)).fetchall()
            pages = [r["page"] for r in rows]

        if not pages:
            print(f"No column assets found for {args.date} in file_assets")
            return 1

        print(f"Claiming columns for {args.date} ({len(pages)} page(s))")

        total_written = 0
        total_skipped = 0
        for page in pages:
            print(f"  page {page}:")
            w, s = claim_for_page(
                conn,
                year=year, month=month, day=day, page=page,
                prompt_template_text=prompt_template_text)
            print(f"    wrote {w} ticket(s), skipped {s} already-done")
            total_written += w
            total_skipped += s
            if args.limit is not None and total_written >= args.limit:
                print(f"  reached --limit {args.limit}, stopping")
                break
    finally:
        conn.close()

    print(f"\nDone. Wrote {total_written} ticket(s); "
          f"skipped {total_skipped} already-done. "
          f"Tickets in {WORK_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
