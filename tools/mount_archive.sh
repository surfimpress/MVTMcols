#!/bin/bash
# mount_archive.sh — mount mvtm:MVTM-corpus-backup/columns/ at
# columns_archive/ so the existing serve.py / Cloudflare tunnel can
# serve archived issues as ordinary files. Same-origin from the
# viewer's point of view: no proxy, no second hostname.
#
# Uses `rclone nfsmount` rather than `rclone mount` so we don't need
# macFUSE (no kernel extension, no reboot). Rclone spawns a tiny NFS
# server on a local port and asks the system NFS client to mount it.
#
# Read-only, daemonised, 4 GB VFS cache. Idempotent — won't re-mount
# if the directory is already a live mountpoint.
#
# Usage:
#   tools/mount_archive.sh         # mount (idempotent)
#   tools/mount_archive.sh unmount # umount
#
# Logs: /tmp/mount_archive.log

set -u

cd "$(dirname "$0")/.."

MOUNT="columns_archive"
REMOTE="mvtm:MVTM-corpus-backup/columns/"
LOG="/tmp/mount_archive.log"
CACHE_SIZE="${MOUNT_ARCHIVE_CACHE:-4G}"

if [[ "${1:-}" == "unmount" || "${1:-}" == "umount" ]]; then
    if mount | grep -q " on .*$MOUNT (nfs"; then
        umount "$MOUNT" && echo "unmounted $MOUNT" || {
            echo "umount failed — try: sudo umount -f $MOUNT" >&2; exit 1
        }
    else
        echo "$MOUNT not mounted"
    fi
    exit 0
fi

# Already mounted? Nothing to do.
if mount | grep -q " on .*$MOUNT (nfs"; then
    echo "$MOUNT already mounted"
    exit 0
fi

mkdir -p "$MOUNT"

# Refuse to mount on top of a non-empty dir — that would hide local files.
if [[ -n "$(ls -A "$MOUNT" 2>/dev/null)" ]]; then
    echo "error: $MOUNT is not empty — refusing to mount over it" >&2
    exit 2
fi

echo "$(date -Iseconds)  mounting $REMOTE at $MOUNT" >> "$LOG"

rclone nfsmount "$REMOTE" "$MOUNT" \
    --daemon \
    --read-only \
    --vfs-cache-mode full \
    --vfs-cache-max-size "$CACHE_SIZE" \
    --vfs-read-ahead 1M \
    --dir-cache-time 24h \
    --poll-interval 0 \
    --log-file "$LOG" \
    --log-level INFO

# Verify within a short window — nfsmount returns before the NFS
# handshake completes.
for i in 1 2 3 4 5; do
    if mount | grep -q " on .*$MOUNT (nfs"; then
        echo "mounted: $REMOTE → $MOUNT"
        # Kickstart the shared dev server so it picks up the mount
        # cleanly. A process that was running before the mount
        # appeared sees an empty pre-mount inode for paths inside
        # the mountpoint and can stat() directories but not open()
        # files inside — restart fixes it. launchd respawns it
        # automatically (KeepAlive=true). Idempotent: if the server
        # isn't loaded, kickstart -k is a no-op.
        launchctl kickstart -k "gui/$(id -u)/com.surfaceimpression.projects-server" \
            >> "$LOG" 2>&1 || true
        exit 0
    fi
    sleep 1
done
echo "error: mount did not appear within 5s — check $LOG" >&2
exit 3
