#!/bin/bash
# One-time installer for the cutter supervisor LaunchAgent.
#
# What it does:
#   1. Stops any currently-running refill_cutters.py (the cutters
#      themselves are NOT touched — they're independent processes).
#   2. Copies the plist into ~/Library/LaunchAgents/.
#   3. Loads it with launchctl so it starts now AND on every login.
#
# After install:
#   - The supervisor will be started by launchd whenever the user
#     logs in (covers reboot recovery).
#   - It will be restarted automatically if it crashes (KeepAlive).
#   - When the queue is drained it exits 0 and launchd does NOT
#     restart it (SuccessfulExit=false in the plist).
#
# To remove later:
#   launchctl unload ~/Library/LaunchAgents/com.mvtm.cutters.plist
#   rm ~/Library/LaunchAgents/com.mvtm.cutters.plist

set -e
set -u

REPO=/Users/peter/Projects/MVTM
PLIST_SRC="$REPO/tools/com.mvtm.cutters.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.mvtm.cutters.plist"

if [[ ! -f "$PLIST_SRC" ]]; then
    echo "error: $PLIST_SRC not found" >&2
    exit 2
fi

# Stop any existing manual-run supervisor (PIDs come and go, but the
# command line tag is stable).
echo "Stopping any existing refill_cutters.py …"
pkill -f 'refill_cutters.py' 2>/dev/null || true
sleep 1

# Unload any previous version of the agent (idempotent).
if [[ -f "$PLIST_DST" ]]; then
    echo "Unloading existing LaunchAgent …"
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

mkdir -p "$(dirname "$PLIST_DST")"
cp "$PLIST_SRC" "$PLIST_DST"
echo "Installed plist at $PLIST_DST"

launchctl load -w "$PLIST_DST"
echo "Loaded LaunchAgent."

sleep 2

# Show status
echo
echo "Supervisor status:"
launchctl list | grep com.mvtm.cutters || echo "  (not yet listed — try \`launchctl list | grep mvtm\` in a moment)"

echo
echo "Live cutters now:"
pgrep -af 'cut_corpus.py --year' || echo "  (none yet — supervisor may still be launching them)"

echo
echo "Log: tail -f /tmp/refill_supervisor.log"
