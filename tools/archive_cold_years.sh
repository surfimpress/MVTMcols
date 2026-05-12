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
MIN_AGE_HOURS=0    # 0 = no freshness guard (passed to archive_issue.sh)
LOOP=0             # 1 = keep re-scanning, sleeping between passes
LOOP_SLEEP=1800    # seconds between scans in --loop mode (30 min default)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-free-gb) TARGET_FREE_GB="$2"; shift 2 ;;
        --limit)          LIMIT="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=1; shift ;;
        --min-age-hours)  MIN_AGE_HOURS="$2"; shift 2 ;;
        --loop)           LOOP=1; shift ;;
        --loop-sleep)     LOOP_SLEEP="$2"; shift 2 ;;
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

# ─── Helpers (re-evaluated per pass when in --loop mode) ─────────────
build_protect_set() {
    # Fresh array each call — assigned to global `protect`.
    protect=()
    if [[ -f data/cut_queue.json ]]; then
        while IFS= read -r y; do
            [[ -n "$y" ]] && protect+=( "$y" )
        done < <(
            python3 -c "import json,sys; print('\n'.join(str(y) for y in json.load(open('data/cut_queue.json'))))" \
                2>/dev/null || true
        )
    fi
    while IFS= read -r y; do
        [[ -n "$y" ]] && protect+=( "$y" )
    done < <(live_cutter_years)
}

build_ranking() {
    # Fresh size ranking each call — assigned to global `ranking`.
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
}

is_protected() {
    local y="$1"
    for p in "${protect[@]:-}"; do
        [[ "$p" == "$y" ]] && return 0
    done
    return 1
}

live_cutter_years() {
    ps -axo command= 2>/dev/null \
        | awk '/cut_corpus\.py/ { for (i=1;i<=NF;i++) if ($i=="--year" && (i+1)<=NF) print $(i+1) }'
}

cutter_campaign_complete() {
    # The cutter supervisor writes this marker when the queue drains
    # AND no cutters are alive. Used as the stop signal for --loop mode
    # so the archiver can keep running until the cut campaign is done.
    [[ -f data/cut_campaign_complete.json ]]
}

# Build the args we'll pass to archive_year.sh once — same for every
# year in every pass.
year_args=( --with-backup )
[[ "$MIN_AGE_HOURS" -gt 0 ]] && year_args=( --with-backup --min-age-hours "$MIN_AGE_HOURS" )

# ─── Header / banner ─────────────────────────────────────────────────
log "started.  target_free_gb=$TARGET_FREE_GB  limit=$LIMIT  dry_run=$DRY_RUN  min_age_hours=$MIN_AGE_HOURS  loop=$LOOP"
log "disk_free_gb_start: $(disk_free_gb)"

# Cumulative counters across all passes.
n_done=0
n_skipped=0
n_failed=0
pass=0

do_one_pass() {
    pass=$((pass + 1))
    build_protect_set
    build_ranking
    log "── pass $pass ── protect=${#protect[@]} years  candidates=${#ranking[@]} years"

    local pass_archived=0
    for entry in "${ranking[@]:-}"; do
        [[ -z "$entry" ]] && continue
        local size_kb year size_gb
        size_kb=$(awk '{print $1}' <<<"$entry")
        year=$(awk '{print $2}' <<<"$entry")
        size_gb=$(awk -v k="$size_kb" 'BEGIN{printf "%.2f", k/1024/1024}')

        if is_protected "$year"; then
            n_skipped=$((n_skipped + 1))
            continue
        fi
        if live_cutter_years | grep -qx "$year"; then
            log "skip $year ($size_gb GB) — cutter started mid-job"
            n_skipped=$((n_skipped + 1))
            continue
        fi

        log "── $year ($size_gb GB) ──"
        if [[ "$DRY_RUN" -eq 1 ]]; then
            bash tools/archive_year.sh --dry-run "$year" 2>&1 | tee -a "$LOG"
            n_done=$((n_done + 1))
            pass_archived=$((pass_archived + 1))
        else
            if bash tools/archive_year.sh "${year_args[@]}" "$year" 2>&1 | tee -a "$LOG"; then
                log "$year ok.  disk_free_gb: $(disk_free_gb)"
                n_done=$((n_done + 1))
                pass_archived=$((pass_archived + 1))
            else
                log "$year FAILED — continuing to next year"
                n_failed=$((n_failed + 1))
            fi
        fi

        if [[ "$LIMIT" -gt 0 && "$n_done" -ge "$LIMIT" ]]; then
            log "limit reached ($LIMIT years archived) — stopping"
            return 10
        fi
        if [[ "$TARGET_FREE_GB" -gt 0 ]]; then
            local free
            free=$(disk_free_gb)
            if awk -v f="$free" -v t="$TARGET_FREE_GB" 'BEGIN{exit !(f>=t)}'; then
                log "target_free_gb $TARGET_FREE_GB reached (free=$free) — stopping"
                return 10
            fi
        fi
    done

    log "pass $pass complete.  archived_in_pass=$pass_archived  total_archived=$n_done"
    return 0
}

# ─── Outer pass loop ─────────────────────────────────────────────────
while true; do
    if do_one_pass; then
        :  # pass finished naturally; either loop again or exit
    else
        # rc=10 means an early-stop condition (LIMIT / TARGET_FREE_GB).
        # That's terminal regardless of --loop mode.
        break
    fi

    if [[ "$LOOP" -ne 1 ]]; then
        break
    fi

    # Loop mode: if there's nothing left to do and the cutter campaign
    # is complete, we're genuinely done. Otherwise sleep and re-scan.
    # ranking is the candidate list AFTER size-sort but BEFORE protect
    # filter — count what would actually be archivable next pass.
    eligible=0
    for entry in "${ranking[@]:-}"; do
        [[ -z "$entry" ]] && continue
        y=$(awk '{print $2}' <<<"$entry")
        if ! is_protected "$y" && ! live_cutter_years | grep -qx "$y"; then
            eligible=$((eligible + 1))
        fi
    done
    if [[ "$eligible" -eq 0 ]] && cutter_campaign_complete; then
        log "loop: nothing eligible AND cutter campaign complete — exiting"
        break
    fi
    log "loop: $eligible eligible candidate(s); sleeping ${LOOP_SLEEP}s before next pass"
    sleep "$LOOP_SLEEP"
done

log "DONE.  archived=$n_done  skipped=$n_skipped  failed=$n_failed  passes=$pass  free_now=$(disk_free_gb) GB"
