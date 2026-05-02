"""Backfill page_display.avif from existing page_raw.png files.

Walks columns/<YYYY-MM-DD>/p<N>/ and, for each directory containing a
page_raw.png, writes a sibling page_display.avif (150-DPI LANCZOS-grey,
quality=70).

Move-aside policy: any pre-existing page_display.avif is renamed to
page_display.avif.bak-<run-stamp> before being overwritten. Cleanup is
a separate user-authorised step.

Idempotent: re-running skips dirs that already have a fresh
page_display.avif (mtime newer than page_raw.png) unless --force is
passed.

Usage:
    python3 scripts/backfill_page_display.py columns/1902-01-10
    python3 scripts/backfill_page_display.py columns/1902-*
    python3 scripts/backfill_page_display.py columns/  # everything
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from glob import glob

from PIL import Image


_FALLBACK_NATIVE_PPI = 510.0


def _target_dims(page_dir, native_w, native_h):
    """Compute the 150-DPI display target dim for this page.

    Preferred path: read pdf_path from page_meta.json, open the PDF,
    use page.rect.width × 150/72 — same arithmetic as the in-pipeline
    writer, so backfilled artefacts match freshly-processed ones.

    Fallback (no meta, no PDF on disk): scale native dims by
    150/_FALLBACK_NATIVE_PPI. Imprecise but still yields a
    browser-safe display variant — just slightly oversized vs the
    pipeline default. A warning is printed when this fires.
    """
    meta_path = os.path.join(page_dir, "page_meta.json")
    if os.path.isfile(meta_path):
        try:
            import json
            with open(meta_path) as f:
                meta = json.load(f)
            pdf_path = meta.get("pdf_path")
            if pdf_path and os.path.isfile(pdf_path):
                import fitz
                doc = fitz.open(pdf_path)
                try:
                    rect = doc[meta.get("page_number", 0)].rect
                    return (int(round(rect.width * 150 / 72.0)),
                            int(round(rect.height * 150 / 72.0)))
                finally:
                    doc.close()
        except Exception as e:
            print(f"  [warn] {page_dir}: meta-derived dim failed "
                  f"({type(e).__name__}: {e}); falling back to PPI "
                  f"heuristic", file=sys.stderr)
    scale = 150.0 / _FALLBACK_NATIVE_PPI
    return int(round(native_w * scale)), int(round(native_h * scale))


def backfill_page(page_dir, run_stamp, force=False):
    """Generate page_display.avif from page_raw.png in `page_dir`.

    Returns: 'wrote' | 'skipped-fresh' | 'skipped-no-raw' | 'error: ...'
    """
    raw_path = os.path.join(page_dir, "page_raw.png")
    display_path = os.path.join(page_dir, "page_display.avif")

    if not os.path.isfile(raw_path):
        return "skipped-no-raw"

    if not force and os.path.isfile(display_path):
        if os.path.getmtime(display_path) >= os.path.getmtime(raw_path):
            return "skipped-fresh"

    # Only backfill un-gate-era pages: mode='1' page_raw.png at native PPI
    # is what trips the browser. Pre-un-gate RGB page_raw.png is already
    # at ~150 DPI and renders fine; the viewer falls back to page_raw.png
    # via retryImg when page_display.avif is missing.
    try:
        with Image.open(raw_path) as probe:
            if probe.mode != "1":
                return "skipped-rgb"
    except Exception as e:
        return f"error: probe failed: {type(e).__name__}: {e}"

    # Move-aside policy: any existing display variant is renamed before
    # being overwritten. Future cleanup is explicit.
    if os.path.isfile(display_path):
        bak = f"{display_path}.bak-{run_stamp}"
        if not os.path.isfile(bak):
            os.rename(display_path, bak)

    try:
        native = Image.open(raw_path)
        target_w, target_h = _target_dims(page_dir, *native.size)
        # Force load before resize (PIL's lazy loading can hold the file
        # handle longer than necessary).
        native.load()
        display = native.convert("L").resize(
            (target_w, target_h), Image.LANCZOS)
        # Atomic write: tmp file + rename so a crash mid-encode doesn't
        # leave a truncated file mistaken for the real thing.
        tmp = display_path + ".tmp"
        # Explicit format — Pillow infers from extension, and .tmp is
        # not recognised. AVIF q=70 is the chosen display variant.
        display.save(tmp, format="AVIF", quality=70)
        os.replace(tmp, display_path)
        return "wrote"
    except Exception as e:
        return f"error: {type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+",
                    help="One or more issue dirs (columns/<date>) "
                         "or glob patterns")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate even if a fresh page_display.avif "
                         "already exists (older than page_raw.png is "
                         "always re-generated regardless).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be processed; do not write.")
    args = ap.parse_args()

    run_stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"

    # Expand inputs to a flat list of issue dirs. An "issue dir" is a
    # path like columns/1902-01-10 that contains p1/, p2/, ... children.
    issue_dirs = []
    for raw_path in args.paths:
        # raw_path may be a glob, a single dir, or columns/ itself.
        candidates = glob(raw_path) if any(c in raw_path for c in "*?[") else [raw_path]
        for c in candidates:
            if not os.path.isdir(c):
                continue
            # If the dir directly has p<N>/ subdirs, it's an issue dir.
            page_subs = [d for d in os.listdir(c)
                         if d.startswith("p") and d[1:].isdigit()]
            if page_subs:
                issue_dirs.append(c)
            else:
                # Otherwise treat as a parent — find issue dirs inside.
                for child in sorted(os.listdir(c)):
                    cp = os.path.join(c, child)
                    if not os.path.isdir(cp):
                        continue
                    if any(d.startswith("p") and d[1:].isdigit()
                           for d in os.listdir(cp)):
                        issue_dirs.append(cp)

    issue_dirs = sorted(set(issue_dirs))
    print(f"backfill: {len(issue_dirs)} issue dir(s)")
    if args.dry_run:
        for d in issue_dirs:
            print(f"  would process: {d}")
        return

    counts = {"wrote": 0, "skipped-fresh": 0, "skipped-no-raw": 0,
              "skipped-rgb": 0, "error": 0}
    t0 = time.time()
    for d in issue_dirs:
        page_dirs = sorted(
            os.path.join(d, p) for p in os.listdir(d)
            if p.startswith("p") and p[1:].isdigit())
        issue_t0 = time.time()
        issue_counts = {"wrote": 0, "skipped-fresh": 0,
                        "skipped-no-raw": 0, "skipped-rgb": 0,
                        "error": 0}
        for pd in page_dirs:
            result = backfill_page(pd, run_stamp, force=args.force)
            if result.startswith("error"):
                counts["error"] += 1
                issue_counts["error"] += 1
                print(f"  {pd}: {result}", file=sys.stderr)
            else:
                counts[result] += 1
                issue_counts[result] += 1
        elapsed = time.time() - issue_t0
        # Only print issues where we actually did work — keeps logs short
        # when running across the full corpus.
        if issue_counts['wrote'] or issue_counts['error']:
            print(f"  {d}: {issue_counts['wrote']} wrote, "
                  f"{issue_counts['skipped-fresh']} fresh, "
                  f"{issue_counts['skipped-rgb']} rgb-skip, "
                  f"{issue_counts['skipped-no-raw']} no-raw, "
                  f"{issue_counts['error']} err  ({elapsed:.1f}s)")

    total = time.time() - t0
    print(f"\nDONE: {counts['wrote']} wrote, {counts['skipped-fresh']} fresh, "
          f"{counts['skipped-rgb']} rgb-skip, "
          f"{counts['skipped-no-raw']} no-raw, {counts['error']} error  "
          f"({total:.1f}s)")


if __name__ == "__main__":
    main()
