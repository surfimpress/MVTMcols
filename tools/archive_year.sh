#!/bin/bash
# Free local disk for one year by deleting columns/<YYYY>-MM-DD/ trees.
# Thin loop over tools/archive_issue.sh — each issue carries its own
# interlock check against issue_backups (md5_verified=1). If any local
# issue is unbacked it is skipped, the other issues still archive, and
# the script exits non-zero so the caller knows the year isn't fully
# freed.
#
# Usage:
#   ./tools/archive_year.sh 1969               # delete-only (interlock-checked)
#   ./tools/archive_year.sh --with-backup 1969 # back up each issue first
#   ./tools/archive_year.sh --dry-run 1969     # preview deletions
#
# What is NOT deleted:
#   - data/mvtm.db rows (page_layouts, detected_ads etc.) — small, and
#     the source of truth for the cuts.
#   - logs/, manifests, viewer state.
#   - Any local issue dir without md5_verified=1.
#
# To bring a year back: ./tools/restore_year.sh YYYY

set -u
set -o pipefail

WITH_BACKUP=0
DRY_RUN=0
YEAR=""

for arg in "$@"; do
    case "$arg" in
        --with-backup) WITH_BACKUP=1 ;;
        --dry-run)     DRY_RUN=1 ;;
        [0-9][0-9][0-9][0-9]) YEAR="$arg" ;;
        *) echo "usage: $0 [--with-backup] [--dry-run] YYYY" >&2; exit 2 ;;
    esac
done

if [[ -z "$YEAR" ]]; then
    echo "usage: $0 [--with-backup] [--dry-run] YYYY" >&2; exit 2
fi

cd "$(dirname "$0")/.."

ISSUE_SH="tools/archive_issue.sh"
if [[ ! -x "$ISSUE_SH" ]]; then
    echo "error: $ISSUE_SH missing or not executable" >&2; exit 3
fi

shopt -s nullglob
local_dirs=( "columns/${YEAR}-"*/ )
shopt -u nullglob
if [[ ${#local_dirs[@]} -eq 0 ]]; then
    echo "no local columns/${YEAR}-* dirs — nothing to archive"
    exit 0
fi

echo "──────────────────────────────────────────────────────────────"
echo "  year:   $YEAR"
echo "  issues: ${#local_dirs[@]} local issue dirs"
if [[ "$WITH_BACKUP" -eq 1 ]]; then echo "  mode:   --with-backup (per-issue)"; fi
if [[ "$DRY_RUN"     -eq 1 ]]; then echo "  mode:   DRY RUN (no deletions)"; fi
echo "──────────────────────────────────────────────────────────────"

args=()
[[ "$WITH_BACKUP" -eq 1 ]] && args+=( --with-backup )
[[ "$DRY_RUN"     -eq 1 ]] && args+=( --dry-run )

archived=()
skipped=()
for d in "${local_dirs[@]}"; do
    issue="$(basename "${d%/}")"
    if "$ISSUE_SH" "${args[@]}" "$issue"; then
        archived+=( "$issue" )
    else
        skipped+=( "$issue" )
    fi
done

echo
echo "──────────────────────────────────────────────────────────────"
echo "  $YEAR done.  archived=${#archived[@]}  skipped=${#skipped[@]}"
if [[ ${#skipped[@]} -gt 0 ]]; then
    for s in "${skipped[@]}"; do echo "    skipped: $s"; done
    echo "──────────────────────────────────────────────────────────────"
    exit 7
fi
echo "──────────────────────────────────────────────────────────────"
