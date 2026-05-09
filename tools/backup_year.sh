#!/bin/bash
# Back up one year's columns/<YYYY>-MM-DD/ trees to Google Drive via
# rclone, with checksum verification.
#
# Prerequisites (one-time):
#   brew install rclone
#   rclone config           # add a Drive remote named exactly `mvtm`
#
# Usage:
#   ./tools/backup_year.sh 1969
#
# Output:
#   logs/drive_backup_<YEAR>_<stamp>.log     — rclone sync log
#   logs/drive_check_<YEAR>_<stamp>.log      — rclone check log
#   logs/drive_backup_<YEAR>.manifest.json   — audit record
#
# Re-running is safe: rclone sync skips files already on Drive whose
# md5 matches the local file. Resume after interruption is automatic.

set -u
set -o pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 YYYY" >&2
    exit 2
fi

YEAR="$1"
if ! [[ "$YEAR" =~ ^[0-9]{4}$ ]]; then
    echo "error: YEAR must be a 4-digit year, got '$YEAR'" >&2
    exit 2
fi

REMOTE="mvtm:MVTM-corpus-backup/columns"
SRC="columns"
INCLUDE="${YEAR}-*/**"
STAMP="$(date +%Y%m%d-%H%M%S)"
SYNC_LOG="logs/drive_backup_${YEAR}_${STAMP}.log"
CHECK_LOG="logs/drive_check_${YEAR}_${STAMP}.log"
MANIFEST="logs/drive_backup_${YEAR}.manifest.json"

cd "$(dirname "$0")/.."   # project root

# ─── Sanity checks ────────────────────────────────────────────────────
if ! command -v rclone >/dev/null 2>&1; then
    echo "error: rclone not on PATH (brew install rclone)" >&2
    exit 3
fi

if ! rclone listremotes 2>/dev/null | grep -qx 'mvtm:'; then
    echo "error: rclone remote 'mvtm:' not configured." >&2
    echo "       run: rclone config   (add a Google Drive remote named 'mvtm')" >&2
    exit 3
fi

# Match the inclusion pattern locally to count what we're about to push
# (avoids surprising the user with an unexpectedly large or empty year).
shopt -s nullglob
issue_dirs=( "$SRC"/${YEAR}-*/ )
shopt -u nullglob
if [[ ${#issue_dirs[@]} -eq 0 ]]; then
    echo "error: no local issue dirs match $SRC/${YEAR}-* — nothing to back up" >&2
    exit 4
fi

n_files=$(find "${issue_dirs[@]}" -type f | wc -l | tr -d ' ')
bytes_local=$(du -sk "${issue_dirs[@]}" | awk '{s+=$1} END {print s*1024}')

echo "──────────────────────────────────────────────────────────────"
echo "  year:        $YEAR"
echo "  issues:      ${#issue_dirs[@]}"
echo "  files:       $n_files"
echo "  bytes:       $bytes_local  ($(numfmt --to=iec-i --suffix=B "$bytes_local" 2>/dev/null || echo "$bytes_local bytes"))"
echo "  destination: $REMOTE/"
echo "  sync log:    $SYNC_LOG"
echo "  check log:   $CHECK_LOG"
echo "──────────────────────────────────────────────────────────────"
echo

# ─── Sync ─────────────────────────────────────────────────────────────
# --checksum:   compare md5, not mod-time/size — defends against the
#               failure mode the user hit with the Drive web UI.
# --transfers / --checkers: comfortably under Drive's per-account
#               concurrency budget.
echo "$(date -Iseconds)  starting rclone sync"
if ! rclone sync --checksum \
        --progress --transfers 8 --checkers 16 \
        --log-file "$SYNC_LOG" --log-level INFO \
        "$SRC/" "$REMOTE/" \
        --include "$INCLUDE"; then
    echo "rclone sync FAILED — see $SYNC_LOG" >&2
    # write a manifest noting the failure so the trail isn't silent
    cat >"$MANIFEST" <<EOF
{
  "year": $YEAR,
  "issues": ${#issue_dirs[@]},
  "files": $n_files,
  "bytes_local": $bytes_local,
  "rclone_sync_log": "$SYNC_LOG",
  "rclone_check_log": null,
  "verified": false,
  "verified_at": null,
  "error": "sync_failed"
}
EOF
    exit 5
fi

# ─── Verify ───────────────────────────────────────────────────────────
# --one-way: every local file must exist on Drive with matching md5;
#            extra files on Drive are tolerated.
echo "$(date -Iseconds)  starting rclone check"
if ! rclone check --checksum --one-way \
        --log-file "$CHECK_LOG" --log-level INFO \
        "$SRC/" "$REMOTE/" \
        --include "$INCLUDE"; then
    echo "rclone check FAILED — see $CHECK_LOG" >&2
    cat >"$MANIFEST" <<EOF
{
  "year": $YEAR,
  "issues": ${#issue_dirs[@]},
  "files": $n_files,
  "bytes_local": $bytes_local,
  "rclone_sync_log": "$SYNC_LOG",
  "rclone_check_log": "$CHECK_LOG",
  "verified": false,
  "verified_at": null,
  "error": "verify_failed"
}
EOF
    exit 6
fi

# ─── Manifest ─────────────────────────────────────────────────────────
verified_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat >"$MANIFEST" <<EOF
{
  "year": $YEAR,
  "issues": ${#issue_dirs[@]},
  "files": $n_files,
  "bytes_local": $bytes_local,
  "rclone_sync_log": "$SYNC_LOG",
  "rclone_check_log": "$CHECK_LOG",
  "verified": true,
  "verified_at": "$verified_at"
}
EOF

echo
echo "──────────────────────────────────────────────────────────────"
echo "  $YEAR backed up + verified."
echo "  manifest: $MANIFEST"
echo "──────────────────────────────────────────────────────────────"
