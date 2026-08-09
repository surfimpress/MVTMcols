#!/usr/bin/env python3
"""Background refresher for transcribe/ocr_llm_stats.json.

Keeps the OCR+LLM route monitor (transcribe/ocr_llm_monitor.html)
current without any manual invocation and without the monitor page
itself ever touching the database -- only this script queries
transcribe.db, on its own slow interval, independent of whatever
transcription Workflow is or isn't running at the time.

Managed by ~/Library/LaunchAgents/com.mvtm.ocr_llm_stats.plist.
"""
import os
import sys
import time

REPO = "/Users/peter/Projects/MVTM"
INTERVAL = 60  # seconds -- a handful of aggregate SQL queries, cheap at this scale

os.chdir(REPO)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from transcribe.build_ocr_llm_stats import main as build_stats

while True:
    try:
        build_stats()
    except Exception as e:
        print(f"refresh failed: {e!r}", flush=True)
    time.sleep(INTERVAL)
