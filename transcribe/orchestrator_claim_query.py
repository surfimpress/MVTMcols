"""Run directly by the orchestrator (Bash), not by a spawned agent.

For each given date: claim its columns (download + slice + DB stub),
then query the local transcribe.db for everything still not 'done',
including each ticket's slice count. Self-times both steps. Prints one
JSON object to stdout: {"per_date": {...}, "items": [...], "elapsed_s": {...}}
"""
import contextlib
import io
import json
import os
import sqlite3
import sys
import time

REPO_ROOT = "/Users/peter/Projects/MVTM"
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from transcribe import claim_columns as cc  # noqa: E402

TXC_DB = os.path.join(REPO_ROOT, "transcribe", "data", "transcribe.db")


def query_remaining(y, m, d):
    conn = sqlite3.connect(TXC_DB)
    conn.row_factory = sqlite3.Row
    # A re-cut column gets a NEW row (new image_sha256); the old row is
    # deliberately left in place for history (transcribe/CLAUDE.md's
    # "Re-cuts and history"). Only the latest row per
    # (year,month,day,page,col_idx) reflects current reality -- without
    # this filter, an orphaned pre-recut 'claimed' row would be treated
    # as real outstanding work and dispatched against a stale,
    # superseded ticket. Confirmed live 2026-08-06 on 1871-06-16 p2c1
    # (same bug, first caught in build_repair_stats.py's monitor).
    rows = conn.execute(
        "SELECT id, page, col_idx FROM column_transcripts ct "
        "WHERE year=? AND month=? AND day=? AND status != 'done' "
        "AND created_at = ("
        "  SELECT MAX(created_at) FROM column_transcripts ct2"
        "  WHERE ct2.year=ct.year AND ct2.month=ct.month AND ct2.day=ct.day"
        "    AND ct2.page=ct.page AND ct2.col_idx=ct.col_idx) "
        "ORDER BY page, col_idx",
        (y, m, d),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        p = os.path.join(REPO_ROOT, "transcribe", "work", "columns", f"{r['id']}.json")
        n = 0
        if os.path.exists(p):
            with open(p) as f:
                n = len(json.load(f).get("slices", []))
        out.append({"id": r["id"], "page": r["page"], "col_idx": r["col_idx"], "n_slices": n})
    return out


def main(dates):
    per_date = {}
    all_items = []
    t_wall_start = time.time()

    for date_str in dates:
        y, m, d = (int(x) for x in date_str.split("-"))

        t0 = time.time()
        claim_stdout = io.StringIO()
        with contextlib.redirect_stdout(claim_stdout):
            cc.main([date_str])
        t1 = time.time()
        # last line of claim_columns' own summary, for the per-date log
        claim_summary = [l for l in claim_stdout.getvalue().splitlines() if l.strip()][-1:] or [""]

        items = query_remaining(y, m, d)
        for it in items:
            it["date"] = date_str
            it["y"], it["m"], it["day"] = y, m, d
        t2 = time.time()

        all_items.extend(items)
        per_date[date_str] = {
            "n_items": len(items),
            "claim_s": round(t1 - t0, 2),
            "query_s": round(t2 - t1, 2),
            "claim_summary": claim_summary[0],
        }

    t_wall_end = time.time()

    result = {
        "per_date": per_date,
        "items": all_items,
        "elapsed_s": {
            "total_wall_s": round(t_wall_end - t_wall_start, 2),
        },
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
