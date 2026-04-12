"""
Issue-level orchestrator for the Almonte Gazette pipeline.

Downloads all pages of an issue, processes them in two passes:
  Pass 1: Independent detection on every page (no prior)
  Pass 2: Establish pitch from the most regular pages, re-process
           weak pages using anchored transposition

Usage:
    python process_issue.py 1937 1 14 [--output-dir DIR] [--db PATH]

    from process_issue import process_issue
    result = process_issue(1937, 1, 14, output_dir="columns/1937-01-14")
"""

import os
import sys
import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import numpy as np

from split_page import split_page, PageResult
from page_profile import profile_page
from detect_ads import detect_ads, extract_ad_images, store_ads, get_ad_exclusion_zones


def download_issue(year, month, day, db_path="data/mvtm.db",
                   download_dir=None):
    """
    Download all PDF pages for an issue from Google Drive.

    Returns list of (page_number, pdf_path) tuples.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT page, drive_id, directory_path FROM files
        WHERE year=? AND month=? AND day=? AND file_type='pdf'
        ORDER BY page
    """, (year, month, day)).fetchall()
    conn.close()

    if not rows:
        print(f"No pages found for {year}-{month:02d}-{day:02d}")
        return []

    if download_dir is None:
        download_dir = f"/tmp/issue_{year}-{month:02d}-{day:02d}"
    os.makedirs(download_dir, exist_ok=True)

    pages = []
    for page_num, drive_id, dpath in rows:
        fname = dpath.split("/")[-1]
        pdf_path = os.path.join(download_dir, fname)

        # Download
        subprocess.run(
            ["curl", "-sL", "-o", pdf_path,
             f"https://drive.google.com/uc?export=download&id={drive_id}"],
            check=False,
        )

        # Verify
        if not os.path.exists(pdf_path):
            print(f"  Page {page_num}: download failed")
            continue
        with open(pdf_path, "rb") as f:
            if f.read(5) != b"%PDF-":
                print(f"  Page {page_num}: not a valid PDF")
                continue

        pages.append((page_num, pdf_path))

    return pages


def _score_regularity(result):
    """
    Score how regular a page's column widths are.

    Returns (cv, median_width, num_columns) where lower CV = more regular.
    Returns (999, 0, 0) for failed pages.
    """
    if result.error or not result.columns or len(result.columns) < 3:
        return 999.0, 0.0, 0

    widths = [c.width_vw for c in result.columns]
    median_w = float(np.median(widths))
    if median_w == 0:
        return 999.0, 0.0, 0

    cv = float(np.std(widths) / median_w)
    return cv, median_w, len(result.columns)


def _establish_pitch(pass1_results):
    """
    Determine the column pitch from the most regular pages.

    Scores all pages by CV, picks the two most regular that agree
    on column count, and averages their median widths.

    Returns (pitch, num_columns, grounding_pages) or (None, None, []).
    """
    # Score all pages
    scored = []
    for page_num, result, profile in pass1_results:
        cv, median_w, num_cols = _score_regularity(result)
        if num_cols >= 3:
            scored.append({
                "page": page_num,
                "cv": cv,
                "median_width": median_w,
                "num_columns": num_cols,
                "result": result,
                "profile": profile,
            })

    if not scored:
        return None, None, []

    # Sort by regularity (lowest CV first)
    scored.sort(key=lambda s: s["cv"])

    # Find two pages that agree on column count
    best = scored[0]
    partner = None
    for s in scored[1:]:
        if s["num_columns"] == best["num_columns"]:
            partner = s
            break

    if partner:
        pitch = (best["median_width"] + partner["median_width"]) / 2
        grounding = [best["page"], partner["page"]]
    else:
        pitch = best["median_width"]
        grounding = [best["page"]]

    return round(pitch, 2), best["num_columns"], grounding


def process_issue(year, month, day, output_dir=None, db_path="data/mvtm.db",
                  download_dir=None, dpi=450):
    """
    Process all pages of an issue with two-pass pitch establishment.

    Pass 1: Independent detection on every page
    Pass 2: Establish pitch from best pages, re-process weak ones

    Args:
        year, month, day: Issue date
        output_dir: Where to save column PNGs (default: columns/YYYY-MM-DD/)
        db_path: SQLite database path
        download_dir: Where to cache downloaded PDFs
        dpi: Render resolution

    Returns:
        dict with pitch, num_columns, page_results, grounding_pages
    """
    t0 = time.time()

    if output_dir is None:
        output_dir = f"columns/{year}-{month:02d}-{day:02d}"

    # ── Download ─────────────────────────────────────────────────────
    print(f"Downloading {year}-{month:02d}-{day:02d}...")
    pages = download_issue(year, month, day, db_path, download_dir)
    if not pages:
        return {"error": "no_pages_found"}

    print(f"  {len(pages)} pages downloaded")

    # ── Ad detection (before column detection) ─────────────────────
    print("Detecting display ads...")
    page_ads = {}  # page_num → list of ad dicts
    total_ads = 0

    ads_dir = os.path.join(output_dir, "ads")
    os.makedirs(ads_dir, exist_ok=True)

    for page_num, pdf_path in pages:
        ads = detect_ads(pdf_path, column_pitch=None)
        if ads:
            ad_out = os.path.join(ads_dir, f"p{page_num}")
            ads_with_images = extract_ad_images(pdf_path, ads, ad_out, dpi=dpi)
            store_ads(db_path, year, month, day, page_num, ads_with_images)
            page_ads[page_num] = ads
            total_ads += len(ads)
            ad_desc = ", ".join(str(a["cols"]) + "col" for a in ads)
            print(f"  P{page_num}: {len(ads)} ads ({ad_desc})")

    if total_ads:
        print(f"  Total: {total_ads} ads catalogued")
    else:
        print("  No display ads found")

    # ── Pass 1: Independent column detection ─────────────────────────
    print("Pass 1: Independent detection...")
    pass1_results = []

    for page_num, pdf_path in pages:
        page_out = os.path.join(output_dir, f"p{page_num}")
        os.makedirs(page_out, exist_ok=True)

        prof = profile_page(pdf_path)
        result = split_page(pdf_path, output_dir=page_out, dpi=dpi)

        cv, median_w, num_cols = _score_regularity(result)
        widths = " ".join(f"{c.width_vw:.0f}%" for c in result.columns)
        n_ads = len(page_ads.get(page_num, []))
        ad_note = f" [{n_ads} ads]" if n_ads else ""
        print(f"  P{page_num} ({prof['page_type']}): {num_cols}c [{widths}] "
              f"CV={cv:.3f}{ad_note}")

        pass1_results.append((page_num, result, prof))

    # ── Establish pitch ──────────────────────────────────────────────
    pitch, num_columns, grounding_pages = _establish_pitch(pass1_results)
    if pitch is None:
        print("  Could not establish pitch — all pages failed")
        return {"error": "no_pitch"}

    print(f"\nPitch: {pitch:.1f}% from {num_columns} columns "
          f"(grounding pages: {grounding_pages})")

    # ── Pass 2: Re-process weak pages with prior ─────────────────────
    # Get boundaries from the best grounding page
    grounding_page_num = grounding_pages[0]
    grounding_result = None
    grounding_prof = None
    for pn, r, p in pass1_results:
        if pn == grounding_page_num:
            grounding_result = r
            grounding_prof = p
            break

    prior_bounds = ([c.left_vw for c in grounding_result.columns]
                    + [grounding_result.columns[-1].right_vw])
    prior_page_type = grounding_prof["page_type"]

    # Determine which pages need re-processing
    REGULARITY_THRESHOLD = 0.10  # CV below this = good enough
    pages_to_reprocess = []
    for page_num, result, prof in pass1_results:
        if page_num in grounding_pages:
            continue  # grounding pages are already good
        cv, _, nc = _score_regularity(result)
        if cv > REGULARITY_THRESHOLD or nc != num_columns:
            pages_to_reprocess.append((page_num, result, prof))

    if pages_to_reprocess:
        print(f"\nPass 2: Re-processing {len(pages_to_reprocess)} weak pages "
              f"with pitch={pitch:.1f}%...")

        for page_num, old_result, prof in pages_to_reprocess:
            pdf_path = None
            for pn, pp in pages:
                if pn == page_num:
                    pdf_path = pp
                    break

            page_out = os.path.join(output_dir, f"p{page_num}")
            shutil.rmtree(page_out, ignore_errors=True)
            os.makedirs(page_out)

            new_result = split_page(
                pdf_path, output_dir=page_out, dpi=dpi,
                expected_columns=num_columns,
                prior_boundaries=prior_bounds,
                prior_page_type=prior_page_type,
            )

            cv_old, _, _ = _score_regularity(old_result)
            cv_new, _, _ = _score_regularity(new_result)
            widths = " ".join(f"{c.width_vw:.0f}%" for c in new_result.columns)
            improved = "IMPROVED" if cv_new < cv_old else "same"
            anchored = "ANCHORED" if any("anchor" in f for f in new_result.quality_flags) else ""
            print(f"  P{page_num}: {new_result.num_columns}c [{widths}] "
                  f"CV {cv_old:.3f}→{cv_new:.3f} {improved} {anchored}")

            # Update the result in pass1_results
            for i, (pn, r, p) in enumerate(pass1_results):
                if pn == page_num:
                    pass1_results[i] = (pn, new_result, p)
                    break
    else:
        print("\nPass 2: All pages regular — no re-processing needed")

    # ── Summary ──────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")
    print(f"Issue: {year}-{month:02d}-{day:02d}")
    print(f"Pitch: {pitch:.1f}%, {num_columns} columns")
    print(f"Grounding: pages {grounding_pages}")
    for page_num, result, prof in pass1_results:
        cv, _, _ = _score_regularity(result)
        widths = " ".join(f"{c.width_vw:.0f}%" for c in result.columns)
        flags = [f for f in result.quality_flags
                 if "anchor" in f or "prior" in f]
        note = f"  {flags[0]}" if flags else ""
        print(f"  P{page_num}: {result.num_columns}c [{widths}] CV={cv:.3f}{note}")

    return {
        "year": year, "month": month, "day": day,
        "pitch": pitch,
        "num_columns": num_columns,
        "grounding_pages": grounding_pages,
        "elapsed": round(elapsed, 1),
        "pages": [
            {
                "page": pn,
                "num_columns": r.num_columns,
                "cv": round(_score_regularity(r)[0], 3),
                "widths": [round(c.width_vw, 1) for c in r.columns],
                "quality_flags": r.quality_flags,
            }
            for pn, r, _ in pass1_results
        ],
    }


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python process_issue.py <year> <month> <day> "
              "[--output-dir DIR] [--db PATH]")
        sys.exit(1)

    year = int(sys.argv[1])
    month = int(sys.argv[2])
    day = int(sys.argv[3])

    output_dir = None
    db_path = "data/mvtm.db"
    for i, arg in enumerate(sys.argv[4:], 4):
        if arg == "--output-dir" and i + 1 < len(sys.argv):
            output_dir = sys.argv[i + 1]
        elif arg == "--db" and i + 1 < len(sys.argv):
            db_path = sys.argv[i + 1]

    result = process_issue(year, month, day, output_dir=output_dir,
                           db_path=db_path)

    # Save issue summary
    if output_dir and "error" not in result:
        summary_path = os.path.join(output_dir or f"columns/{year}-{month:02d}-{day:02d}",
                                    "issue_summary.json")
        with open(summary_path, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\nSummary saved: {summary_path}")
