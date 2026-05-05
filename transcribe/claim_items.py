"""Claim a page for the pass-2 items run.

For one (year, month, day, page) where pass-1A and pass-1B are done,
assemble the per-page ticket the items-classifier agent needs:

- page state (boundary positions, page geometry)
- every column transcript on the page (id, col_idx, text,
  slice_boundaries) — slice markers stay inline in transcript_text
  as ``---`` / ``--`` so they double as item-boundary hints
- every ad transcript on the page (id, ad_uuid, bbox, text)
- the page-level h_rules list (context only — bbox derivation is
  done by the ingester, not the agent)

Idempotent: if any ``items`` rows already exist for this (page,
content_hash) where content_hash = sha256 of the sorted column +
ad transcript ids, the page is treated as done and the claim is a
no-op. Re-running after a column or ad has been re-transcribed
(new transcript ids) creates a fresh ticket — items go to a new
batch tagged with the new content_hash, and the prior batch stays
for history (no auto-merge).

Usage::

    python3 -m transcribe.claim_items 1912-12-27 --page 6
    python3 -m transcribe.claim_items 1912-12-27           # all done pages

The output to stdout summarises how many tickets were written and
where they live. The agent dispatch is the next stage; this script
does no LLM work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys

from . import db as _db


WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "items")
AGENT_FILE_REL = ".claude/agents/items-classifier.md"


def parse_date(s: str) -> tuple[int, int, int]:
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(f"Expected YYYY-MM-DD, got {s!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_agent_instructions(agent_file_path: str) -> str:
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


def load_h_rules_for_page(date_str: str, page: int) -> list[dict]:
    path = os.path.join(_db.REPO_ROOT, "columns", date_str,
                        f"p{page}", "page_analysis.json")
    if not os.path.isfile(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("h_rules", []) or []


def content_hash(column_ids: list[str], ad_ids: list[str]) -> str:
    """Stable hash of which transcripts feed this items pass.

    If a column or ad is re-transcribed (new transcript row), the
    hash changes and we treat the page as needing a fresh items
    pass.
    """
    h = hashlib.sha256()
    for tid in sorted(column_ids):
        h.update(b"col:")
        h.update(tid.encode())
        h.update(b"\n")
    for tid in sorted(ad_ids):
        h.update(b"ad:")
        h.update(tid.encode())
        h.update(b"\n")
    return h.hexdigest()


def page_done(conn: sqlite3.Connection,
              year: int, month: int, day: int, page: int,
              chash: str) -> bool:
    """True if items already exist on this page tagged with this
    content_hash. Stored on items.notes as ``content_hash=<hex>``
    so we don't need a new schema column for first-cut idempotency.
    """
    rows = conn.execute(
        """SELECT notes FROM items
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchall()
    needle = f"content_hash={chash}"
    return any((r["notes"] or "").find(needle) >= 0 for r in rows)


def build_ticket(*, year: int, month: int, day: int, page: int,
                 page_layout: dict, page_geom: dict | None,
                 column_rows: list[dict], ad_rows: list[dict],
                 h_rules: list[dict],
                 prompt_template_text: str,
                 chash: str) -> dict:
    """Assemble the per-page items ticket."""
    columns = []
    for c in column_rows:
        slice_boundaries = c.get("slice_boundaries")
        if isinstance(slice_boundaries, str):
            try:
                slice_boundaries = json.loads(slice_boundaries)
            except json.JSONDecodeError:
                slice_boundaries = None
        columns.append({
            "column_transcript_id": c["id"],
            "col_idx": c["col_idx"],
            "transcript_text": c["transcript_text"] or "",
            "char_count": len(c["transcript_text"] or ""),
            "slice_boundaries": slice_boundaries,
        })

    ads = []
    for a in ad_rows:
        ads.append({
            "ad_transcript_id": a["id"],
            "ad_uuid": a["ad_uuid"],
            "bbox_pct": {
                "x_pct":     a["x_pct"],
                "y_pct":     a["y_pct"],
                "x_end_pct": a["x_end_pct"],
                "y_end_pct": a["y_end_pct"],
                "w_pct":     a["w_pct"],
                "h_pct":     a["h_pct"],
            },
            "cols_spanned": a["cols"],
            "transcript_text": a["transcript_text"] or "",
        })

    page_state = {
        "num_columns": page_layout["num_columns"],
        "boundary_positions": json.loads(page_layout["boundary_positions"]),
    }
    if page_geom is not None:
        page_state["page_geometry"] = {
            "text_left":    page_geom["text_left"],
            "text_right":   page_geom["text_right"],
            "binding_side": page_geom["binding_side"],
        }

    context = {
        "issue": {"year": year, "month": month, "day": day},
        "page": page,
        "page_state": page_state,
        "columns": columns,
        "ads": ads,
        "h_rules": h_rules,
    }

    ticket = dict(context)
    ticket.update({
        "content_hash": chash,
        "agent_file_path": AGENT_FILE_REL,
        "prompt_hash": _db.prompt_hash(prompt_template_text, context),
    })
    return ticket


def claim_for_page(conn: sqlite3.Connection,
                   *, year: int, month: int, day: int, page: int,
                   prompt_template_text: str) -> tuple[bool, str | None]:
    """Claim one page. Returns (wrote_ticket, ticket_path_or_None).

    Conditions for skipping:
      - no column transcripts in 'done' state on this page → not ready
      - items already exist for this content_hash → already done
    """
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    # Column transcripts: latest 'done' per col_idx (handles re-cuts).
    col_rows = conn.execute(
        """SELECT id, col_idx, transcript_text, slice_boundaries,
                  image_sha256, created_at
             FROM column_transcripts
            WHERE year=? AND month=? AND day=? AND page=? AND status='done'
         ORDER BY col_idx, created_at DESC""",
        (year, month, day, page)).fetchall()

    # De-dup: keep the most recent 'done' row per col_idx.
    seen_cols: set[int] = set()
    columns_dedup: list[dict] = []
    for r in col_rows:
        if r["col_idx"] in seen_cols:
            continue
        seen_cols.add(r["col_idx"])
        columns_dedup.append(dict(r))

    if not columns_dedup:
        return False, None

    # Ad transcripts: latest 'done' per ad_uuid (handles re-cuts).
    ad_t_rows = conn.execute(
        """SELECT t.id, t.ad_uuid, t.transcript_text, t.image_sha256,
                  t.created_at,
                  a.x_pct, a.y_pct, a.x_end_pct, a.y_end_pct,
                  a.w_pct, a.h_pct, a.cols
             FROM ad_transcripts t
             JOIN mvtm.detected_ads a ON a.uuid = t.ad_uuid
            WHERE t.year=? AND t.month=? AND t.day=? AND t.page=?
              AND t.status='done'
         ORDER BY t.ad_uuid, t.created_at DESC""",
        (year, month, day, page)).fetchall()

    seen_ads: set[str] = set()
    ads_dedup: list[dict] = []
    for r in ad_t_rows:
        if r["ad_uuid"] in seen_ads:
            continue
        seen_ads.add(r["ad_uuid"])
        ads_dedup.append(dict(r))

    chash = content_hash(
        [c["id"] for c in columns_dedup],
        [a["id"] for a in ads_dedup])

    if page_done(conn, year, month, day, page, chash):
        return False, None

    page_layout = conn.execute(
        """SELECT num_columns, boundary_positions
             FROM mvtm.page_layouts
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchone()
    if page_layout is None:
        return False, None

    page_geom = conn.execute(
        """SELECT text_left, text_right, binding_side
             FROM mvtm.page_geometry
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchone()

    h_rules = load_h_rules_for_page(date_str, page)

    ticket = build_ticket(
        year=year, month=month, day=day, page=page,
        page_layout=dict(page_layout),
        page_geom=dict(page_geom) if page_geom is not None else None,
        column_rows=columns_dedup,
        ad_rows=ads_dedup,
        h_rules=h_rules,
        prompt_template_text=prompt_template_text,
        chash=chash)

    os.makedirs(WORK_DIR, exist_ok=True)
    page_id = f"{year:04d}-{month:02d}-{day:02d}_p{page}"
    ticket_path = os.path.join(WORK_DIR, f"{page_id}.json")
    with open(ticket_path, "w") as f:
        json.dump(ticket, f, indent=2, ensure_ascii=False)

    return True, ticket_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Claim a page for the pass-2 items run.")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--page", type=int, default=None,
                   help="Only claim this page (default: every page where "
                        "pass-1A is done)")
    args = p.parse_args(argv)

    year, month, day = parse_date(args.date)

    agent_path = os.path.join(_db.REPO_ROOT, AGENT_FILE_REL)
    if not os.path.isfile(agent_path):
        print(f"agent file missing: {agent_path}", file=sys.stderr)
        return 1
    prompt_template_text = read_agent_instructions(agent_path)

    conn = _db.open_connection(attach_mvtm=True)
    try:
        if args.page is not None:
            pages = [args.page]
        else:
            rows = conn.execute(
                """SELECT DISTINCT page FROM column_transcripts
                    WHERE year=? AND month=? AND day=? AND status='done'
                 ORDER BY page""",
                (year, month, day)).fetchall()
            pages = [r["page"] for r in rows]

        if not pages:
            print("no pages with done column transcripts; "
                  "run pass-1A first.")
            return 0

        wrote = 0
        skipped = 0
        for page in pages:
            ok, path = claim_for_page(
                conn,
                year=year, month=month, day=day, page=page,
                prompt_template_text=prompt_template_text)
            if ok:
                print(f"  p{page}: ticket -> {path}")
                wrote += 1
            else:
                skipped += 1

        print(f"\nwrote {wrote} ticket(s); skipped {skipped}.")
        print(f"Tickets in {WORK_DIR}/")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
