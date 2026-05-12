#!/bin/bash
# Restore one year's columns/<YYYY>-MM-DD/ trees from Google Drive.
# Thin loop over tools/restore_issue.sh — see that file for behaviour.
#
# Usage:
#   ./tools/restore_year.sh 1969
#
# Output:
#   logs/drive_restore_<YYYY-MM-DD>.log    — per-issue rclone log
#   logs/drive_restore_<YEAR>.summary.json — year roll-up
#
# Exit codes:
#   0  every issue on the remote restored ok
#   2  bad usage
#   4  no issue dirs for this year on the remote
#   7  one or more issues failed

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

cd "$(dirname "$0")/.."

ISSUE_SH="tools/restore_issue.sh"
REMOTE_BASE="mvtm:MVTM-corpus-backup/columns"
SUMMARY="logs/drive_restore_${YEAR}.summary.json"
mkdir -p logs

if [[ ! -x "$ISSUE_SH" ]]; then
    echo "error: $ISSUE_SH missing or not executable" >&2; exit 3
fi

echo "$(date -Iseconds)  enumerating $YEAR issues on $REMOTE_BASE/$YEAR/ …"
# rclone lsf -R --dirs-only returns relative paths like "01/", "01/1969-01-23/", "02/",
# "02/1969-02-06/" … strip the trailing slash and keep only the leaf dir names
# that match YYYY-MM-DD.
mapfile -t remote_issues < <(
    rclone lsf -R --dirs-only "$REMOTE_BASE/$YEAR/" 2>/dev/null \
        | sed 's:/$::' \
        | awk -F/ '{print $NF}' \
        | grep -E "^${YEAR}-[0-9]{2}-[0-9]{2}$" \
        | sort -u
)

if [[ ${#remote_issues[@]} -eq 0 ]]; then
    echo "error: no $YEAR-* issue dirs found on remote — nothing to restore" >&2
    exit 4
fi

echo "──────────────────────────────────────────────────────────────"
echo "  year:    $YEAR"
echo "  issues:  ${#remote_issues[@]} on remote"
echo "  summary: $SUMMARY"
echo "──────────────────────────────────────────────────────────────"

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ok=()
failed=()

for issue in "${remote_issues[@]}"; do
    if "$ISSUE_SH" "$issue"; then
        ok+=( "$issue" )
    else
        failed+=( "$issue" )
        echo "  ↑ continuing past failure on $issue" >&2
    fi
done

ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

join_json() {
    local first=1; printf '['
    for x in "$@"; do
        [[ $first -eq 1 ]] || printf ','
        printf '"%s"' "$x"; first=0
    done
    printf ']'
}

cat >"$SUMMARY" <<EOF
{
  "year": $YEAR,
  "started_at": "$started_at",
  "ended_at":   "$ended_at",
  "issues_total":  ${#remote_issues[@]},
  "issues_ok":     ${#ok[@]},
  "issues_failed": ${#failed[@]},
  "ok":     $(join_json "${ok[@]:-}"),
  "failed": $(join_json "${failed[@]:-}")
}
EOF

echo
echo "──────────────────────────────────────────────────────────────"
echo "  $YEAR done.  ok=${#ok[@]}  failed=${#failed[@]}"
echo "  summary: $SUMMARY"
if [[ ${#failed[@]} -gt 0 ]]; then
    for f in "${failed[@]}"; do echo "    failed: $f"; done
    echo "──────────────────────────────────────────────────────────────"
    exit 7
fi
echo "──────────────────────────────────────────────────────────────"
