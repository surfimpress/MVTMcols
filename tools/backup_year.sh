#!/bin/bash
# Back up one year's columns/<YYYY>-MM-DD/ trees to Google Drive.
#
# This is now a thin loop over tools/backup_issue.sh — each issue is
# its own independent rclone sync + check + DB UPSERT. If any one
# issue fails the others still run; the script exits non-zero at the
# end so caller scripts (archive_year.sh --with-backup) can see the
# failure.
#
# Why per-issue grain (vs the previous whole-year manifest):
#   - One bad issue doesn't poison a whole year's DB landing.
#   - Partial progress survives interruption — every verified issue
#     has its row before we move on.
#   - Same primitive can be invoked by the cutter pipeline as soon
#     as a single issue finishes cutting.
#
# Usage:
#   ./tools/backup_year.sh 1969
#
# Output:
#   logs/drive_backup_<YYYY-MM-DD>.log    — per-issue rclone log
#   logs/drive_backup_<YEAR>.summary.json — year roll-up
#   data/mvtm.db: one issue_backups row per issue with md5_verified=1
#
# Exit codes:
#   0  every local issue dir backed up + verified + recorded
#   2  bad usage
#   4  no local issue dirs match
#   7  one or more issues failed (see summary for details)

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

ISSUE_SH="tools/backup_issue.sh"
SUMMARY="logs/drive_backup_${YEAR}.summary.json"
mkdir -p logs

if [[ ! -x "$ISSUE_SH" ]]; then
    echo "error: $ISSUE_SH missing or not executable" >&2
    exit 3
fi

shopt -s nullglob
issue_dirs=( "columns/${YEAR}-"*/ )
shopt -u nullglob
if [[ ${#issue_dirs[@]} -eq 0 ]]; then
    echo "error: no local columns/${YEAR}-* dirs — nothing to back up" >&2
    exit 4
fi

echo "──────────────────────────────────────────────────────────────"
echo "  year:     $YEAR"
echo "  issues:   ${#issue_dirs[@]} local issue dirs"
echo "  per-issue logs: logs/drive_backup_${YEAR}-MM-DD.log"
echo "  summary:  $SUMMARY"
echo "──────────────────────────────────────────────────────────────"

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ok_issues=()
fail_issues=()

for d in "${issue_dirs[@]}"; do
    issue="$(basename "${d%/}")"
    if "$ISSUE_SH" "$issue"; then
        ok_issues+=( "$issue" )
    else
        fail_issues+=( "$issue" )
        echo "  ↑ continuing past failure on $issue" >&2
    fi
done

ended_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# JSON-quote helper (issue dates are date-shaped so this is safe).
join_json() {
    local first=1
    printf '['
    for x in "$@"; do
        [[ $first -eq 1 ]] || printf ','
        printf '"%s"' "$x"
        first=0
    done
    printf ']'
}

cat >"$SUMMARY" <<EOF
{
  "year": $YEAR,
  "started_at": "$started_at",
  "ended_at":   "$ended_at",
  "issues_total":  ${#issue_dirs[@]},
  "issues_ok":     ${#ok_issues[@]},
  "issues_failed": ${#fail_issues[@]},
  "ok":     $(join_json "${ok_issues[@]:-}"),
  "failed": $(join_json "${fail_issues[@]:-}")
}
EOF

echo
echo "──────────────────────────────────────────────────────────────"
echo "  $YEAR done.  ok=${#ok_issues[@]}  failed=${#fail_issues[@]}"
echo "  summary: $SUMMARY"
if [[ ${#fail_issues[@]} -gt 0 ]]; then
    echo "  failed issues:"
    for f in "${fail_issues[@]}"; do echo "    $f"; done
    echo "──────────────────────────────────────────────────────────────"
    exit 7
fi
echo "──────────────────────────────────────────────────────────────"
