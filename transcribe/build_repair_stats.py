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

Cheap on a warm cache (~0.3s): a few GROUP BY queries against
transcribe.db, plus live done-status for the corpus-wide progress
denominator. The corpus-wide STRUCTURE (which issues/pages exist in
mvtm.db at all) is cached at transcribe/work/corpus_totals_cache.json
for up to an hour (see CORPUS_CACHE_MAX_AGE_S) since it only changes
when the cutting pipeline runs -- that pair of queries cost ~3s
uncached (measured 2026-08-06) and this script can be invoked very
frequently by live-monitoring loops. Output is consumed by
transcribe/repair_monitor.html.
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

from . import db as _txdb

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, os.pardir))
TXC_DB = os.path.join(_THIS_DIR, "data", "transcribe.db")
OUT_PATH = os.path.join(_THIS_DIR, "repair_stats.json")
SLICES_DIR = os.path.join(_THIS_DIR, "work", "slices")

# Corpus-wide structure (which issues/pages exist in mvtm.db at all) only
# changes when the cutting pipeline runs, which doesn't happen during a
# transcription session -- an hour-old cache is exact in practice. Measured
# 2026-08-06: the two queries this caches cost ~3s combined (96% of this
# script's total runtime), and this script was being invoked on every
# single page-completion event by a live-monitoring loop -- see
# PLAYBOOK.md's waste-audit entry. done-status (what's transcribed so far)
# is NOT cached -- that's cheap (~0.1s) and must stay live.
CORPUS_CACHE_PATH = os.path.join(_THIS_DIR, "work", "corpus_totals_cache.json")
CORPUS_CACHE_MAX_AGE_S = 3600

# Per real-world processing day, permanent once a day is closed (see the
# day-by-day chart section below for the full rationale). Unlike the
# corpus cache above, this has no max-age -- a closed day's stats are
# correct forever, not just "probably still fresh."
DAY_STATS_CACHE_PATH = os.path.join(_THIS_DIR, "work", "day_stats_cache.json")

# Rule-of-thumb concurrency for the elapsed-time estimate (total agent
# compute / assumed_concurrency). Real runs rarely hold their target
# concurrency steadily -- there's always some ramp-up/ramp-down at issue
# boundaries and while refilling slots -- so we discount the *target*
# concurrency for an issue by this fraction rather than subtracting a
# flat constant.
#
# Was 0.75. Measured 2026-08-06 against wf_0fb941b2-c73 (the first real
# continuous-queue run, 1869-01-29+1886-07-16+1897-11-05 as one queue,
# no barrier between issues): real wall-clock elapsed (81.7 min, cross-
# checked both from earliest-agent-start-to-now and against the
# Workflow app's own ~1h20 display) implied actual achieved concurrency
# of ~5.74 of a target-6 -- a 0.957 fraction, well above 0.75. Moved to
# 0.85 as a halfway step on a single data point rather than jumping
# straight to ~0.95 -- revisit once a second run's real elapsed time is
# known (see PLAYBOOK.md).
ASSUMED_CONCURRENCY_FRACTION = 0.85

# The orchestrator's target in-flight-agent count has changed over the
# course of this project (6 was the standing practice for most of it;
# raised to 12 starting 2026-08-05). There's no recorded per-issue
# concurrency in the DB -- claim_columns.py stamps created_at in one
# bulk pass at claim time, not per-dispatch, so timestamps can't
# reconstruct true concurrency (the multi-week-resume-gap problem noted
# below applies here too). This table is a declarative record of what
# target concurrency was actually used for a given issue; issues not
# listed fall back to DEFAULT_TARGET_CONCURRENCY. Add an entry here
# whenever a session runs an issue at a non-default concurrency.
DEFAULT_TARGET_CONCURRENCY = 6
ISSUE_TARGET_CONCURRENCY = {
    # 1937-02-11 ran at 6 for most of its columns, raised to 12 partway
    # through for the last ~19 -- left at the default since the rule of
    # thumb doesn't support a mid-issue split, and 6 was the majority.
    "1958-09-25": 12,
}


def _load_corpus_cache() -> dict | None:
    """Return {"total_corpus_issues", "page_totals"} if a fresh cache
    exists, else None. Never raises -- any read/parse problem is
    treated as a cache miss so a corrupt cache can't break the build.
    """
    try:
        with open(CORPUS_CACHE_PATH) as f:
            cache = json.load(f)
        cached_at = datetime.fromisoformat(cache["cached_at"])
        age_s = (datetime.now(timezone.utc) - cached_at).total_seconds()
        if age_s > CORPUS_CACHE_MAX_AGE_S:
            return None
        return cache
    except Exception:
        return None


def _save_corpus_cache(total_corpus_issues: int, page_totals: list[dict]) -> None:
    os.makedirs(os.path.dirname(CORPUS_CACHE_PATH), exist_ok=True)
    with open(CORPUS_CACHE_PATH, "w") as f:
        json.dump({
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "total_corpus_issues": total_corpus_issues,
            "page_totals": page_totals,
        }, f)


def _load_day_stats_cache() -> dict:
    """Return {day: {avg_duration_s, n, fastest10_avg_s, slowest10_avg_s}}
    for every closed day computed so far. Never raises -- a corrupt
    cache is treated as empty so it can't break the build (it will
    just recompute everything once and rewrite a clean file).
    """
    try:
        with open(DAY_STATS_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_day_stats_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(DAY_STATS_CACHE_PATH), exist_ok=True)
    with open(DAY_STATS_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


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

        all_done_rows = conn.execute(
            "SELECT year, page, col_idx FROM column_transcripts WHERE status='done'"
        ).fetchall()

        # A column whose PNG is re-cut gets a NEW row (new image_sha256);
        # the old row is deliberately left in place for history (see
        # transcribe/CLAUDE.md's "Re-cuts and history"). Only the latest
        # row per (year,month,day,page,col_idx) reflects current reality
        # -- an orphaned pre-recut 'claimed' row must not count as
        # outstanding once a newer row for the same position is 'done'.
        # Confirmed live 2026-08-06: 1871-06-16 p2c1 has exactly this --
        # an orphaned claimed row from 2026-07-30 12:02 alongside the
        # real done row from 70s later -- and was misreported as having
        # 1 outstanding column.
        outstanding_rows = conn.execute(
            "SELECT year, month, day, COUNT(*) AS n_outstanding "
            "FROM column_transcripts ct WHERE status != 'done' "
            "AND created_at = ("
            "  SELECT MAX(created_at) FROM column_transcripts ct2"
            "  WHERE ct2.year=ct.year AND ct2.month=ct.month AND ct2.day=ct.day"
            "    AND ct2.page=ct.page AND ct2.col_idx=ct.col_idx) "
            "GROUP BY year, month, day"
        ).fetchall()

        # When an issue's last column finished (its "round up" moment),
        # not the newspaper's own date. Only meaningful once the issue is
        # fully done (outstanding==0 below); an in-progress issue has no
        # date_transcribed yet. Same source query the processing-day chart
        # uses -- computed once here so both stay consistent.
        completed_at_rows = conn.execute(
            "SELECT year, month, day, MAX(updated_at) AS completed_at "
            "FROM column_transcripts WHERE status='done' "
            "GROUP BY year, month, day"
        ).fetchall()
    finally:
        conn.close()

    outstanding_by_issue = {
        f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}": r["n_outstanding"]
        for r in outstanding_rows
    }

    completed_at_by_issue = {
        f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}": r["completed_at"]
        for r in completed_at_rows
        if r["completed_at"]
        and outstanding_by_issue.get(
            f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}", 0) == 0
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
        target_concurrency = ISSUE_TARGET_CONCURRENCY.get(
            date, DEFAULT_TARGET_CONCURRENCY)
        assumed_concurrency = round(
            target_concurrency * ASSUMED_CONCURRENCY_FRACTION, 2)
        if n_timed:
            secs = [d_ms / 1000.0 for d_ms, _ in durations]
            calls = [c for _, c in durations if c is not None]
            total_agent_time_s = round(sum(secs), 1)
            timing = {
                "n_timed": n_timed,
                "median_duration_s": round(statistics.median(secs), 1),
                "mean_duration_s": round(statistics.mean(secs), 1),
                "total_agent_time_s": total_agent_time_s,
                "median_tool_calls": round(statistics.median(calls), 1)
                                     if calls else None,
                # Rule-of-thumb wall-clock estimate: total compute divided
                # by the assumed concurrency, rather than an actual
                # claim-to-finish timestamp span (which we tried and
                # rejected -- issues resumed after a multi-week gap made
                # that span meaningless, e.g. 94 days for a issue finished
                # in one sitting decades after its first column was
                # claimed). assumed_concurrency is 75% of this issue's
                # target in-flight count (see ISSUE_TARGET_CONCURRENCY
                # above), not a measured value.
                "target_concurrency": target_concurrency,
                "assumed_concurrency": assumed_concurrency,
                "elapsed_s": round(total_agent_time_s / assumed_concurrency, 1),
            }
        else:
            timing = {
                "n_timed": 0,
                "median_duration_s": None,
                "mean_duration_s": None,
                "total_agent_time_s": None,
                "median_tool_calls": None,
                "target_concurrency": target_concurrency,
                "assumed_concurrency": assumed_concurrency,
                "elapsed_s": None,
            }

        n_outstanding = outstanding_by_issue.get(date, 0)
        complete = n_outstanding == 0
        date_transcribed = (completed_at_by_issue.get(date, "")[:10] or None) \
            if complete else None

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
            "complete": complete,
            "date_transcribed": date_transcribed,
        })

    # Corpus-wide denominator: every issue with column assets + a page
    # layout in mvtm.db, regardless of transcription status -- same
    # eligibility rule as transcribe.pick_issue._eligible_issues, but
    # counting the whole set rather than the not-done remainder.
    #
    # This structure (which issues/pages exist at all) changes rarely --
    # only when the cutting pipeline runs -- so it's cached (see
    # CORPUS_CACHE_MAX_AGE_S above) rather than recomputed on every call.
    # done_count_by_page below is NOT cached -- that's the live
    # transcription-progress signal and must reflect the current DB.
    cached = _load_corpus_cache()
    if cached is not None:
        total_corpus_issues = cached["total_corpus_issues"]
        page_totals = cached["page_totals"]
    else:
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

            # Page-level counterpart: every page_layouts row belonging to
            # an eligible issue is one corpus page. A page counts as
            # "done" only when every column on it has status='done' -- a
            # page with 4/5 columns done isn't done, unlike the
            # issue-level stat above (which counts an issue as "in" the
            # corpus numerator the moment it has any done column at
            # all). Different semantics on purpose: this is meant to
            # read as real page-level progress.
            page_totals_rows = corpus_conn.execute(
                """SELECT pl.year, pl.month, pl.day, pl.page, pl.num_columns
                     FROM mvtm.page_layouts AS pl
                    WHERE EXISTS (
                        SELECT 1 FROM mvtm.file_assets AS fa
                         WHERE fa.kind = 'column'
                           AND fa.year = pl.year
                           AND fa.month = pl.month
                           AND fa.day = pl.day)"""
            ).fetchall()
            page_totals = [dict(r) for r in page_totals_rows]
        finally:
            corpus_conn.close()
        _save_corpus_cache(total_corpus_issues, page_totals)

    total_corpus_pages = len(page_totals)

    done_conn = sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)
    done_conn.row_factory = sqlite3.Row
    try:
        done_counts = done_conn.execute(
            "SELECT year, month, day, page, COUNT(*) AS n "
            "FROM column_transcripts WHERE status='done' "
            "GROUP BY year, month, day, page"
        ).fetchall()
    finally:
        done_conn.close()
    done_count_by_page = {
        (r["year"], r["month"], r["day"], r["page"]): r["n"]
        for r in done_counts
    }
    pages_done = sum(
        1 for r in page_totals
        if done_count_by_page.get(
            (r["year"], r["month"], r["day"], r["page"]), 0) >= r["num_columns"]
    )

    # Timing breakdowns for the monitor's charts. All three average
    # agent_duration_ms per bucket (never sum), so a bucket that only a
    # few issues reach (e.g. page 9, or a decade with one sample issue)
    # isn't penalized for having fewer contributing columns.
    timed_detail_conn = sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)
    timed_detail_conn.row_factory = sqlite3.Row
    try:
        timed_detail = timed_detail_conn.execute(
            "SELECT year, page, col_idx, agent_duration_ms, updated_at "
            "FROM column_transcripts "
            "WHERE status='done' AND agent_duration_ms IS NOT NULL"
        ).fetchall()
    finally:
        timed_detail_conn.close()

    def _avg_by(keyfn, label_fn=str):
        # Seed every bucket that has at least one done column, even if none
        # of them are timed -- e.g. 1940-06-20 predates timing capture
        # entirely, but its decade should still show up on the chart as
        # "no timing data" rather than silently vanishing.
        buckets: dict = {keyfn(r): [] for r in all_done_rows}
        for r in timed_detail:
            buckets.setdefault(keyfn(r), []).append(r["agent_duration_ms"] / 1000.0)
        return [
            {"label": label_fn(k),
             "avg_duration_s": round(statistics.mean(v), 1) if v else None,
             "n": len(v)}
            for k, v in sorted(buckets.items())
        ]

    avg_time_by_page = _avg_by(lambda r: r["page"], lambda k: f"p{k}")
    avg_time_by_col_idx = _avg_by(lambda r: r["col_idx"], lambda k: f"c{k}")
    avg_time_by_decade = _avg_by(
        lambda r: (r["year"] // 10) * 10, lambda k: f"{k}s")

    # Real-world processing day (when we actually ran the column, from
    # updated_at), NOT the newspaper issue's own date -- this is the
    # chart that answers "is OUR pipeline getting faster over time," so
    # it has to be keyed by when the work happened, not what decade the
    # source material is from. Every calendar day between the first and
    # last day we have timing for is represented, even ones with no
    # work done (avg_duration_s: null, n: 0), so idle days are visible
    # in the axis rather than silently compressing the timeline.
    #
    # A closed day (strictly before today, real UTC date) never changes
    # once computed -- there's no reason to rescan the full
    # done-columns table for it on every call. Its stats (including the
    # fastest/slowest-10 averages, which even out single-outlier noise
    # vs a bare min/max) are cached permanently in DAY_STATS_CACHE_PATH.
    # Only today (still accumulating) and any day that has just closed
    # since the last run get a fresh per-day query.
    day_cache_conn = sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        day_cache = _load_day_stats_cache()

        distinct_days = [
            r[0] for r in day_cache_conn.execute(
                "SELECT DISTINCT substr(updated_at,1,10) FROM column_transcripts "
                "WHERE status='done' AND agent_duration_ms IS NOT NULL "
                "AND updated_at IS NOT NULL"
            ).fetchall()
        ]

        def _day_stats_from_durations_ms(duration_ms_list):
            durs = sorted(v / 1000.0 for v in duration_ms_list)
            n = len(durs)
            return {
                "avg_duration_s": round(statistics.mean(durs), 1),
                "n": n,
                "fastest10_avg_s": round(statistics.mean(durs[:10]), 1),
                "slowest10_avg_s": round(statistics.mean(durs[-10:]), 1),
            }

        cache_dirty = False
        day_stats: dict = dict(day_cache)
        for day in distinct_days:
            if day == today_str or day not in day_cache:
                rows = day_cache_conn.execute(
                    "SELECT agent_duration_ms FROM column_transcripts "
                    "WHERE status='done' AND agent_duration_ms IS NOT NULL "
                    "AND substr(updated_at,1,10)=?",
                    (day,)
                ).fetchall()
                stats = _day_stats_from_durations_ms([r[0] for r in rows])
                day_stats[day] = stats
                if day != today_str:
                    day_cache[day] = stats
                    cache_dirty = True

        if cache_dirty:
            _save_day_stats_cache(day_cache)
    finally:
        day_cache_conn.close()

    avg_time_by_processing_day = []
    if day_stats:
        first_day = datetime.strptime(min(day_stats), "%Y-%m-%d")
        last_day = datetime.strptime(max(day_stats), "%Y-%m-%d")
        cursor = first_day
        while cursor <= last_day:
            key = cursor.strftime("%Y-%m-%d")
            s = day_stats.get(key)
            avg_time_by_processing_day.append({
                "label": key,
                "avg_duration_s": s["avg_duration_s"] if s else None,
                "n": s["n"] if s else 0,
                "fastest10_avg_s": s["fastest10_avg_s"] if s else None,
                "slowest10_avg_s": s["slowest10_avg_s"] if s else None,
            })
            cursor += timedelta(days=1)

    # Issues fully completed per real processing day -- a
    # production/throughput companion to the speed chart above. Shares
    # that chart's exact day range (derived from it directly, not
    # recomputed) so the two line up for a direct visual comparison.
    # An issue's "day" here is when its LAST column finished, not the
    # newspaper's own date -- same convention as the speed chart. Reuses
    # completed_at_by_issue (already filtered to complete issues only,
    # per the date_transcribed field above) rather than re-querying.
    issues_completed_by_day: dict = {}
    for date_key, completed_at in completed_at_by_issue.items():
        day = completed_at[:10]
        issues_completed_by_day[day] = issues_completed_by_day.get(day, 0) + 1

    issues_per_day = [
        {"label": e["label"], "n": issues_completed_by_day.get(e["label"], 0)}
        for e in avg_time_by_processing_day
    ]

    # Distribution of agent run time -- how many columns fall in each
    # 1-minute bucket, with a tail overflow bucket so a handful of very
    # long outliers (e.g. the 24-minute p8c1 case) don't stretch every
    # other bar into invisibility.
    BIN_WIDTH_S = 60
    MAX_BINS = 15  # buckets 0-14 minutes; 15+ overflows into one bucket
    duration_histogram = []
    bin_counts = [0] * (MAX_BINS + 1)
    for r in timed_detail:
        b = min(int((r["agent_duration_ms"] / 1000.0) // BIN_WIDTH_S), MAX_BINS)
        bin_counts[b] += 1
    for i, n in enumerate(bin_counts):
        label = f"{MAX_BINS}m+" if i == MAX_BINS else f"{i}m"
        duration_histogram.append({"label": label, "n": n})

    return {
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "repair_kinds": sorted(all_kinds),
        "issues": issues,
        "total_corpus_issues": total_corpus_issues,
        "corpus_pct": round(100.0 * len(issues) / total_corpus_issues, 2)
                      if total_corpus_issues else None,
        "total_corpus_pages": total_corpus_pages,
        "pages_done": pages_done,
        "pages_done_pct": round(100.0 * pages_done / total_corpus_pages, 2)
                          if total_corpus_pages else None,
        "avg_time_by_page": avg_time_by_page,
        "avg_time_by_col_idx": avg_time_by_col_idx,
        "avg_time_by_decade": avg_time_by_decade,
        "avg_time_by_processing_day": avg_time_by_processing_day,
        "issues_completed_by_day": issues_per_day,
        "duration_histogram": duration_histogram,
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
