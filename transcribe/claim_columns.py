"""Claim pending column transcripts for one issue.

For a given YYYY-MM-DD, walk every page's columns, hash each column
PNG, insert a stub row into ``column_transcripts`` (status='claimed')
and write a ticket file under ``transcribe/work/columns/<id>.json``
that contains everything an agent needs to produce the transcript.

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
import sqlite3
import sys

from . import db as _db
from . import slice as _slice


WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "columns")
SLICES_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "slices")

# The column-transcriber agent definition is the source of truth for
# the durable instructions a transcriber follows. The agent file's
# YAML frontmatter sets the default model; its body holds the
# instructions. We hash the body (not the frontmatter) plus the
# per-call context to derive prompt_hash, so the design fingerprint
# is stable across model overrides.
AGENT_FILE_REL = ".claude/agents/column-transcriber.md"


def read_agent_instructions(agent_file_path: str) -> str:
    """Return the agent body with YAML frontmatter stripped.

    The frontmatter is delimited by a leading ``---`` line and a
    matching closing ``---``. The model and tool routing live there
    and are captured separately on each row, so they don't belong in
    the prompt-hash. Everything after the closing ``---`` is the
    durable transcription instructions.
    """
    with open(agent_file_path) as f:
        text = f.read()
    if not text.startswith("---"):
        return text
    # Find the closing fence after the opening one.
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


def overlap_width(a_left: float, a_right: float,
                  b_left: float, b_right: float) -> float:
    """Width of horizontal overlap between two intervals (0 if none)."""
    return max(0.0, min(a_right, b_right) - max(a_left, b_left))


# An ad is treated as "registered to" a column when it covers at
# least this fraction of the column's width. detected_ads carries
# `cols` (a count of how many columns the ad spans) but no explicit
# column index, so we infer registration from x extents against the
# column boundaries. A 50% rule cleanly separates real spans from
# measurement bleed — for example, an ad at x=26.37 against a column
# whose right edge is 26.54 covers only 1.3% of that column's width
# (0.17pct of page-width / 13.49pct column), which is rounding noise
# from the detector, not a real intrusion. Multi-column ads cover
# ~100% of each interior column, so the same threshold catches them.
_AD_COLUMN_COVERAGE_MIN = 0.5


def load_page_meta(date_str: str, page: int) -> dict:
    path = os.path.join(_db.REPO_ROOT, "columns", date_str,
                        f"p{page}", "page_meta.json")
    with open(path) as f:
        return json.load(f)


def load_h_rules_for_page(date_str: str, page: int) -> list[dict]:
    """Read just the h_rules list from page_analysis.json.

    page_analysis.json is huge (the row/col debug charts dominate),
    so we load it fully but only keep the small h_rules list. If the
    file gets bigger we can switch to a streaming parser; today this
    is fine.
    """
    path = os.path.join(_db.REPO_ROOT, "columns", date_str,
                        f"p{page}", "page_analysis.json")
    with open(path) as f:
        data = json.load(f)
    return data.get("h_rules", [])


def build_ticket(*,
                 row_id: str,
                 year: int, month: int, day: int, page: int,
                 col_idx: int, num_columns: int,
                 boundary_positions: list[float],
                 h_rules: list[dict],
                 ads_on_page: list[dict],
                 image_path_rel: str,
                 image_sha256: str,
                 prompt_template_text: str) -> dict:
    """Assemble the per-column ticket dict.

    Coordinates here are page-percentages. The ticket is the
    self-contained brief the agent reads — it doesn't need to query
    mvtm.db itself.
    """
    col_left = boundary_positions[col_idx]
    col_right = boundary_positions[col_idx + 1]

    # H-rules in this column. The detector tags each rule with col_idx
    # already, but we also clip any that nominally span this column.
    h_in_col = []
    for r in h_rules:
        if r.get("col_idx") == col_idx:
            h_in_col.append({
                "y_pct": r.get("y_pct"),
                "x1_pct": r.get("x1_pct"),
                "x2_pct": r.get("x2_pct"),
                "strength": r.get("strength"),
            })

    # Ads that overlap this column horizontally, listed with their
    # vertical extent so the agent knows which slabs of the column
    # have been masked out.
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
        "h_rules_in_column": h_in_col,
        "ads_in_column": ads_in_col,
    }

    # Slice the column PNG at h-rules. The slicer writes slice PNGs
    # under transcribe/work/slices/<row-id>/ and returns a manifest;
    # we put the manifest on the ticket so the orchestrator dispatches
    # one agent call per slice. Phase A confirmed slicing eliminates
    # the downsampling-driven fabrication failure mode (see
    # transcribe/work/experiments.jsonl).
    image_path_abs = os.path.join(_db.REPO_ROOT, image_path_rel)
    slice_out_dir = os.path.join(SLICES_DIR, row_id)
    slices = _slice.slice_column(
        image_path=image_path_abs,
        column_position=context["column_position"],
        h_rules=h_in_col,
        out_dir=slice_out_dir,
        repo_root=_db.REPO_ROOT)

    ticket = dict(context)
    ticket.update({
        "row_id": row_id,
        "image_path": image_path_rel,
        "image_sha256": image_sha256,
        "prompt_hash": _db.prompt_hash(prompt_template_text, context),
        "slices": slices,
    })
    return ticket


def claim_for_page(conn: sqlite3.Connection,
                   *,
                   year: int, month: int, day: int, page: int,
                   prompt_template_text: str) -> tuple[int, int]:
    """Claim every column on one page; returns (written, skipped)."""
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    # Page-level state from mvtm (read-only via ATTACH).
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

    # Per-page on-disk state.
    try:
        page_meta = load_page_meta(date_str, page)
    except FileNotFoundError:
        print(f"  p{page}: no page_meta.json, skipping")
        return 0, 0

    try:
        h_rules = load_h_rules_for_page(date_str, page)
    except FileNotFoundError:
        h_rules = []

    written = 0
    skipped = 0

    for col_meta in page_meta.get("columns", []):
        col_idx = col_meta["index"]
        image_path_rel = col_meta["image_path"]
        image_path_abs = os.path.join(_db.REPO_ROOT, image_path_rel)

        if not os.path.isfile(image_path_abs):
            print(f"    col{col_idx}: missing PNG at {image_path_rel}")
            continue

        sha = _db.sha256_file(image_path_abs)

        # Already done for this exact image content?
        existing = conn.execute(
            """SELECT id, status FROM column_transcripts
                WHERE year=? AND month=? AND day=? AND page=?
                  AND col_idx=? AND image_sha256=?""",
            (year, month, day, page, col_idx, sha)).fetchone()
        if existing is not None and existing["status"] == "done":
            skipped += 1
            continue

        row_id = _db.claim_column(
            conn,
            year=year, month=month, day=day, page=page,
            col_idx=col_idx,
            image_path=image_path_rel,
            image_sha256=sha)

        ticket = build_ticket(
            row_id=row_id,
            year=year, month=month, day=day, page=page,
            col_idx=col_idx, num_columns=num_columns,
            boundary_positions=boundary_positions,
            h_rules=h_rules,
            ads_on_page=ads_on_page,
            image_path_rel=image_path_rel,
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
                """SELECT page FROM mvtm.page_layouts
                    WHERE year=? AND month=? AND day=?
                 ORDER BY page""",
                (year, month, day)).fetchall()
            pages = [r["page"] for r in rows]

        if not pages:
            print(f"No pages found for {args.date} in mvtm.page_layouts")
            return 1

        print(f"Claiming columns for {args.date} "
              f"({len(pages)} page(s))")

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
