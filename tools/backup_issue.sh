#!/bin/bash
# Back up ONE issue's columns/<YYYY-MM-DD>/ tree to Google Drive via
# rclone, with checksum verification, and record the result in
# data/mvtm.db (issue_backups). The smallest unit of backup work.
#
# Why per-issue (not per-year):
#   - Failure of one issue doesn't poison a whole year's run.
#   - Each issue's DB row commits as soon as that issue is verified,
#     so partial progress survives interruption.
#   - Lets us back up an issue immediately after the cutter finishes
#     it, not at the end of the year.
#
# Re-running on an already-backed-up issue is cheap: rclone --checksum
# sees md5s match and skips the bytes; the verify pass + UPSERT still
# refresh verified_at in the DB. Useful for landing DB rows after a
# whole-year backup completed under older code.
#
# Usage:
#   ./tools/backup_issue.sh 1969-04-03
#
# Output:
#   logs/drive_backup_<YYYY-MM-DD>.log    — rclone sync + check log
#   data/mvtm.db: issue_backups row with md5_verified=1
#
# Exit codes:
#   0  ok
#   2  bad usage / bad date arg
#   3  rclone missing / remote unconfigured
#   4  local issue dir missing
#   5  rclone sync failed
#   6  rclone check failed (md5 mismatch)

set -u
set -o pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 YYYY-MM-DD" >&2
    exit 2
fi

ISSUE="$1"
if ! [[ "$ISSUE" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]]; then
    echo "error: ISSUE must be YYYY-MM-DD, got '$ISSUE'" >&2
    exit 2
fi
IY="${BASH_REMATCH[1]}"
IM_PAD="${BASH_REMATCH[2]}"             # zero-padded, for paths
ID_PAD="${BASH_REMATCH[3]}"
IM="$((10#${BASH_REMATCH[2]}))"         # integer, for DB INTEGER columns
ID="$((10#${BASH_REMATCH[3]}))"

cd "$(dirname "$0")/.."

# Remote layout: mvtm:MVTM-corpus-backup/columns/<YEAR>/<MM>/<YEAR-MM-DD>/
# Three levels because Drive's web UI gets sluggish even at a few
# dozen folders per parent — the year-only layout still pushed up to
# ~52 issues per year folder, which is uncomfortable to scroll. The
# month folder cuts that to ~4-5 per parent.
REMOTE_BASE="mvtm:MVTM-corpus-backup/columns"
DB_REMOTE="mvtm:"
SRC="columns/$ISSUE"
DST="$REMOTE_BASE/$IY/$IM_PAD/$ISSUE"
LOG="logs/drive_backup_${ISSUE}.log"
DB="data/mvtm.db"

if ! command -v rclone >/dev/null 2>&1; then
    echo "error: rclone not on PATH" >&2
    exit 3
fi
if ! rclone listremotes 2>/dev/null | grep -qx 'mvtm:'; then
    echo "error: rclone remote 'mvtm:' not configured" >&2
    exit 3
fi
if [[ ! -d "$SRC" ]]; then
    echo "error: $SRC does not exist locally" >&2
    exit 4
fi

mkdir -p logs

n_files=$(find "$SRC" -type f | wc -l | tr -d ' ')
bytes_local=$(du -sk "$SRC" | awk '{print $1*1024}')

# Truncate-then-append: each run starts a fresh log so the file size
# stays bounded if an issue is re-backed-up many times.
: > "$LOG"

# ─── Sync ─────────────────────────────────────────────────────────────
if ! rclone sync --checksum \
        --transfers 8 --checkers 16 \
        --log-file "$LOG" --log-level INFO \
        "$SRC/" "$DST/"; then
    echo "  $ISSUE: sync FAILED — see $LOG" >&2
    exit 5
fi

# ─── Verify ───────────────────────────────────────────────────────────
if ! rclone check --checksum --one-way \
        --log-file "$LOG" --log-level INFO \
        "$SRC/" "$DST/"; then
    echo "  $ISSUE: check FAILED — see $LOG" >&2
    exit 6
fi

# ─── DB UPSERT ────────────────────────────────────────────────────────
verified_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ -f "$DB" ]]; then
    sqlite3 "$DB" <<SQL
INSERT INTO issue_backups
    (year, month, day, remote, file_count, bytes_local,
     md5_verified, backed_up_at, verified_at, manifest_path)
VALUES
    ($IY, $IM, $ID, '$DB_REMOTE', $n_files, $bytes_local,
     1, datetime('now'), '$verified_at', '$LOG')
ON CONFLICT(year, month, day, remote) DO UPDATE SET
    file_count    = excluded.file_count,
    bytes_local   = excluded.bytes_local,
    md5_verified  = 1,
    backed_up_at  = datetime('now'),
    verified_at   = excluded.verified_at,
    manifest_path = excluded.manifest_path;
SQL
else
    echo "warning: $DB not found — skipped DB UPSERT for $ISSUE" >&2
fi

human=$(numfmt --to=iec-i --suffix=B "$bytes_local" 2>/dev/null || echo "$bytes_local bytes")
echo "  $ISSUE  ok  files=$n_files  bytes=$human"
