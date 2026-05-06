"""Claim a page for the pass-3 items-tidier run.

For one (year, month, day, page) where pass-2 items already exist,
assemble the per-page ticket the items-tidier agent needs:

- the latest pass-2 items list (id, item_type, headline, summary,
  bbox, column_spans, classification_confidence, etc.)
- the page's registered ads (uuid, bbox, transcript excerpt)
- page_state (boundary positions, page geometry)
- the source pass-2 prompt_hash (so the ingester can verify the
  edit envelope still applies to the batch we tidied)

Pass-3 input is intentionally small: no column transcripts, no
full ad transcripts. The tidier reasons from pass-2's editorial
output, not from the raw page.

Idempotent: if pass-3 rows already exist on this page tagged with
the prompt_hash this ticket would produce, the page is treated as
done and the claim is a no-op. Re-running after the pass-2 batch
or the items-tidier agent prompt has changed produces a fresh
ticket — its prompt_hash differs, so the new pass-3 batch lands
alongside the old one (distinguishable by prompt_hash and
derived_from_item_ids).

Usage::

    python3 -m transcribe.claim_items_tidy 1912-12-27 --page 3
    python3 -m transcribe.claim_items_tidy 1912-12-27          # all pages
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import db as _db


WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "items_tidy")
AGENT_FILE_REL = ".claude/agents/items-tidier.md"

# Length of the per-ad transcript excerpt the tidier sees. Long
# enough to recognise the ad (vendor + product), short enough to
# keep the ticket compact.
AD_EXCERPT_CHARS = 200


def parse_date(s: str) -> tuple[int, int, int]:
    parts = s.split("-")
    if len(parts) != 3:
        raise ValueError(f"Expected YYYY-MM-DD, got {s!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_agent_instructions(agent_file_path: str) -> str:
    """Strip YAML frontmatter and return the durable agent body."""
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


def latest_pass2_batch(conn: sqlite3.Connection,
                       year: int, month: int, day: int,
                       page: int) -> str | None:
    """Return the prompt_hash of the most-recent pass-2 batch on
    this page. Pass-2 rows have ``derived_from_item_ids IS NULL``
    (pass-3 rows always set it).
    """
    row = conn.execute(
        """SELECT prompt_hash, MAX(created_at) AS t
             FROM items
            WHERE year=? AND month=? AND day=? AND page=?
              AND derived_from_item_ids IS NULL
            GROUP BY prompt_hash
            ORDER BY t DESC
            LIMIT 1""",
        (year, month, day, page)).fetchone()
    return row["prompt_hash"] if row is not None else None


def load_pass2_items(conn: sqlite3.Connection,
                     year: int, month: int, day: int, page: int,
                     pass2_prompt_hash: str) -> list[dict]:
    """Return the pass-2 items for one page with their column
    spans and ad associations attached. Sorted by reading order
    (top-to-bottom within left-to-right column groups).
    """
    item_rows = conn.execute(
        """SELECT id, item_type, headline, byline, summary,
                  bbox_left_pct, bbox_top_pct,
                  bbox_right_pct, bbox_bottom_pct,
                  column_span_json, crosses_columns, is_inset,
                  classification_confidence, repair_needed,
                  repair_reason, language
             FROM items
            WHERE year=? AND month=? AND day=? AND page=?
              AND prompt_hash=?
              AND derived_from_item_ids IS NULL
            ORDER BY bbox_left_pct, bbox_top_pct""",
        (year, month, day, page, pass2_prompt_hash)).fetchall()

    items: list[dict] = []
    for r in item_rows:
        spans = conn.execute(
            """SELECT s.column_transcript_id, s.sequence,
                      s.start_offset, s.end_offset,
                      c.col_idx
                 FROM item_column_spans s
                 JOIN column_transcripts c
                   ON c.id = s.column_transcript_id
                WHERE s.item_id=?
                ORDER BY s.sequence""",
            (r["id"],)).fetchall()
        ad_uuids = [
            a["ad_uuid"] for a in conn.execute(
                "SELECT ad_uuid FROM item_ad_associations "
                "WHERE item_id=? ORDER BY ad_uuid",
                (r["id"],)).fetchall()
        ]
        items.append({
            "item_id":   r["id"],
            "item_type": r["item_type"],
            "headline":  r["headline"],
            "byline":    r["byline"],
            "summary":   r["summary"],
            "language":  r["language"],
            "bbox_pct": {
                "left":   r["bbox_left_pct"],
                "top":    r["bbox_top_pct"],
                "right":  r["bbox_right_pct"],
                "bottom": r["bbox_bottom_pct"],
            },
            "column_spans": [
                {
                    "column_transcript_id": s["column_transcript_id"],
                    "col_idx":              s["col_idx"],
                    "sequence":             s["sequence"],
                    "start_offset":         s["start_offset"],
                    "end_offset":           s["end_offset"],
                }
                for s in spans
            ],
            "ad_uuids":                  ad_uuids,
            "is_inset":                  bool(r["is_inset"]),
            "crosses_columns":           bool(r["crosses_columns"]),
            "classification_confidence": r["classification_confidence"],
            "repair_needed":             bool(r["repair_needed"]),
            "repair_reason":             r["repair_reason"],
        })
    return items


def load_ads_for_page(conn: sqlite3.Connection,
                      year: int, month: int, day: int,
                      page: int) -> list[dict]:
    """Return registered ads on this page with bboxes and the
    most-recent ad_transcripts row's text excerpt.
    """
    rows = conn.execute(
        """SELECT a.uuid AS ad_uuid,
                  a.x_pct, a.y_pct, a.x_end_pct, a.y_end_pct,
                  a.w_pct, a.h_pct, a.cols
             FROM mvtm.detected_ads a
            WHERE a.year=? AND a.month=? AND a.day=? AND a.page=?
            ORDER BY a.y_pct, a.x_pct""",
        (year, month, day, page)).fetchall()
    ads: list[dict] = []
    for r in rows:
        # Pull the latest 'done' transcript excerpt for this ad.
        t = conn.execute(
            """SELECT transcript_text
                 FROM ad_transcripts
                WHERE ad_uuid=? AND status='done'
                ORDER BY created_at DESC
                LIMIT 1""",
            (r["ad_uuid"],)).fetchone()
        excerpt = (t["transcript_text"] or "") if t is not None else ""
        if len(excerpt) > AD_EXCERPT_CHARS:
            excerpt = excerpt[:AD_EXCERPT_CHARS].rstrip() + "…"
        ads.append({
            "ad_uuid": r["ad_uuid"],
            "bbox_pct": {
                "x_pct":     r["x_pct"],
                "y_pct":     r["y_pct"],
                "x_end_pct": r["x_end_pct"],
                "y_end_pct": r["y_end_pct"],
                "w_pct":     r["w_pct"],
                "h_pct":     r["h_pct"],
            },
            "cols_spanned":      r["cols"],
            "transcript_excerpt": excerpt,
        })
    return ads


def load_page_state(conn: sqlite3.Connection,
                    year: int, month: int, day: int,
                    page: int) -> dict | None:
    layout = conn.execute(
        """SELECT num_columns, boundary_positions
             FROM mvtm.page_layouts
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchone()
    if layout is None:
        return None
    state = {
        "num_columns":         layout["num_columns"],
        "boundary_positions":  json.loads(layout["boundary_positions"]),
    }
    geom = conn.execute(
        """SELECT text_left, text_right, binding_side
             FROM mvtm.page_geometry
            WHERE year=? AND month=? AND day=? AND page=?""",
        (year, month, day, page)).fetchone()
    if geom is not None:
        state["page_geometry"] = {
            "text_left":    geom["text_left"],
            "text_right":   geom["text_right"],
            "binding_side": geom["binding_side"],
        }
    return state


def page_tidied(conn: sqlite3.Connection,
                year: int, month: int, day: int, page: int,
                planned_prompt_hash: str) -> bool:
    """True if pass-3 rows already exist on this page tagged with
    the prompt_hash this claim would emit. Pass-3 rows always have
    ``derived_from_item_ids`` non-null.
    """
    row = conn.execute(
        """SELECT 1
             FROM items
            WHERE year=? AND month=? AND day=? AND page=?
              AND prompt_hash=?
              AND derived_from_item_ids IS NOT NULL
            LIMIT 1""",
        (year, month, day, page, planned_prompt_hash)).fetchone()
    return row is not None


def build_ticket(*, year: int, month: int, day: int, page: int,
                 pass2_items: list[dict],
                 ads: list[dict],
                 page_state: dict,
                 pass2_prompt_hash: str,
                 prompt_template_text: str) -> dict:
    context = {
        "issue":             {"year": year, "month": month, "day": day},
        "page":              page,
        "page_state":        page_state,
        "pass2_items":       pass2_items,
        "ads":               ads,
        "pass2_prompt_hash": pass2_prompt_hash,
    }
    ticket = dict(context)
    ticket.update({
        "agent_file_path": AGENT_FILE_REL,
        "prompt_hash":     _db.prompt_hash(prompt_template_text, context),
    })
    return ticket


def claim_for_page(conn: sqlite3.Connection,
                   *, year: int, month: int, day: int, page: int,
                   prompt_template_text: str,
                   pass2_prompt_hash: str | None = None,
                   force: bool = False) -> tuple[bool, str | None]:
    """Claim one page. Returns (wrote_ticket, ticket_path_or_None).

    Skips when:
      - no pass-2 batch exists for this page → not ready
      - pass-3 rows already exist with the prompt_hash this claim
        would emit → already done (bypass with ``force=True``)
    """
    if pass2_prompt_hash is None:
        pass2_prompt_hash = latest_pass2_batch(conn, year, month, day, page)
    if pass2_prompt_hash is None:
        return False, None  # no pass-2 yet

    pass2_items = load_pass2_items(
        conn, year, month, day, page, pass2_prompt_hash)
    if not pass2_items:
        return False, None

    page_state = load_page_state(conn, year, month, day, page)
    if page_state is None:
        return False, None

    ads = load_ads_for_page(conn, year, month, day, page)

    ticket = build_ticket(
        year=year, month=month, day=day, page=page,
        pass2_items=pass2_items,
        ads=ads,
        page_state=page_state,
        pass2_prompt_hash=pass2_prompt_hash,
        prompt_template_text=prompt_template_text)

    if not force and page_tidied(
            conn, year, month, day, page, ticket["prompt_hash"]):
        return False, None

    os.makedirs(WORK_DIR, exist_ok=True)
    page_id = f"{year:04d}-{month:02d}-{day:02d}_p{page}"
    ticket_path = os.path.join(WORK_DIR, f"{page_id}.json")
    with open(ticket_path, "w") as f:
        json.dump(ticket, f, indent=2, ensure_ascii=False)
    return True, ticket_path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Claim a page for the pass-3 items-tidier run.")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--page", type=int, default=None,
                   help="Only claim this page (default: every page "
                        "with pass-2 items)")
    p.add_argument("--pass2-prompt-hash", default=None,
                   help="Pin the pass-2 batch to tidy by its "
                        "prompt_hash. Default: the most recent "
                        "pass-2 batch on this page.")
    p.add_argument("--force", action="store_true",
                   help="Re-claim even if a pass-3 batch with the "
                        "same prompt_hash already exists.")
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
                """SELECT DISTINCT page
                     FROM items
                    WHERE year=? AND month=? AND day=?
                      AND derived_from_item_ids IS NULL
                 ORDER BY page""",
                (year, month, day)).fetchall()
            pages = [r["page"] for r in rows]

        if not pages:
            print("no pages with pass-2 items; run pass-2 first.")
            return 0

        wrote = 0
        skipped = 0
        for page in pages:
            ok, path = claim_for_page(
                conn,
                year=year, month=month, day=day, page=page,
                prompt_template_text=prompt_template_text,
                pass2_prompt_hash=args.pass2_prompt_hash,
                force=args.force)
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
