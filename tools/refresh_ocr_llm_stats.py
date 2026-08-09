#!/usr/bin/env python3
"""Background refresher for transcribe/ocr_llm_stats.json,
transcribe/entities.json, and transcribe/terminology_review.json.

Keeps the monitor pages (ocr_llm_monitor.html, entities.html,
terminology_review.html) current without any manual invocation and
without any page ever touching the database -- only this script
queries transcribe.db, on its own slow interval, independent of
whatever transcription Workflow is or isn't running at the time.

Each refresh runs as its own subprocess (`python3 -m
transcribe.build_X`), not an in-process function call. This is
deliberate, not incidental: a long-lived process that imports a
build module once at start and calls it in a loop will keep running
that exact code forever, even after the .py file on disk changes --
bit this project three times in one session (2026-08-09) before the
fix landed here. A subprocess re-imports fresh from disk on every
single invocation, so there is no in-memory code to go stale --
editing a build_*.py file takes effect on the very next cycle, no
`launchctl unload`/`load -w` dance required.

Managed by ~/Library/LaunchAgents/com.mvtm.ocr_llm_stats.plist.
"""
import subprocess
import sys
import time

REPO = "/Users/peter/Projects/MVTM"
INTERVAL = 60  # seconds -- a handful of aggregate SQL queries, cheap at this scale

BUILD_MODULES = [
    "transcribe.build_ocr_llm_stats",
    "transcribe.build_entities_stats",
    "transcribe.build_terminology_review_stats",
]

# Note: this only rebuilds terminology_review.json from whatever's
# already in terminology_reviews (a cheap local query) -- it does NOT
# run transcribe.terminology_cleanup itself, which makes live SPARQL
# calls against nomenclature.info and takes ~45s. That's a separate,
# deliberately-invoked (or separately-scheduled) pass -- see
# transcribe/terminology_cleanup.py's docstring.

while True:
    for module in BUILD_MODULES:
        try:
            subprocess.run(
                [sys.executable, "-m", module],
                cwd=REPO, check=True, capture_output=True, text=True, timeout=30,
            )
        except subprocess.CalledProcessError as e:
            print(f"{module} refresh failed (exit {e.returncode}): {e.stderr}", flush=True)
        except subprocess.TimeoutExpired:
            print(f"{module} refresh timed out after 30s", flush=True)
    time.sleep(INTERVAL)
