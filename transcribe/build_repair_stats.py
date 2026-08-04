"""Write transcribe/repair_stats.json for the repair-rate monitor page.

For every issue that has at least one 'done' column, reports how many
columns were transcribed and how many of those were flagged
repair_needed by the column-transcriber agent. This is a diagnostic
signal for the upstream cutting/ad-detection stages, not a
transcription-quality metric -- see transcribe/quality_review.md and
the 1963-10-10 / 1961-06-29 findings for what a high rate usually means
(column boundary miscalibration, or unregistered/missing ad detection).

Run after any transcription batch:

    python3 -m transcribe.build_repair_stats

Cheap: two GROUP BY queries against transcribe.db. Output is consumed
by transcribe/repair_monitor.html.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

from . import db as _txdb

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
TXC_DB = os.path.join(_THIS_DIR, "data", "transcribe.db")
OUT_PATH = os.path.join(_THIS_DIR, "repair_stats.json")
SLICES_DIR = os.path.join(_THIS_DIR, "work", "slices")


def _slice_images(column_id: str) -> list[str]:
    """Repo-root-relative paths to a column's slice PNGs, verified to
    exist on disk right now.

    column_transcripts.image_path points at columns/<date>/p<N>/..., but
    that file is frequently not present locally (columns/ gets archived
    to Drive to free disk -- see viewer.html's ARCHIVE_BASE fallback).
    The slice PNGs under transcribe/work/slices/<id>/ are what
    claim_columns.py actually downloads+cuts and cleanup.py deliberately
    leaves in place, so they're the reliable local source for a preview.
    """
    d = os.path.join(SLICES_DIR, column_id)
    if not os.path.isdir(d):
        return []
    names = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
    return [f"transcribe/work/slices/{column_id}/{name}" for name in names]


def build() -> dict:
    if not os.path.isfile(TXC_DB):
        return {"generated_at": None, "repair_kinds": [], "issues": []}

    conn = sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT year, month, day, "
            "COUNT(*) AS n_columns, "
            "COUNT(DISTINCT page) AS n_pages, "
            "SUM(CASE WHEN repair_needed=1 THEN 1 ELSE 0 END) AS n_repairs "
            "FROM column_transcripts WHERE status='done' "
            "GROUP BY year, month, day "
            "ORDER BY year, month, day"
        ).fetchall()

        repair_rows = conn.execute(
            "SELECT ct.year, ct.month, ct.day, ct.page, ct.col_idx, "
            "ct.id AS column_id, ct.transcript_text, ct.agent_duration_ms, "
            "r.description, r.repair_kind "
            "FROM repairs r "
            "JOIN column_transcripts ct ON ct.id = r.related_column_id "
            "ORDER BY ct.year, ct.month, ct.day, ct.page, ct.col_idx"
        ).fetchall()

        timing_rows = conn.execute(
            "SELECT year, month, day, agent_duration_ms, agent_tool_calls "
            "FROM column_transcripts "
            "WHERE status='done' AND agent_duration_ms IS NOT NULL"
        ).fetchall()

        outstanding_rows = conn.execute(
            "SELECT year, month, day, COUNT(*) AS n_outstanding "
            "FROM column_transcripts WHERE status != 'done' "
            "GROUP BY year, month, day"
        ).fetchall()
    finally:
        conn.close()

    outstanding_by_issue = {
        f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}": r["n_outstanding"]
        for r in outstanding_rows
    }

    timing_by_issue: dict[str, list] = {}
    for r in timing_rows:
        key = f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}"
        timing_by_issue.setdefault(key, []).append(
            (r["agent_duration_ms"], r["agent_tool_calls"]))

    repairs_by_issue: dict[str, list] = {}
    all_kinds: set[str] = set()
    for r in repair_rows:
        key = f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}"
        kind = r["repair_kind"] or "other"
        all_kinds.add(kind)
        repairs_by_issue.setdefault(key, []).append({
            "page": r["page"],
            "col_idx": r["col_idx"],
            "repair_kind": kind,
            "description": r["description"],
            "slice_images": _slice_images(r["column_id"]),
            "transcript_text": r["transcript_text"],
            "duration_s": round(r["agent_duration_ms"] / 1000.0, 1)
                          if r["agent_duration_ms"] is not None else None,
        })

    issues = []
    for r in rows:
        n_columns = r["n_columns"]
        n_repairs = r["n_repairs"] or 0
        date = f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}"
        issue_repairs = repairs_by_issue.get(date, [])

        by_kind: dict[str, int] = {}
        for rep in issue_repairs:
            by_kind[rep["repair_kind"]] = by_kind.get(rep["repair_kind"], 0) + 1

        durations = timing_by_issue.get(date, [])
        n_timed = len(durations)
        if n_timed:
            secs = [d_ms / 1000.0 for d_ms, _ in durations]
            calls = [c for _, c in durations if c is not None]
            timing = {
                "n_timed": n_timed,
                "median_duration_s": round(statistics.median(secs), 1),
                "mean_duration_s": round(statistics.mean(secs), 1),
                "total_agent_time_s": round(sum(secs), 1),
                "median_tool_calls": round(statistics.median(calls), 1)
                                     if calls else None,
            }
        else:
            timing = {
                "n_timed": 0,
                "median_duration_s": None,
                "mean_duration_s": None,
                "total_agent_time_s": None,
                "median_tool_calls": None,
            }

        n_outstanding = outstanding_by_issue.get(date, 0)

        issues.append({
            "date": date,
            "year": r["year"],
            "n_columns": n_columns,
            "n_columns_total": n_columns + n_outstanding,
            "n_pages": r["n_pages"],
            "n_repairs": n_repairs,
            "repair_rate": round(100.0 * n_repairs / n_columns, 1)
                           if n_columns else 0.0,
            "repairs_by_kind": by_kind,
            "repairs": issue_repairs,
            "timing": timing,
            "complete": n_outstanding == 0,
        })

    # Corpus-wide denominator: every issue with column assets + a page
    # layout in mvtm.db, regardless of transcription status -- same
    # eligibility rule as transcribe.pick_issue._eligible_issues, but
    # counting the whole set rather than the not-done remainder.
    corpus_conn = _txdb.open_connection(attach_mvtm=True)
    try:
        total_corpus_issues = corpus_conn.execute(
            """SELECT COUNT(DISTINCT fa.year || '-' || fa.month || '-' || fa.day)
                 AS n
               FROM mvtm.file_assets AS fa
              WHERE fa.kind = 'column'
                AND EXISTS (
                    SELECT 1 FROM mvtm.page_layouts AS pl
                     WHERE pl.year = fa.year
                       AND pl.month = fa.month
                       AND pl.day = fa.day)"""
        ).fetchone()["n"]
    finally:
        corpus_conn.close()

    # Timing breakdowns for the monitor's charts. All three average
    # agent_duration_ms per bucket (never sum), so a bucket that only a
    # few issues reach (e.g. page 9, or a decade with one sample issue)
    # isn't penalized for having fewer contributing columns.
    timed_detail_conn = sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)
    timed_detail_conn.row_factory = sqlite3.Row
    try:
        timed_detail = timed_detail_conn.execute(
            "SELECT year, page, col_idx, agent_duration_ms "
            "FROM column_transcripts "
            "WHERE status='done' AND agent_duration_ms IS NOT NULL"
        ).fetchall()
    finally:
        timed_detail_conn.close()

    def _avg_by(keyfn, label_fn=str):
        buckets: dict = {}
        for r in timed_detail:
            k = keyfn(r)
            buckets.setdefault(k, []).append(r["agent_duration_ms"] / 1000.0)
        return [
            {"label": label_fn(k), "avg_duration_s": round(statistics.mean(v), 1),
             "n": len(v)}
            for k, v in sorted(buckets.items())
        ]

    avg_time_by_page = _avg_by(lambda r: r["page"], lambda k: f"p{k}")
    avg_time_by_col_idx = _avg_by(lambda r: r["col_idx"], lambda k: f"c{k}")
    avg_time_by_decade = _avg_by(
        lambda r: (r["year"] // 10) * 10, lambda k: f"{k}s")

    return {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "repair_kinds": sorted(all_kinds),
        "issues": issues,
        "total_corpus_issues": total_corpus_issues,
        "corpus_pct": round(100.0 * len(issues) / total_corpus_issues, 2)
                      if total_corpus_issues else None,
        "avg_time_by_page": avg_time_by_page,
        "avg_time_by_col_idx": avg_time_by_col_idx,
        "avg_time_by_decade": avg_time_by_decade,
    }


def main() -> int:
    payload = build()
    with open(OUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
    n = len(payload["issues"])
    total_pages = sum(i["n_pages"] for i in payload["issues"])
    total_cols = sum(i["n_columns"] for i in payload["issues"])
    total_repairs = sum(i["n_repairs"] for i in payload["issues"])
    rate = f"{100.0 * total_repairs / total_cols:.1f}%" if total_cols else "n/a"
    print(f"wrote {os.path.relpath(OUT_PATH, REPO_ROOT)}: "
          f"{n} issue(s), {total_pages} page(s), {total_cols} column(s), "
          f"{total_repairs} repair(s) ({rate})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
