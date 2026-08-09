#!/bin/bash
# Back up mvtm.db and transcribe.db to Drive via SQLite-safe online
# snapshots.
#
# Why this exists:
#   Neither DB is tracked in git -- mvtm.db exceeds GitHub's 100 MB
#   hard limit, and transcribe.db (added 2026-08-09) has no other
#   off-machine copy at all. Drive is the durable copy for both.
#
#   Both DBs are in WAL mode and actively written by their respective
#   supervisors. A raw `cp` can produce a half-written copy (WAL
#   contents not yet checkpointed into the main file). The SQLite
#   `.backup` API does a consistent online snapshot regardless of who
#   else is writing.
#
# What it does, per DB:
#   1. Snapshot -> /tmp/<name>-snapshot.db via sqlite3 .backup (a few
#      seconds, no writer block needed).
#   2. rclone copy that snapshot to mvtm:MVTM-corpus-backup/db/<name>
#      (overwrites). Drive's built-in versioning retains prior copies
#      for ~30 days on the same file ID, so short-term rollback is
#      free.
#   3. Remove the tmp file.
# One DB failing doesn't skip the other -- each is independent; the
# script exits non-zero if any DB failed.
#
# Schedule: ~/Library/LaunchAgents/com.mvtm.db_backup.plist fires this
# daily (StartInterval 86400). Manual one-shot is fine too
# (`tools/backup_db_to_drive.sh`).
#
# Log: /tmp/db_backup.log (appended). Per-run lines are date-stamped.

set -u
set -o pipefail

REPO=/Users/peter/Projects/MVTM
LOG=/tmp/db_backup.log
REMOTE_BASE="mvtm:MVTM-corpus-backup/db"

# name:local-path pairs.
DBS=(
    "mvtm.db:$REPO/data/mvtm.db"
    "transcribe.db:$REPO/transcribe/data/transcribe.db"
)

log() {
    printf '%s  %s\n' "$(date -Iseconds)" "$*" >> "$LOG"
}

backup_one() {
    local name="$1" db="$2"
    local snapshot="/tmp/${name}-snapshot.db"

    if [[ ! -f "$db" ]]; then
        log "ERROR: $db not found"
        return 2
    fi

    log "starting backup of $name (db size: $(stat -f '%z' "$db") bytes)"

    if ! sqlite3 "$db" ".backup '$snapshot'" 2>>"$LOG"; then
        log "ERROR: sqlite3 .backup failed for $name"
        rm -f "$snapshot"
        return 3
    fi

    local snapshot_size
    snapshot_size=$(stat -f '%z' "$snapshot" 2>/dev/null || echo "?")
    log "$name snapshot ok ($snapshot_size bytes); uploading"

    if ! rclone copyto "$snapshot" "$REMOTE_BASE/$name" \
            --transfers 1 --checkers 2 --log-level INFO 2>>"$LOG"; then
        log "ERROR: rclone copyto failed for $name"
        rm -f "$snapshot"
        return 4
    fi

    rm -f "$snapshot"
    log "$name DONE"
    return 0
}

overall=0
for entry in "${DBS[@]}"; do
    name="${entry%%:*}"
    path="${entry#*:}"
    backup_one "$name" "$path" || overall=1
done

exit "$overall"
