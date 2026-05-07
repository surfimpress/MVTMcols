"""Supervisor for batch column-cutting across the MVTM corpus.

Runs `archive.process_archive` one year at a time, in a deterministic
non-sequential order, persisting state to data/cut_corpus_state.json
and logs to cut_corpus.log so a long-running campaign can be
monitored and resumed.

Usage:
    python3 cut_corpus.py --workers 4
    python3 cut_corpus.py --dry-run
    python3 cut_corpus.py --year 1973 --workers 4

The campaign target is years 1862-1979 ending in 1/3/4/6/8/9 with
zero `page_layouts` rows, plus years with 0 < page_layouts < 10
(experimental-sample years to redo).

See plan: ~/.claude/plans/imperative-popping-hollerith.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sqlite3
import sys
import time

import archive

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(REPO_ROOT, "data", "mvtm.db")
DEFAULT_STATE = os.path.join(REPO_ROOT, "data", "cut_corpus_state.json")
DEFAULT_LOG = os.path.join(REPO_ROOT, "cut_corpus.log")
DEFAULT_BACKUP = os.path.join(
    REPO_ROOT, "data", "mvtm.db.pre-corpus-cut.bak")

# Deterministic shuffle seed. Don't change unless intentionally
# re-permuting the campaign — the seed is what guarantees that a
# resumed run keeps the same year order as the original launch.
SHUFFLE_SEED = 20260506

YEAR_MIN = 1862
YEAR_MAX = 1979
TARGET_DIGIT_SET = {1, 3, 4, 6, 8, 9}


class Tee:
    """Write to multiple streams at once.

    archive.process_archive prints heavily to stdout and stderr
    during a year batch; we redirect both through a Tee so the
    output appears live in the terminal AND lands in the log file
    for after-the-fact inspection.
    """

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self._streams:
            try:
                st.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def log_line(log_fh, msg: str) -> None:
    """Write a timestamped supervisor event to log file + stdout."""
    line = f"{now_iso()}  {msg}\n"
    log_fh.write(line)
    log_fh.flush()
    sys.__stdout__.write(line)
    sys.__stdout__.flush()


def open_db_ro(db_path: str) -> sqlite3.Connection:
    """Open mvtm.db read-only via URI mode."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def query_year_coverage(db_path: str) -> dict[int, dict]:
    """Return {year: {files_count, page_layouts_count}} for all years."""
    conn = open_db_ro(db_path)
    try:
        rows = conn.execute(
            """
            SELECT f.year,
                   COUNT(DISTINCT f.id) AS files_count,
                   COUNT(DISTINCT (pl.year || '-' || pl.month || '-'
                                || pl.day || '-' || pl.page))
                       AS page_layouts_count
              FROM files f
         LEFT JOIN page_layouts pl
                ON f.year = pl.year AND f.month = pl.month
               AND f.day = pl.day AND f.page = pl.page
             WHERE f.year BETWEEN ? AND ?
               AND f.file_type = 'pdf'
          GROUP BY f.year
            """,
            (YEAR_MIN, YEAR_MAX),
        ).fetchall()
    finally:
        conn.close()
    return {r["year"]: dict(r) for r in rows}


def select_target_years(coverage: dict[int, dict],
                        partial_threshold: int) -> list[int]:
    """Pick untouched (digits 1/3/4/6/8/9) + partial-coverage years."""
    targets = []
    for year, cov in coverage.items():
        pl = cov["page_layouts_count"]
        ends_in_target = (year % 10) in TARGET_DIGIT_SET
        if ends_in_target and pl == 0:
            targets.append(year)
        elif 0 < pl < partial_threshold:
            targets.append(year)
    return sorted(targets)


def shuffle_year_list(years: list[int]) -> list[int]:
    """Deterministic shuffle of the year list."""
    out = list(years)
    random.Random(SHUFFLE_SEED).shuffle(out)
    return out


def issues_for_year(db_path: str, year: int) -> list[tuple[int, int, int]]:
    """Distinct (y,m,d) PDF issues registered for the year."""
    conn = open_db_ro(db_path)
    try:
        rows = conn.execute(
            """SELECT DISTINCT year, month, day
                 FROM files
                WHERE year = ? AND file_type = 'pdf'
             ORDER BY month, day""",
            (year,),
        ).fetchall()
    finally:
        conn.close()
    return [(r["year"], r["month"], r["day"]) for r in rows]


def load_state(state_path: str) -> dict | None:
    if not os.path.isfile(state_path):
        return None
    with open(state_path) as f:
        return json.load(f)


def save_state(state_path: str, state: dict) -> None:
    """Atomic state write — temp file + os.replace."""
    tmp = state_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_path)


def build_fresh_state(db_path: str, partial_threshold: int) -> dict:
    coverage = query_year_coverage(db_path)
    targets = select_target_years(coverage, partial_threshold)
    order = shuffle_year_list(targets)
    return {
        "created_at": now_iso(),
        "shuffle_seed": SHUFFLE_SEED,
        "partial_threshold": partial_threshold,
        "year_order": order,
        "status": {
            str(y): {
                "state": "pending",
                "files_count": coverage[y]["files_count"],
                "page_layouts_at_start":
                    coverage[y]["page_layouts_count"],
            }
            for y in order
        },
    }


def check_backup(backup_path: str, log_fh) -> bool:
    """Warn if backup is missing or stale; return True to proceed."""
    if not os.path.isfile(backup_path):
        log_line(log_fh, f"WARNING  no backup at {backup_path}")
        log_line(log_fh, "Suggestion: cp data/mvtm.db "
                         "data/mvtm.db.pre-corpus-cut.bak")
        try:
            answer = input("Continue without a fresh backup? [y/N]: ")
        except EOFError:
            answer = ""
        return answer.strip().lower() == "y"
    age_h = (time.time() - os.path.getmtime(backup_path)) / 3600
    if age_h > 24:
        log_line(log_fh, f"WARNING  backup is {age_h:.1f}h old "
                         f"({backup_path})")
    else:
        log_line(log_fh, f"OK  backup present ({age_h:.1f}h old)")
    return True


def run_year(year: int, dates, *,
             db_path: str, max_workers: int, log_fh) -> dict:
    """Invoke process_archive for one year, return summary stats."""
    log_line(log_fh, f"YEAR_START  {year}  {len(dates)} issues  "
                     f"workers={max_workers}")
    t0 = time.time()
    tee = Tee(sys.__stdout__, log_fh)
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    sys.stdout = tee
    sys.stderr = tee
    try:
        result = archive.process_archive(
            dates, db_path=db_path, max_workers=max_workers)
    finally:
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
    elapsed = time.time() - t0
    ok = len(result.get("results", []))
    fail = len(result.get("failures", []))
    log_line(log_fh, f"YEAR_END    {year}  ok={ok} fail={fail}  "
                     f"elapsed={elapsed:.0f}s")
    return {"ok": ok, "fail": fail, "elapsed_s": elapsed}


def cmd_dry_run(state: dict) -> int:
    print(f"\nYear order ({len(state['year_order'])} years):")
    for y in state["year_order"]:
        st = state["status"][str(y)]
        print(f"  {y}  [{st['state']:>8s}]  "
              f"files={st.get('files_count', '?')}  "
              f"page_layouts_at_start="
              f"{st.get('page_layouts_at_start', '?')}")
    return 0


def cmd_single_year(args, log_fh) -> int:
    log_line(log_fh, f"SUPERVISOR  single-year smoke test {args.year}")
    if not args.no_backup_check \
            and not check_backup(args.backup, log_fh):
        log_line(log_fh, "ABORTED  no fresh backup")
        return 1
    dates = issues_for_year(args.db, args.year)
    if not dates:
        log_line(log_fh, f"ERROR  no issues registered for {args.year}")
        return 1
    run_year(args.year, dates,
             db_path=args.db, max_workers=args.workers, log_fh=log_fh)
    return 0


def cmd_campaign(args, log_fh) -> int:
    state = load_state(args.state)
    if state is None:
        state = build_fresh_state(args.db, args.partial_threshold)
        save_state(args.state, state)
        log_line(log_fh, f"SUPERVISOR  fresh state built "
                         f"({len(state['year_order'])} years) "
                         f"→ {args.state}")
    else:
        pending = sum(1 for y in state["year_order"]
                      if state["status"][str(y)]["state"] != "done")
        log_line(log_fh, f"SUPERVISOR  resuming from {args.state} "
                         f"({pending} pending of "
                         f"{len(state['year_order'])} total)")

    if args.dry_run:
        return cmd_dry_run(state)

    if not args.no_backup_check \
            and not check_backup(args.backup, log_fh):
        log_line(log_fh, "ABORTED  no fresh backup")
        return 1

    try:
        for year in state["year_order"]:
            ystr = str(year)
            cur = state["status"][ystr]
            if cur["state"] == "done":
                continue
            dates = issues_for_year(args.db, year)
            if not dates:
                log_line(log_fh, f"SKIP  {year}  no issues registered")
                cur["state"] = "skipped"
                save_state(args.state, state)
                continue
            cur["state"] = "running"
            cur["started_at"] = now_iso()
            cur["issues"] = len(dates)
            save_state(args.state, state)
            try:
                summary = run_year(
                    year, dates,
                    db_path=args.db, max_workers=args.workers,
                    log_fh=log_fh)
            except KeyboardInterrupt:
                cur["state"] = "interrupted"
                cur["interrupted_at"] = now_iso()
                save_state(args.state, state)
                log_line(log_fh,
                         f"YEAR_INTERRUPT  {year}  Ctrl-C during run")
                log_line(log_fh, "STOPPING")
                return 130
            except Exception as e:
                cur["state"] = "failed"
                cur["error"] = repr(e)
                save_state(args.state, state)
                log_line(log_fh, f"YEAR_FAIL  {year}  {e!r}")
                continue
            cur["state"] = "done"
            cur["ended_at"] = now_iso()
            cur["ok"] = summary["ok"]
            cur["fail"] = summary["fail"]
            cur["elapsed_s"] = summary["elapsed_s"]
            save_state(args.state, state)
            if args.cooldown_s > 0:
                log_line(log_fh,
                         f"COOLDOWN  sleeping {args.cooldown_s}s")
                try:
                    time.sleep(args.cooldown_s)
                except KeyboardInterrupt:
                    log_line(log_fh,
                             "STOPPING  Ctrl-C during cooldown")
                    return 130
    except KeyboardInterrupt:
        log_line(log_fh, "STOPPING  Ctrl-C between years")
        return 130

    log_line(log_fh, "CAMPAIGN_END  all years processed")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n")[0])
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--state", default=DEFAULT_STATE)
    p.add_argument("--log", default=DEFAULT_LOG)
    p.add_argument("--backup", default=DEFAULT_BACKUP)
    p.add_argument("--partial-threshold", type=int, default=20,
                   help="Years with 0 < page_layouts < N count as redo "
                        "(default 20). Sample-runs typically left 8-10 "
                        "page_layouts on years with 400+ files; 20 "
                        "catches them with margin without sweeping in "
                        "any genuinely-cut year.")
    p.add_argument("--cooldown-s", type=int, default=30,
                   help="Sleep between year batches (default 30s)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the year order and exit")
    p.add_argument("--year", type=int, default=None,
                   help="Process a single year (state file untouched)")
    p.add_argument("--no-backup-check", action="store_true",
                   help="Skip the backup-file age check")
    args = p.parse_args(argv)

    log_fh = open(args.log, "a")
    try:
        if args.year is not None:
            return cmd_single_year(args, log_fh)
        return cmd_campaign(args, log_fh)
    finally:
        log_fh.close()


if __name__ == "__main__":
    sys.exit(main())
