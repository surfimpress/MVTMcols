#!/usr/bin/env python3
"""Populate file_assets with one row per known artefact.

Two phases:
  A. Ads — one row per detected_ads entry. Whether or not the local file
     still exists (issues may have been archived and the local tree
     removed), the row is created so a future Drive-backfill step can
     update drive_id/drive_url against the stable (remote, local_path)
     key.
  B. Local non-ad files — walk columns/<YYYY-MM-DD>/p<N>/ for files we
     care about (page_raw.png, page_display.avif, *_col<M>.png) and
     insert a row each. Issues already archived (no local dir) are
     skipped here — the future Drive backfill will pick them up from
     `rclone lsjson`.

Idempotent: re-runs use INSERT OR IGNORE keyed on UNIQUE(remote, local_path).
Existing rows are left untouched, so this is safe to interrupt and resume.

Drive fields (drive_id, drive_url, drive_md5, synced_at) are left NULL.
They get filled by archive_issue.sh after each rclone sync (future work).

`remote` is hard-coded to 'mvtm:' (the only remote we have). If a second
remote is ever added, populate it the same way with a different remote
string — the UNIQUE constraint allows the same file path to coexist
under two remotes.

Usage:
    python3 tools/populate_file_assets.py
    python3 tools/populate_file_assets.py --dry-run
    python3 tools/populate_file_assets.py --phase ads
    python3 tools/populate_file_assets.py --phase local
"""
import argparse
import os
import re
import sqlite3
import sys
import time

REPO = "/Users/peter/Projects/MVTM"
DB_PATH = os.path.join(REPO, "data/mvtm.db")
COLUMNS_DIR = os.path.join(REPO, "columns")
REMOTE = "mvtm:"

# Column slices look like '1980-12-31-01_col3.png' — date prefix + 2-digit
# page + _col + 1+ digits + .png. The 'sc_ad' variant in the ads tree is a
# subcolumn ad; treated identically to 'ad' for file_assets purposes.
COL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}_col\d+\.png$")


def issue_date_from_dir(name):
    """'1980-12-31' -> (1980, 12, 31), or None for any other dir name."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def page_num_from_dir(name):
    """'p3' -> 3, or None."""
    m = re.fullmatch(r"p(\d+)", name)
    if not m:
        return None
    return int(m.group(1))


def repo_relative(abs_path):
    return os.path.relpath(abs_path, REPO)


def populate_ads(conn, dry_run=False):
    """Phase A: one file_assets row per detected_ads row."""
    cur = conn.cursor()
    cur.execute("""
        SELECT year, month, day, page, image_filename, uuid
          FROM detected_ads
         WHERE image_filename IS NOT NULL
    """)
    inserted = skipped = missing_local = 0
    batch = []
    BATCH_SIZE = 1000
    t0 = time.time()
    for year, month, day, page, image_filename, uuid in cur:
        date_dir = f"{year:04d}-{month:02d}-{day:02d}"
        local_rel = f"columns/{date_dir}/ads/p{page}/{image_filename}"
        local_abs = os.path.join(REPO, local_rel)
        bytes_ = None
        if os.path.exists(local_abs):
            try:
                bytes_ = os.path.getsize(local_abs)
            except OSError:
                pass
        else:
            missing_local += 1
        batch.append((
            year, month, day, page, "ad",
            image_filename, uuid, local_rel, bytes_, REMOTE,
        ))
        if len(batch) >= BATCH_SIZE:
            n = _flush_ads(conn, batch, dry_run)
            inserted += n
            skipped += len(batch) - n
            batch.clear()
            if (inserted + skipped) % 20000 == 0:
                print(f"  ads: {inserted + skipped} processed, "
                      f"{inserted} inserted, {missing_local} missing local "
                      f"({time.time() - t0:.0f}s)", flush=True)
    if batch:
        n = _flush_ads(conn, batch, dry_run)
        inserted += n
        skipped += len(batch) - n
    print(f"phase A (ads): {inserted} inserted, {skipped} already present, "
          f"{missing_local} with no local file ({time.time() - t0:.0f}s)")


def _flush_ads(conn, batch, dry_run):
    if dry_run:
        return len(batch)
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR IGNORE INTO file_assets
            (year, month, day, page, kind, filename, ad_uuid,
             local_path, bytes, remote)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)
    conn.commit()
    return cur.rowcount


def populate_local(conn, dry_run=False):
    """Phase B: walk columns/<YYYY-MM-DD>/ for page_raw, page_display,
    and column slices that are still on disk."""
    inserted = 0
    skipped = 0
    issues_scanned = 0
    t0 = time.time()

    for issue_name in sorted(os.listdir(COLUMNS_DIR)):
        ymd = issue_date_from_dir(issue_name)
        if ymd is None:
            continue
        year, month, day = ymd
        issue_dir = os.path.join(COLUMNS_DIR, issue_name)
        if not os.path.isdir(issue_dir):
            continue
        issues_scanned += 1

        batch = []
        for page_name in sorted(os.listdir(issue_dir)):
            page_num = page_num_from_dir(page_name)
            if page_num is None:
                continue   # skip 'ads', '_archive', etc.
            page_dir = os.path.join(issue_dir, page_name)
            if not os.path.isdir(page_dir):
                continue
            for fname in os.listdir(page_dir):
                kind = None
                if fname == "page_raw.png":
                    kind = "page_raw"
                elif fname == "page_display.avif":
                    kind = "page_display"
                elif COL_RE.match(fname):
                    kind = "column"
                if kind is None:
                    continue
                abs_path = os.path.join(page_dir, fname)
                try:
                    bytes_ = os.path.getsize(abs_path)
                except OSError:
                    bytes_ = None
                batch.append((
                    year, month, day, page_num, kind,
                    fname, None, repo_relative(abs_path), bytes_, REMOTE,
                ))

        if batch:
            n = _flush_local(conn, batch, dry_run)
            inserted += n
            skipped += len(batch) - n

        if issues_scanned % 200 == 0:
            print(f"  local: {issues_scanned} issues scanned, "
                  f"{inserted} inserted ({time.time() - t0:.0f}s)", flush=True)

    print(f"phase B (local): {issues_scanned} issues scanned, "
          f"{inserted} inserted, {skipped} already present "
          f"({time.time() - t0:.0f}s)")


def _flush_local(conn, batch, dry_run):
    if dry_run:
        return len(batch)
    cur = conn.cursor()
    cur.executemany("""
        INSERT OR IGNORE INTO file_assets
            (year, month, day, page, kind, filename, ad_uuid,
             local_path, bytes, remote)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)
    conn.commit()
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--phase", choices=("ads", "local", "both"),
                    default="both")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"db not found: {DB_PATH}", file=sys.stderr)
        sys.exit(2)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        if args.phase in ("ads", "both"):
            populate_ads(conn, dry_run=args.dry_run)
        if args.phase in ("local", "both"):
            populate_local(conn, dry_run=args.dry_run)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
