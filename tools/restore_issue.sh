#!/bin/bash
# Restore ONE issue's columns/<YYYY-MM-DD>/ tree from Google Drive
# back to local disk. Counterpart to tools/backup_issue.sh.
#
# Behaviour:
#   - rclone copy (NOT sync) so we never delete local files that
#     exist only on this machine (e.g. unbacked work-in-progress).
#   - --checksum so md5 governs whether each file is re-fetched.
#   - Resume after interruption is automatic.
#   - Does NOT touch the DB. issue_backups still describes the remote;
#     local presence is observable via the filesystem.
#
# Usage:
#   ./tools/restore_issue.sh 1969-04-03

set -u
set -o pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 YYYY-MM-DD" >&2
    exit 2
fi

ISSUE="$1"
if ! [[ "$ISSUE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "error: ISSUE must be YYYY-MM-DD, got '$ISSUE'" >&2
    exit 2
fi

cd "$(dirname "$0")/.."

# Remote layout: mvtm:MVTM-corpus-backup/columns/<YEAR>/<MM>/<YEAR-MM-DD>/
# (See backup_issue.sh for the rationale.)
IFS='-' read -r IY IM_PAD _ <<<"$ISSUE"
REMOTE_BASE="mvtm:MVTM-corpus-backup/columns"
SRC="$REMOTE_BASE/$IY/$IM_PAD/$ISSUE"
DST="columns/$ISSUE"
LOG="logs/drive_restore_${ISSUE}.log"
mkdir -p logs columns

if ! command -v rclone >/dev/null 2>&1; then
    echo "error: rclone not on PATH" >&2; exit 3
fi
if ! rclone listremotes 2>/dev/null | grep -qx 'mvtm:'; then
    echo "error: rclone remote 'mvtm:' not configured" >&2; exit 3
fi

# Sanity-check the remote dir exists before we make an empty local one.
if ! rclone lsf --dirs-only "$REMOTE_BASE/$IY/$IM_PAD/" 2>/dev/null | grep -qx "${ISSUE}/"; then
    echo "error: $SRC not present on remote — nothing to restore" >&2
    exit 4
fi

: > "$LOG"
if ! rclone copy --checksum \
        --transfers 8 --checkers 16 \
        --log-file "$LOG" --log-level INFO \
        "$SRC/" "$DST/"; then
    echo "  $ISSUE: restore FAILED — see $LOG" >&2
    exit 5
fi

n_files=$(find "$DST" -type f | wc -l | tr -d ' ')
echo "  $ISSUE  restored  files=$n_files"
