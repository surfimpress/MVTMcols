#!/usr/bin/env python3
"""Capture Drive IDs/URLs for one issue's files into file_assets.

Called from tools/backup_issue.sh after the rclone sync + verify pass.
Walks the remote issue dir via `rclone lsjson --hash`, classifies each
file into one of the four tracked kinds (ad / column / page_raw /
page_display), and UPSERTs into file_assets matching on
(remote, local_path).

Files not in those four kinds are skipped — body_blur.png,
page_analysis.json, page_cv.{json,npz}, page_meta.json, .DS_Store are
intermediate/computed artefacts we don't surface in the viewer.

For ad rows, ad_uuid is looked up from detected_ads on insert so the
file_assets row can join cleanly back to the ad's metadata.

Usage:
    python3 tools/capture_drive_urls.py 1924-05-30

Exits 0 on success, 2 on bad args, 3 on rclone failure.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

REPO = "/Users/peter/Projects/MVTM"
DB_PATH = os.path.join(REPO, "data/mvtm.db")
REMOTE = "mvtm:"
REMOTE_BASE = "mvtm:MVTM-corpus-backup/columns"
DRIVE_URL_TMPL = "https://drive.google.com/file/d/{id}/view"

# Recognised filename shapes. Mirror the populator's set so the two
# stay in lockstep.
COL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}_col\d+\.png$")
AD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}_(sc_)?ad\d+\.png$")
PAGE_DIR_RE = re.compile(r"^p\d+$")


def classify(path_in_issue):
    """rclone Path -> (kind, page, filename) or (None, None, None) when
    we don't track this file type.

    Path shapes:
        p<N>/page_raw.png       -> ('page_raw', N, 'page_raw.png')
        p<N>/page_display.avif  -> ('page_display', N, ...)
        p<N>/<date>-<NN>_col<M>.png -> ('column', N, ...)
        ads/p<N>/<date>-<NN>_(sc_)?ad<M>.png -> ('ad', N, ...)
    Everything else returns (None, None, None).
    """
    parts = path_in_issue.split("/")
    if len(parts) == 2 and PAGE_DIR_RE.fullmatch(parts[0]):
        page = int(parts[0][1:])
        fname = parts[1]
        if fname == "page_raw.png":
            return "page_raw", page, fname
        if fname == "page_display.avif":
            return "page_display", page, fname
        if COL_RE.match(fname):
            return "column", page, fname
        return None, None, None
    if (len(parts) == 3 and parts[0] == "ads"
            and PAGE_DIR_RE.fullmatch(parts[1])):
        page = int(parts[1][1:])
        fname = parts[2]
        if AD_RE.match(fname):
            return "ad", page, fname
    return None, None, None


def capture(issue, conn, verbose=False):
    year, month, day = (int(p) for p in issue.split("-"))
    remote_path = f"{REMOTE_BASE}/{year:04d}/{month:02d}/{issue}/"

    try:
        proc = subprocess.run(
            ["rclone", "lsjson", "--recursive", "--files-only", "--hash",
             remote_path],
            capture_output=True, text=True, check=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        print(f"rclone lsjson failed for {issue}: rc={e.returncode}",
              file=sys.stderr)
        if e.stderr:
            print(e.stderr.strip(), file=sys.stderr)
        return 3
    except subprocess.TimeoutExpired:
        print(f"rclone lsjson timed out for {issue}", file=sys.stderr)
        return 3

    entries = json.loads(proc.stdout) if proc.stdout.strip() else []
    cur = conn.cursor()

    upserted = ignored = 0
    for ent in entries:
        kind, page, fname = classify(ent["Path"])
        if kind is None:
            ignored += 1
            continue
        local_path = f"columns/{issue}/{ent['Path']}"
        drive_id = ent.get("ID")
        if not drive_id:
            # Some backends don't expose an ID; without one we have no
            # URL to record. Skip rather than insert a useless row.
            ignored += 1
            continue
        drive_url = DRIVE_URL_TMPL.format(id=drive_id)
        drive_md5 = (ent.get("Hashes") or {}).get("md5")
        size = ent.get("Size")

        ad_uuid = None
        if kind == "ad":
            row = cur.execute(
                "SELECT uuid FROM detected_ads "
                "WHERE year=? AND month=? AND day=? AND page=? "
                "  AND image_filename=?",
                (year, month, day, page, fname),
            ).fetchone()
            if row:
                ad_uuid = row[0]

        cur.execute("""
            INSERT INTO file_assets
                (year, month, day, page, kind, filename, ad_uuid,
                 local_path, bytes, remote,
                 drive_id, drive_url, drive_md5,
                 synced_at, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    datetime('now'), datetime('now'))
            ON CONFLICT(remote, local_path) DO UPDATE SET
                drive_id    = excluded.drive_id,
                drive_url   = excluded.drive_url,
                drive_md5   = excluded.drive_md5,
                bytes       = COALESCE(file_assets.bytes, excluded.bytes),
                ad_uuid     = COALESCE(file_assets.ad_uuid, excluded.ad_uuid),
                synced_at   = excluded.synced_at,
                verified_at = excluded.verified_at
        """, (year, month, day, page, kind, fname, ad_uuid,
              local_path, size, REMOTE,
              drive_id, drive_url, drive_md5))
        upserted += 1

    conn.commit()

    if verbose:
        # Report coverage so the operator can spot when the populator
        # is out of sync with what's actually on Drive.
        with_drive = cur.execute(
            "SELECT COUNT(*) FROM file_assets "
            " WHERE year=? AND month=? AND day=? AND drive_id IS NOT NULL",
            (year, month, day),
        ).fetchone()[0]
        total = cur.execute(
            "SELECT COUNT(*) FROM file_assets "
            " WHERE year=? AND month=? AND day=?",
            (year, month, day),
        ).fetchone()[0]
        print(f"  {issue}: drive URLs upserted={upserted} "
              f"ignored={ignored}  coverage={with_drive}/{total}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", help="YYYY-MM-DD")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.issue):
        print("error: ISSUE must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(2)
    if not os.path.exists(DB_PATH):
        print(f"db not found: {DB_PATH}", file=sys.stderr)
        sys.exit(2)

    conn = sqlite3.connect(DB_PATH)
    try:
        rc = capture(args.issue, conn, verbose=args.verbose)
    finally:
        conn.close()
    sys.exit(rc)


if __name__ == "__main__":
    main()
