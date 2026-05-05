"""Parallel batch driver for issue-level processing.

Runs `process_issue` across many issues in parallel. Each issue is one
worker task. A coordinator thread in the main process owns the only
DB-writing connection; workers send writes via a queue.

Usage:
    from archive import process_archive
    process_archive([(1947, 11, 6), (1937, 1, 14)], max_workers=4)

    # CLI
    python3 archive.py 1947-11-06 1937-01-14 --workers 4

Why issue-level (not page-level): the user's actual workload is batch
reprocessing of the archive after a detector change. Issue-level
parallelism scales linearly with worker count and leaves the per-page
detection code untouched. Single-issue dev iteration is unchanged.

Why coordinator-owns-DB: SQLite supports concurrent readers + a single
writer. Routing all writes through one thread eliminates write
contention by construction (no need for retry-on-busy logic). Workers
never block on the DB because each ad gets a UUID at detection time,
not a DB-assigned id.

See: /Users/peter/.claude/plans/issue-parallel-coordinator.md
"""

from __future__ import annotations

import contextlib
import io
import multiprocessing as mp
import os
import queue
import sys
import threading
import time
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed

# Module-global. Set by `_init_worker` once per worker process at pool
# creation. Workers read it from `_run_issue` and pass it as the writer
# kwarg to `process_issue`. We cannot pass the queue as a regular task
# argument because the queue is not picklable per-task — it must be
# inherited via the pool initializer.
_WORKER_WRITER = None


def _init_worker(req_queue):
    """ProcessPoolExecutor initializer. Runs once per worker process."""
    # Local import keeps the worker startup path explicit: each spawned
    # worker re-imports its own modules (fork is unsafe with PyMuPDF on
    # macOS), and we want db_writer to be re-imported here, not relied
    # on via parent inheritance.
    from db_writer import ProxyDBWriter
    global _WORKER_WRITER
    _WORKER_WRITER = ProxyDBWriter(req_queue)


def _run_issue(year, month, day, db_path, output_dir, dpi):
    """Worker entry point. Runs one issue under skip_aggregates=True.

    Returns a dict with the issue date and any captured stdout. The
    parent prints worker logs in completion order so they don't
    interleave with each other.

    Exceptions propagate up to `future.result()` in the parent loop;
    the parent catches them and reports failure with date attribution.
    """
    from process_issue import process_issue

    buf = io.StringIO()
    t0 = time.time()
    with contextlib.redirect_stdout(buf):
        result = process_issue(
            year, month, day,
            output_dir=output_dir,
            db_path=db_path,
            dpi=dpi,
            writer=_WORKER_WRITER,
            skip_aggregates=True,
        )
    return {
        "date": (year, month, day),
        "elapsed": time.time() - t0,
        "log": buf.getvalue(),
        "result": result,
    }


def _coordinator_loop(req_queue, db_path):
    """Drains the request queue, dispatching each message to DirectDBWriter.

    Lives on a thread in the main process. Owns one writing connection
    via DirectDBWriter (which itself opens short-lived connections per
    write — same pattern as standalone runs).

    Wire format: each message is `(method_name, args_tuple)` where
    method_name matches a method on DirectDBWriter. Sentinel for
    shutdown is the bare string `"__shutdown__"`.
    """
    from db_writer import DirectDBWriter
    direct = DirectDBWriter(db_path)

    while True:
        msg = req_queue.get()
        if msg == "__shutdown__":
            break
        op, args = msg
        getattr(direct, op)(*args)


def _handle_done(f, future_to_date, results, failures):
    """Print + collect one completed future. Returns 1 (counts handled)."""
    date = future_to_date[f]
    ymd = f"{date[0]}-{date[1]:02d}-{date[2]:02d}"
    try:
        r = f.result()
    except Exception as e:
        print(f"\n!!! FAILED {ymd}: {e!r}", file=sys.stderr)
        failures.append((date, e))
        return 1
    print(f"\n=== {ymd}  ({r['elapsed']:.1f}s) ===")
    sys.stdout.write(r["log"])
    results.append(r)
    return 1


def _enable_wal(db_path):
    """One-shot WAL mode. Coordinator is the only writer so WAL isn't
    strictly needed for correctness, but it lets concurrent worker reads
    proceed without brief lock stalls during writes."""
    with sqlite3.connect(db_path) as c:
        c.execute("PRAGMA journal_mode=WAL")


def _predownload_serial(dates, db_path, download_dir):
    """Pre-fetch all PDFs in the main process before spawning workers.

    Polite to the source server (one connection at a time) and removes
    the cold-cache spike that would otherwise have N workers fetching
    concurrently. Warm runs are no-ops thanks to P9's on-disk cache.
    """
    # Imported here so the main-module path doesn't pay this cost when
    # the caller skips pre-download.
    from process_issue import download_issue
    for year, month, day in dates:
        download_issue(year, month, day, db_path, download_dir)


def _streaming_downloader(dates, db_path, download_dir, ready_q,
                          batch_size=1, batch_pause_s=0.0):
    """Download issues serially and stream them onto a ready queue.

    Producer side of the streaming-pipeline mode. Stays polite to the
    source (one connection at a time) but drops the up-front "wait for
    all 50 PDFs" stall — workers can start processing the moment the
    first issue is on disk.

    Args:
        dates: list of (y,m,d) tuples to download in order.
        ready_q: queue.Queue receiving each downloaded date as
            (year, month, day). A bare None signals end-of-stream.
        batch_size: how many issues to release together. 1 = fully
            pipelined. >1 = the producer downloads `batch_size`
            issues, releases them, then continues. Useful only if
            the caller wants explicit batch boundaries; performance-
            wise, 1 is strictly best.
        batch_pause_s: optional sleep between batches (seconds). Use
            it if the source server starts rate-limiting; default 0.
    """
    from process_issue import download_issue
    pending_release = []
    for i, (year, month, day) in enumerate(dates):
        download_issue(year, month, day, db_path, download_dir)
        pending_release.append((year, month, day))
        if len(pending_release) >= batch_size:
            for d in pending_release:
                ready_q.put(d)
            pending_release.clear()
            if batch_pause_s and i + 1 < len(dates):
                time.sleep(batch_pause_s)
    for d in pending_release:
        ready_q.put(d)
    ready_q.put(None)


def process_archive(dates, db_path="data/mvtm.db", output_root="columns",
                    download_dir=None, dpi=450, max_workers=None,
                    download_serially=True, download_batch_size=1,
                    download_batch_pause_s=0.0):
    """Run process_issue across many issues in parallel.

    Args:
        dates: iterable of (year, month, day) tuples.
        db_path: SQLite database path.
        output_root: parent dir for per-issue output dirs.
        download_dir: PDF cache dir.
        dpi: render resolution.
        max_workers: process pool size. Default: min(cpu_count, 8).
        download_serially: download PDFs from the main process one
            connection at a time (polite to the source server). When
            True (default), uses a streaming pipeline: a downloader
            thread fetches each issue and hands it to the worker pool
            the moment its PDFs land on disk, so workers don't wait
            for the whole batch to download. When False, workers
            fetch their own PDFs concurrently from inside the pool —
            faster on a warm cache but rude to the source on cold.
        download_batch_size: number of issues released together in
            streaming mode. Default 1 (fully pipelined). Larger values
            release issues in groups; only useful if you want explicit
            batch boundaries.
        download_batch_pause_s: optional sleep between download
            batches (seconds). Use if the source server rate-limits.

    Returns:
        dict with timing and per-issue results.
    """
    if max_workers is None:
        max_workers = min(os.cpu_count() or 4, 8)

    dates = list(dates)
    if not dates:
        return {"elapsed": 0.0, "results": []}

    print(f"process_archive: {len(dates)} issues, max_workers={max_workers}")

    _enable_wal(db_path)

    # spawn context required: PyMuPDF/fitz is not fork-safe on macOS.
    ctx = mp.get_context("spawn")
    req_queue = ctx.Queue()

    coord_thread = threading.Thread(
        target=_coordinator_loop,
        args=(req_queue, db_path),
        name="db-coordinator",
        daemon=False,
    )
    coord_thread.start()

    t_batch = time.time()
    results = []
    failures = []

    with ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=ctx,
        initializer=_init_worker,
        initargs=(req_queue,),
    ) as pool:
        future_to_date = {}
        done_q = queue.Queue()
        completed_count = 0
        submitted_count = 0
        all_submitted = threading.Event()

        def _submit(date_tuple):
            nonlocal submitted_count
            year, month, day = date_tuple
            output_dir = f"{output_root}/{year}-{month:02d}-{day:02d}"
            fut = pool.submit(_run_issue, year, month, day,
                              db_path, output_dir, dpi)
            future_to_date[fut] = date_tuple
            fut.add_done_callback(done_q.put)
            submitted_count += 1

        if download_serially:
            # Streaming pipeline: downloader thread feeds the pool as
            # each issue lands. Workers start the moment the first PDF
            # is on disk; no up-front "wait for all N PDFs" stall.
            ready_q = queue.Queue()
            print(
                f"Streaming downloads → pool "
                f"(batch_size={download_batch_size}, "
                f"pause_between_batches={download_batch_pause_s:g}s)"
            )

            dl_thread = threading.Thread(
                target=_streaming_downloader,
                args=(dates, db_path, download_dir, ready_q,
                      download_batch_size, download_batch_pause_s),
                name="downloader",
                daemon=False,
            )
            dl_thread.start()

            # Submit-as-ready, draining completions in the same loop so
            # logs appear in completion order without waiting for the
            # downloader to finish.
            while True:
                # Drain anything already finished (non-blocking).
                while True:
                    try:
                        f = done_q.get_nowait()
                    except queue.Empty:
                        break
                    completed_count += _handle_done(
                        f, future_to_date, results, failures
                    )
                # Pick up next downloaded issue (blocks). When the
                # downloader signals end-of-stream, stop submitting.
                item = ready_q.get()
                if item is None:
                    break
                _submit(item)
            dl_thread.join()
            all_submitted.set()
        else:
            # Cold-unsafe fast path: let workers fetch concurrently.
            for date in dates:
                _submit(date)
            all_submitted.set()

        # Drain remaining completions.
        while completed_count < submitted_count:
            f = done_q.get()
            completed_count += _handle_done(
                f, future_to_date, results, failures
            )

    # Workers are joined by the executor's __exit__; their mp.Queue
    # feeder threads have flushed by the time we get here.
    req_queue.put("__shutdown__")
    coord_thread.join()

    # End-of-batch aggregates: run once across all issues.
    print("\nRunning end-of-batch aggregates...")
    from layout_intelligence import LayoutDB
    from process_issue import _update_viewer_data
    LayoutDB(db_path).compute_era_patterns()
    _update_viewer_data(db_path, output_root)

    elapsed = time.time() - t_batch
    print(f"\nprocess_archive complete: {len(results)} ok, "
          f"{len(failures)} failed, {elapsed:.1f}s")
    return {
        "elapsed": elapsed,
        "results": results,
        "failures": failures,
    }


def _parse_date(s):
    y, m, d = s.split("-")
    return (int(y), int(m), int(d))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("dates", nargs="+", help="YYYY-MM-DD ...")
    p.add_argument("--db", default="data/mvtm.db")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--no-predownload", action="store_true",
                   help="Let workers fetch their own PDFs concurrently "
                        "(faster on a warm cache, rude to source on cold)")
    p.add_argument("--download-batch-size", type=int, default=1,
                   help="Issues released to the pool per download "
                        "batch (default 1 = fully pipelined)")
    p.add_argument("--download-batch-pause", type=float, default=0.0,
                   help="Seconds to pause between download batches "
                        "(default 0; raise if source rate-limits)")
    args = p.parse_args()

    dates = [_parse_date(s) for s in args.dates]
    process_archive(
        dates,
        db_path=args.db,
        max_workers=args.workers,
        download_serially=not args.no_predownload,
        download_batch_size=args.download_batch_size,
        download_batch_pause_s=args.download_batch_pause,
    )
