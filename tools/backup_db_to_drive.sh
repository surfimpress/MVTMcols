#!/bin/bash
# Back up data/mvtm.db to Drive via a SQLite-safe online snapshot.
#
# Why this exists:
#   data/mvtm.db is no longer tracked in git (it exceeds GitHub's 100 MB
#   hard limit). Drive becomes the off-machine durable copy.
#
#   The DB is in WAL mode and constantly written by the cutter/archiver
#   supervisors. A raw `cp` of mvtm.db can produce a half-written copy
#   (WAL contents not yet checkpointed into the main file). The SQLite
#   `.backup` API does a consistent online snapshot regardless of who
#   else is writing.
#
# What it does:
#   1. Snapshot data/mvtm.db -> /tmp/mvtm-snapshot.db via sqlite3
#      .backup (a few seconds, no writer block needed).
#   2. rclone copy that snapshot to mvtm:MVTM-corpus-backup/db/mvtm.db
#      (overwrites). Drive's built-in versioning retains prior copies
#      for ~30 days on the same file ID, so short-term rollback is free.
#   3. Remove the tmp file.
#
# Schedule: ~/Library/LaunchAgents/com.mvtm.db_backup.plist fires this
# hourly. Manual one-shot is fine too (`tools/backup_db_to_drive.sh`).
#
# Log: /tmp/db_backup.log (appended). Per-run lines are date-stamped.

set -u
set -o pipefail

REPO=/Users/peter/Projects/MVTM
DB="$REPO/data/mvtm.db"
SNAPSHOT=/tmp/mvtm-snapshot.db
REMOTE_PATH="mvtm:MVTM-corpus-backup/db/mvtm.db"
LOG=/tmp/db_backup.log

log() {
    printf '%s  %s\n' "$(date -Iseconds)" "$*" >> "$LOG"
}

cleanup() {
    rm -f "$SNAPSHOT"
}
trap cleanup EXIT

if [[ ! -f "$DB" ]]; then
    log "ERROR: $DB not found"
    exit 2
fi

log "starting backup (db size: $(stat -f '%z' "$DB") bytes)"

if ! sqlite3 "$DB" ".backup '$SNAPSHOT'" 2>>"$LOG"; then
    log "ERROR: sqlite3 .backup failed"
    exit 3
fi

snapshot_size=$(stat -f '%z' "$SNAPSHOT" 2>/dev/null || echo "?")
log "snapshot ok ($snapshot_size bytes); uploading"

if ! rclone copyto "$SNAPSHOT" "$REMOTE_PATH" \
        --transfers 1 --checkers 2 --log-level INFO 2>>"$LOG"; then
    log "ERROR: rclone copyto failed"
    exit 4
fi

log "DONE"
