#!/bin/bash
# Free local disk by deleting ONE issue's columns/<YYYY-MM-DD>/ tree,
# but ONLY if a matching issue_backups row exists with md5_verified=1
# for the configured remote. The DB row is the safety interlock —
# no row, no delete.
#
# Use cases:
#   - Targeted purge of one cold issue.
#   - Building block invoked by tools/archive_year.sh per issue.
#
# Usage:
#   ./tools/archive_issue.sh 1969-04-03              # delete (interlock-checked)
#   ./tools/archive_issue.sh --with-backup 1969-04-03 # back up first, then delete
#   ./tools/archive_issue.sh --dry-run 1969-04-03    # show what would be deleted
#
# Exit codes:
#   0  archived (or dry-run preview)
#   2  bad usage
#   4  --with-backup failed
#   5  no md5_verified=1 row in issue_backups for this issue
#   6  local dir already gone

set -u
set -o pipefail

WITH_BACKUP=0
DRY_RUN=0
ISSUE=""

for arg in "$@"; do
    case "$arg" in
        --with-backup) WITH_BACKUP=1 ;;
        --dry-run)     DRY_RUN=1 ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) ISSUE="$arg" ;;
        *) echo "usage: $0 [--with-backup] [--dry-run] YYYY-MM-DD" >&2; exit 2 ;;
    esac
done

if [[ -z "$ISSUE" ]]; then
    echo "usage: $0 [--with-backup] [--dry-run] YYYY-MM-DD" >&2; exit 2
fi
if ! [[ "$ISSUE" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})$ ]]; then
    echo "error: ISSUE must be YYYY-MM-DD, got '$ISSUE'" >&2; exit 2
fi
IY="${BASH_REMATCH[1]}"; IM="$((10#${BASH_REMATCH[2]}))"; ID="$((10#${BASH_REMATCH[3]}))"

cd "$(dirname "$0")/.."

DB="data/mvtm.db"
LOCAL="columns/$ISSUE"
REMOTE="mvtm:"

if [[ ! -f "$DB" ]]; then
    echo "error: $DB not found" >&2; exit 3
fi

if [[ "$WITH_BACKUP" -eq 1 ]]; then
    if ! bash tools/backup_issue.sh "$ISSUE"; then
        echo "  $ISSUE: --with-backup failed — refusing to archive" >&2
        exit 4
    fi
fi

if [[ ! -d "$LOCAL" ]]; then
    echo "  $ISSUE: $LOCAL already absent — nothing to do"
    exit 6
fi

verified=$(sqlite3 "$DB" \
    "SELECT md5_verified FROM issue_backups
     WHERE year=$IY AND month=$IM AND day=$ID AND remote='$REMOTE';")
if [[ "$verified" != "1" ]]; then
    echo "  $ISSUE: REFUSING TO DELETE — no md5_verified=1 row for $REMOTE" >&2
    echo "         run: ./tools/backup_issue.sh $ISSUE" >&2
    exit 5
fi

bytes=$(du -sk "$LOCAL" | awk '{print $1*1024}')
human=$(numfmt --to=iec-i --suffix=B "$bytes" 2>/dev/null || echo "$bytes bytes")

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  $ISSUE  would rm -rf $LOCAL  ($human)"
else
    rm -rf "$LOCAL"
    echo "  $ISSUE  archived  freed=$human"
fi
