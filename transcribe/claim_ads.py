"""Claim pending ad transcripts for one issue.

For a given YYYY-MM-DD, walk every registered ad in
``mvtm.detected_ads``, hash each ad PNG, insert a stub row into
``ad_transcripts`` (status='claimed') and write a ticket file
under ``transcribe/work/ads/<id>.json`` that contains everything
an agent needs to produce the transcript.

Idempotent: an ad whose PNG SHA-256 matches an existing row is
skipped — re-running picks up only what's new (or has been
re-cut).

Usage::

    python3 -m transcribe.claim_ads 1892-01-01
    python3 -m transcribe.claim_ads 1892-01-01 --page 1
    python3 -m transcribe.claim_ads 1892-01-01 --limit 6

The output to stdout summarises how many tickets were written,
how many were skipped (already done), and where the ticket files
live. The agent loop is the next stage; this script does no LLM
work.

Design note — ad recurrence (apply when wiring pass-1B end-to-end)
-----------------------------------------------------------------
A lot of ads in this corpus recur week-to-week. The ad-transcriber
should not be asked to transcribe each one in isolation; the LLM is
exactly the right tool to compare a new ad image against a prior
transcript and answer one of three questions:

1. **Identical reuse** — same printed plate. Response collapses to
   ``same_as: <prior_transcript_id>``; transcript carries over.
2. **Same template, swapped contents** — grocery weekly specials,
   theatre playbills, auction lists. Prior transcript becomes a
   scaffold; agent marks which slots changed. The diff (new prices,
   new show titles) is itself useful metadata.
3. **Same vendor, fresh variant** — agent treats it as new but is
   given the prior vendor name as a soft consistency hint to avoid
   spelling drift across issues ("McLEOD & SON" vs "McLeod & Son").

When this lands the ticket grows a "prior transcripts" block, and
the agent response envelope grows two optional fields: ``same_as``
and ``changes_from_prior``.

How "the prior" is identified — three options in order of cost:

- **Vendor-name key** (cheapest): once any transcript names a
  vendor, later same-vendor ads inherit it as a soft prior. Works
  after a handful of issues are transcribed in date order.
- **Phase 1 cluster IDs** from the (paused) recurrence_lab if they
  exist in ``mvtm.db`` — without re-running embeddings.
- **Perceptual hash** on the cropped ad PNG matched against priors.

Bootstrap by date order: transcribe earliest issues first so later
issues get richer priors automatically. Do NOT re-run DINOv2 or any
recurrence_lab model — apply the insight, not the lab.

See ``project_ad_recurrence_for_transcription.md`` in
~/.claude/projects/.../memory/ for the durable version of this note.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import db as _db


WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "ads")

# The ad-transcriber agent definition is the source of truth for
# the durable instructions a transcriber follows. Same prompt-hash
# logic as columns: hash the body (frontmatter stripped) plus the
# per-call context.
AGENT_FILE_REL = ".claude/agents/ad-transcriber.md"


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


def ad_image_path_rel(date_str: str, page: int, filename: str) -> str:
    """Repo-relative path to the ad PNG.

    Layout: ``columns/<date>/ads/p<page>/<image_filename>``. The
    filename comes from ``mvtm.detected_ads.image_filename``.
    """
    return os.path.join("columns", date_str, "ads", f"p{page}", filename)


def build_ticket(*,
                 row_id: str,
                 ad_uuid: str,
                 year: int, month: int, day: int, page: int,
                 ad_row: dict,
                 image_path_rel: str,
                 image_sha256: str,
                 prompt_template_text: str) -> dict:
    """Assemble the per-ad ticket dict.

    Coordinates are page-percentages — the agent doesn't need to
    do any pixel math, just sanity-check what it sees against the
    bounding box.
    """
    context = {
        "issue": {"year": year, "month": month, "day": day},
        "page": page,
        "ad_uuid": ad_uuid,
        "bbox_pct": {
            "x_pct":     ad_row["x_pct"],
            "y_pct":     ad_row["y_pct"],
            "x_end_pct": ad_row["x_end_pct"],
            "y_end_pct": ad_row["y_end_pct"],
            "w_pct":     ad_row["w_pct"],
            "h_pct":     ad_row["h_pct"],
        },
        "cols_spanned": ad_row["cols"],
        "confidence": ad_row.get("confidence"),
    }

    ticket = dict(context)
    ticket.update({
        "row_id": row_id,
        "image_path": image_path_rel,
        "image_sha256": image_sha256,
        "agent_file_path": AGENT_FILE_REL,
        "prompt_hash": _db.prompt_hash(prompt_template_text, context),
    })
    return ticket


def claim_for_issue(conn: sqlite3.Connection,
                    *,
                    year: int, month: int, day: int,
                    page: int | None,
                    limit: int | None,
                    prompt_template_text: str) -> tuple[int, int, int]:
    """Claim ads for the issue (or one page). Returns
    (written, skipped, missing_png).
    """
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    where = ["year=?", "month=?", "day=?"]
    params: list = [year, month, day]
    if page is not None:
        where.append("page=?")
        params.append(page)
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"""SELECT uuid, page, x_pct, y_pct, w_pct, h_pct,
                   x_end_pct, y_end_pct, cols, confidence,
                   image_filename
              FROM mvtm.detected_ads
             WHERE {where_sql}
          ORDER BY page, y_pct, x_pct""",
        params).fetchall()

    if not rows:
        return 0, 0, 0

    written = 0
    skipped = 0
    missing_png = 0

    for r in rows:
        ad_row = dict(r)
        ad_uuid = ad_row["uuid"]
        if not ad_uuid:
            print(f"    skipping ad on p{ad_row['page']} with no uuid")
            continue
        filename = ad_row.get("image_filename")
        if not filename:
            print(f"    ad {ad_uuid[:8]}…: no image_filename, skipping")
            continue

        image_path_rel = ad_image_path_rel(
            date_str, ad_row["page"], filename)
        image_path_abs = os.path.join(_db.REPO_ROOT, image_path_rel)

        if not os.path.isfile(image_path_abs):
            print(f"    ad {ad_uuid[:8]}…: missing PNG at {image_path_rel}")
            missing_png += 1
            continue

        sha = _db.sha256_file(image_path_abs)

        existing = conn.execute(
            """SELECT id, status FROM ad_transcripts
                WHERE ad_uuid=? AND image_sha256=?""",
            (ad_uuid, sha)).fetchone()
        if existing is not None and existing["status"] == "done":
            skipped += 1
            continue

        row_id = _db.claim_ad(
            conn,
            ad_uuid=ad_uuid,
            year=year, month=month, day=day, page=ad_row["page"],
            image_path=image_path_rel,
            image_sha256=sha)

        ticket = build_ticket(
            row_id=row_id,
            ad_uuid=ad_uuid,
            year=year, month=month, day=day, page=ad_row["page"],
            ad_row=ad_row,
            image_path_rel=image_path_rel,
            image_sha256=sha,
            prompt_template_text=prompt_template_text)

        ticket_path = os.path.join(WORK_DIR, f"{row_id}.json")
        os.makedirs(WORK_DIR, exist_ok=True)
        with open(ticket_path, "w") as f:
            json.dump(ticket, f, indent=2, ensure_ascii=False)

        written += 1
        if limit is not None and written >= limit:
            break

    return written, skipped, missing_png


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Claim pending ad transcripts for one issue.")
    p.add_argument("date", help="YYYY-MM-DD")
    p.add_argument("--page", type=int, default=None,
                   help="Only claim ads on this page (default: all pages)")
    p.add_argument("--limit", type=int, default=None,
                   help="Stop after writing this many tickets")
    args = p.parse_args(argv)

    year, month, day = parse_date(args.date)

    agent_path = os.path.join(_db.REPO_ROOT, AGENT_FILE_REL)
    prompt_template_text = read_agent_instructions(agent_path)

    conn = _db.open_connection(attach_mvtm=True)
    try:
        scope = f"{args.date}" + (f" p{args.page}" if args.page else "")
        print(f"Claiming ads for {scope}")
        written, skipped, missing = claim_for_issue(
            conn,
            year=year, month=month, day=day,
            page=args.page,
            limit=args.limit,
            prompt_template_text=prompt_template_text)
        print(f"  wrote {written} ticket(s); "
              f"skipped {skipped} already-done; "
              f"missing {missing} PNG(s)")
    finally:
        conn.close()

    print(f"\nDone. Tickets in {WORK_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
