#!/bin/bash
# One-off backfill driver: for every already-backed-up issue
# (issue_backups.md5_verified=1) that has no Drive URLs in file_assets
# yet, run tools/capture_drive_urls.py.
#
# Why this exists:
#   tools/backup_issue.sh now calls capture_drive_urls.py per issue,
#   so newly-backed-up issues get URLs going forward. But the ~1700
#   issues already on Drive predate that wiring and have file_assets
#   rows with drive_url=NULL. This walks them once.
#
# Safe to interrupt: the per-issue capture is idempotent (UPSERT on
# (remote, local_path)). A killed run can be resumed by re-running.
#
# Coexists with the live archiver: capture is read-only against Drive
# and writes only to file_assets, which the archiver doesn't touch.
#
# Usage:
#   nohup bash tools/backfill_drive_urls.sh > /tmp/backfill_urls.log 2>&1 &
#   tools/backfill_drive_urls.sh --limit 5       # process 5 issues then stop

set -u
set -o pipefail

LIMIT=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --limit) LIMIT="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."

# Pull the list of issues that are backed-up-verified but have no Drive
# URLs in file_assets yet. Order by (year, month, day) for deterministic
# resumability.
ISSUES=$(sqlite3 data/mvtm.db <<'SQL'
SELECT printf('%04d-%02d-%02d', b.year, b.month, b.day)
  FROM issue_backups b
 WHERE b.md5_verified = 1
   AND NOT EXISTS (
       SELECT 1 FROM file_assets f
        WHERE f.year=b.year AND f.month=b.month AND f.day=b.day
          AND f.drive_url IS NOT NULL
   )
 ORDER BY b.year, b.month, b.day;
SQL
)

total=$(printf '%s\n' "$ISSUES" | grep -c .)
echo "$(date -Iseconds)  backfill: $total issues to process"

n_done=0
n_failed=0
for issue in $ISSUES; do
    if python3 tools/capture_drive_urls.py "$issue" -v 2>&1; then
        n_done=$((n_done + 1))
    else
        n_failed=$((n_failed + 1))
        echo "  $issue: FAILED"
    fi
    if [[ "$LIMIT" -gt 0 && "$n_done" -ge "$LIMIT" ]]; then
        echo "limit $LIMIT reached"
        break
    fi
    # Light politeness pause — keeps Drive API calls under any
    # rate-limit floor while the archiver is also running.
    sleep 1
done

echo "$(date -Iseconds)  backfill DONE.  ok=$n_done  failed=$n_failed"
