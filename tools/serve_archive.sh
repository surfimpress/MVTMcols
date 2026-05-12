#!/bin/bash
# serve_archive.sh — expose the Drive-archived columns/ tree over HTTP
# so the viewer can show pages of issues whose local copies have been
# freed by archive_cold_years.sh.
#
# Pairs with the Drive-fallback branch in viewer.html's retryImg():
# when an <img> 404s and backupStatus[issue] is set, the viewer rewrites
# the URL to this server. URL transform:
#
#     /MVTM/columns/<YYYY-MM-DD>/...  →  <ARCHIVE_BASE>/<YYYY>/<MM>/<YYYY-MM-DD>/...
#
# Read-only, bound to localhost. Expose to the public via the existing
# Cloudflare tunnel (separate config) — never bind 0.0.0.0 here.
#
# Cache:
#   --vfs-cache-mode=full so files are downloaded once and reused.
#   --vfs-cache-max-size=4G — enough for ~1 year of preview images
#   without thrashing. Cache dir lives under ~/.cache/rclone by default.
#
# Usage:
#   tools/serve_archive.sh                 # foreground (for testing)
#   nohup tools/serve_archive.sh > /tmp/serve_archive.log 2>&1 &
#
# Logs:
#   /tmp/serve_archive.log (when backgrounded)

set -u
set -o pipefail

REMOTE="mvtm:MVTM-corpus-backup/columns/"
ADDR="${SERVE_ARCHIVE_ADDR:-127.0.0.1:8050}"
CACHE_SIZE="${SERVE_ARCHIVE_CACHE:-4G}"

exec rclone serve http "$REMOTE" \
    --addr "$ADDR" \
    --read-only \
    --vfs-cache-mode full \
    --vfs-cache-max-size "$CACHE_SIZE" \
    --vfs-read-ahead 1M \
    --buffer-size 1M \
    --log-level INFO
