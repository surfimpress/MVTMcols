#!/usr/bin/env python3
"""Background refresher for columns/disk_stats.json.

`process_issue.py` writes this file as a side-effect of per-issue processing,
on a 5-minute TTL. When cutting is paused but the archiver keeps freeing
disk, the viewer's "disk N GB free" line goes stale. This script keeps it
current by calling the same refresh function on a loop.

Managed by ~/Library/LaunchAgents/com.mvtm.disk_stats.plist.
"""
import os
import sys
import time

REPO = "/Users/peter/Projects/MVTM"
INTERVAL = 300  # seconds — matches the existing TTL in _refresh_disk_stats_if_stale

os.chdir(REPO)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from process_issue import _refresh_disk_stats_if_stale

while True:
    try:
        _refresh_disk_stats_if_stale("columns", max_age_s=0)
    except Exception as e:
        print(f"refresh failed: {e!r}", flush=True)
    time.sleep(INTERVAL)
