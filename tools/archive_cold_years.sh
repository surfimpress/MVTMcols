#!/bin/bash
# archive_cold_years.sh — bulk-archive low-priority years to Drive
# and free local disk. The point-of-the-spear of the hot/cold model.
#
# Walks every year present under columns/, sorts by GB DESC, and runs
# archive_year.sh --with-backup on each one — but skips:
#   - any year listed in data/cut_queue.json (planned or in-progress cuts)
#   - any year a live cutter is currently working on (matched via ps)
#
# Stops when --target-free-gb is reached, or after --limit years
# archived, whichever comes first. Default: keep going until the
# eligible inventory is exhausted.
#
# Designed to be left running under nohup overnight. Per-issue safety
# is enforced inside archive_issue.sh (md5_verified=1 interlock); this
# wrapper just orders the work.
#
# Usage:
#   nohup tools/archive_cold_years.sh > /tmp/archive_cold.out 2>&1 &
#   tools/archive_cold_years.sh --dry-run                  # plan only
#   tools/archive_cold_years.sh --target-free-gb 70        # stop at 70 GB free
#   tools/archive_cold_years.sh --limit 5                  # archive 5 years then stop
#
# Output:
#   logs/archive_cold_<stamp>.log   — top-level orchestration log
#   logs/drive_backup_<DATE>.log    — per-issue rclone log (from
#                                     backup_issue.sh)
#   data/mvtm.db: one issue_backups row per archived issue

set -u
set -o pipefail

TARGET_FREE_GB=0   # 0 = no early stop
LIMIT=0            # 0 = no limit
DRY_RUN=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-free-gb) TARGET_FREE_GB="$2"; shift 2 ;;
        --limit)          LIMIT="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=1; shift ;;
        -h|--help)
            sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."
mkdir -p logs

LOG="logs/archive_cold_$(date +%Y%m%d-%H%M%S).log"

log() {
    printf '%s  %s\n' "$(date -Iseconds)" "$*" | tee -a "$LOG"
}

disk_free_gb() {
    df -k /Users/peter/Projects/MVTM | awk 'NR==2{printf "%.2f", $4/1024/1024}'
}

# Make sure interrupted runs leave a visible end-of-log marker so the
# user can tell whether the loop quit cleanly or was killed mid-year.
trap 'log "INTERRUPTED.  free_now=$(disk_free_gb) GB.  archived $n_done years."; exit 130' INT TERM

# ─── Build protect set ───────────────────────────────────────────────
protect=()

# Cut queue (planned years, never archive even if local files exist).
if [[ -f data/cut_queue.json ]]; then
    while IFS= read -r y; do
        [[ -n "$y" ]] && protect+=( "$y" )
    done < <(
        python3 -c "import json,sys; print('\n'.join(str(y) for y in json.load(open('data/cut_queue.json'))))" \
            2>/dev/null || true
    )
fi

# Live cutters (snapshot at start; re-checked per year below).
while IFS= read -r y; do
    [[ -n "$y" ]] && protect+=( "$y" )
done < <(
    ps -axo command= 2>/dev/null \
        | awk '/cut_corpus\.py/ { for (i=1;i<=NF;i++) if ($i=="--year" && (i+1)<=NF) print $(i+1) }'
)

# Membership in protect[] tolerates duplicates (cheap linear scan
# per year), so we don't bother de-duping. Skipping declare -A keeps
# us compatible with macOS's stock bash 3.2.

is_protected() {
    local y="$1"
    for p in "${protect[@]:-}"; do
        [[ "$p" == "$y" ]] && return 0
    done
    return 1
}

# Re-scan live cutters before each year, so that if the user kicks off
# a new cut mid-job we don't archive its target out from under it.
live_cutter_years() {
    ps -axo command= 2>/dev/null \
        | awk '/cut_corpus\.py/ { for (i=1;i<=NF;i++) if ($i=="--year" && (i+1)<=NF) print $(i+1) }'
}

# ─── Build size-sorted year ranking ──────────────────────────────────
# Aggregate per-issue du -sk into per-year totals, sort DESC by KB.
# bash 3.2 has no mapfile; read in a loop.
ranking=()
while IFS= read -r line; do
    [[ -n "$line" ]] && ranking+=( "$line" )
done < <(
    du -sk columns/[0-9][0-9][0-9][0-9]-*/ 2>/dev/null \
        | awk '{ p=$2; gsub("/$","",p); gsub(".*/","",p); y=substr(p,1,4);
                 if (y ~ /^[0-9]{4}$/) s[y]+=$1 }
               END { for (y in s) printf "%d %s\n", s[y], y }' \
        | sort -rn
)

# ─── Header / banner ─────────────────────────────────────────────────
log "started.  target_free_gb=$TARGET_FREE_GB  limit=$LIMIT  dry_run=$DRY_RUN"
log "protect set (${#protect[@]} years): ${protect[*]:-}"
log "ranked candidates: ${#ranking[@]} years on disk"
log "disk_free_gb_start: $(disk_free_gb)"

# ─── Main loop ───────────────────────────────────────────────────────
n_done=0
n_skipped=0
n_failed=0
for entry in "${ranking[@]}"; do
    size_kb=$(awk '{print $1}' <<<"$entry")
    year=$(awk '{print $2}' <<<"$entry")
    size_gb=$(awk -v k="$size_kb" 'BEGIN{printf "%.2f", k/1024/1024}')

    if is_protected "$year"; then
        log "skip $year ($size_gb GB) — protected (queue or live cutter)"
        n_skipped=$((n_skipped + 1))
        continue
    fi

    # Re-check live cutters in case the user just started one.
    if live_cutter_years | grep -qx "$year"; then
        log "skip $year ($size_gb GB) — cutter started mid-job"
        n_skipped=$((n_skipped + 1))
        continue
    fi

    log "── $year ($size_gb GB) ──"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        bash tools/archive_year.sh --dry-run "$year" 2>&1 | tee -a "$LOG"
        n_done=$((n_done + 1))
    else
        if bash tools/archive_year.sh --with-backup "$year" 2>&1 | tee -a "$LOG"; then
            log "$year ok.  disk_free_gb: $(disk_free_gb)"
            n_done=$((n_done + 1))
        else
            log "$year FAILED — continuing to next year"
            n_failed=$((n_failed + 1))
        fi
    fi

    # ─── Stop conditions ────────────────────────────────────────────
    if [[ "$LIMIT" -gt 0 && "$n_done" -ge "$LIMIT" ]]; then
        log "limit reached ($LIMIT years archived) — stopping"
        break
    fi

    if [[ "$TARGET_FREE_GB" -gt 0 ]]; then
        free=$(disk_free_gb)
        if awk -v f="$free" -v t="$TARGET_FREE_GB" 'BEGIN{exit !(f>=t)}'; then
            log "target_free_gb $TARGET_FREE_GB reached (free=$free) — stopping"
            break
        fi
    fi
done

log "DONE.  archived=$n_done  skipped=$n_skipped  failed=$n_failed  free_now=$(disk_free_gb) GB"
