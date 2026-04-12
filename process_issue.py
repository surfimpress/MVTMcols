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


def _clean_side_pitch(result, profile):
    """
    Compute pitch from clean-side boundaries only.

    On the clean side (non-binding), boundaries are never contaminated
    by the facing page sliver. Use only these to establish the true pitch.

    Returns (pitch, num_clean_boundaries) or (None, 0).
    """
    if not result.columns or len(result.columns) < 3:
        return None, 0

    clean_side = profile.get("clean_side")
    if not clean_side:
        return None, 0

    # Get all boundary positions
    positions = ([c.left_vw for c in result.columns]
                 + [result.columns[-1].right_vw])

    # Keep only boundaries in the clean half of the page
    if clean_side == "left":
        clean = [p for p in positions if p < 55]
    else:
        clean = [p for p in positions if p > 45]

    if len(clean) < 3:
        return None, 0

    clean.sort()
    widths = [clean[i+1] - clean[i] for i in range(len(clean) - 1)]

    # Filter out any remaining narrow gaps (ad borders)
    median_w = float(np.median(widths))
    regular = [w for w in widths if w > median_w * 0.65]
    if not regular:
        return None, 0

    return round(float(np.median(regular)), 2), len(clean)


def _establish_pitch(pass1_results):
    """
    Determine column pitch separately for recto and verso pages,
    using only clean-side boundaries.

    The clean side (non-binding) never has facing page sliver
    contamination, so its boundaries give the true pitch. Each
    page type (recto/verso) may have a different effective scale
    due to scanning, so they get separate pitches.

    Returns dict with recto and verso pitches, or None.
    """
    recto_pitches = []
    verso_pitches = []
    recto_pages = []
    verso_pages = []

    for page_num, result, profile in pass1_results:
        page_type = profile.get("page_type")
        pitch, n_bounds = _clean_side_pitch(result, profile)
        if pitch is None:
            continue

        if page_type == "recto":
            recto_pitches.append((pitch, n_bounds, page_num))
            recto_pages.append((page_num, result, profile))
        elif page_type == "verso":
            verso_pitches.append((pitch, n_bounds, page_num))
            verso_pages.append((page_num, result, profile))

    def _median_pitch(pitches):
        if not pitches:
            return None, []
        # Weight by number of boundaries (more = more reliable)
        pitches.sort(key=lambda p: -p[1])
        median_p = float(np.median([p[0] for p in pitches]))
        grounding = [p[2] for p in pitches[:2]]
        return round(median_p, 2), grounding

    recto_pitch, recto_grounding = _median_pitch(recto_pitches)
    verso_pitch, verso_grounding = _median_pitch(verso_pitches)

    # Estimate column count from pitch: total content width / pitch
    # Use the text_area span from the best page of each type
    def _estimate_cols(pages, pitch):
        if not pages or not pitch:
            return None
        # Use the page with most detected boundaries
        best = max(pages, key=lambda p: len(p[1].columns))
        ta = best[2].get("text_area", {})
        span = ta.get("right", 90) - ta.get("left", 10)
        return max(3, round(span / pitch))

    recto_cols = _estimate_cols(recto_pages, recto_pitch)
    verso_cols = _estimate_cols(verso_pages, verso_pitch)

    # Use recto as primary if available
    if recto_pitch:
        pitch = recto_pitch
        num_columns = recto_cols
        grounding = recto_grounding
    elif verso_pitch:
        pitch = verso_pitch
        num_columns = verso_cols
        grounding = verso_grounding
    else:
        return None

    return {
        "pitch": pitch, "num_columns": num_columns, "grounding_pages": grounding,
        "recto_pitch": recto_pitch, "recto_cols": recto_cols,
        "recto_grounding": recto_grounding or [],
        "verso_pitch": verso_pitch, "verso_cols": verso_cols,
        "verso_grounding": verso_grounding or [],
    }


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
        prof = profile_page(pdf_path)
        ads = detect_ads(pdf_path, column_pitch=None, page_profile=prof)
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
        # Get ad exclusion zones for this page
        zones = get_ad_exclusion_zones(page_ads.get(page_num, []))
        result = split_page(pdf_path, output_dir=page_out, dpi=dpi,
                           ad_exclusion_zones=zones)

        cv, median_w, num_cols = _score_regularity(result)
        widths = " ".join(f"{c.width_vw:.0f}%" for c in result.columns)
        n_ads = len(page_ads.get(page_num, []))
        ad_note = f" [{n_ads} ads]" if n_ads else ""
        print(f"  P{page_num} ({prof['page_type']}): {num_cols}c [{widths}] "
              f"CV={cv:.3f}{ad_note}")

        pass1_results.append((page_num, result, prof))

    # ── Establish pitch ──────────────────────────────────────────────
    pitch_info = _establish_pitch(pass1_results)
    if pitch_info is None:
        print("  Could not establish pitch — all pages failed")
        return {"error": "no_pitch"}

    pitch = pitch_info["pitch"]
    num_columns = pitch_info["num_columns"]
    grounding_pages = pitch_info["grounding_pages"]

    print(f"\nPitch (primary): {pitch:.1f}% from {num_columns} columns "
          f"(grounding pages: {grounding_pages})")
    rp = pitch_info.get("recto_pitch")
    vp = pitch_info.get("verso_pitch")
    rc = pitch_info.get("recto_cols")
    vc = pitch_info.get("verso_cols")
    if rp and vp:
        print(f"  Recto: {rc}cols @ {rp:.1f}%  Verso: {vc}cols @ {vp:.1f}%")
    elif rp:
        print(f"  Recto: {rc}cols @ {rp:.1f}%  Verso: no data")
    elif vp:
        print(f"  Recto: no data  Verso: {vc}cols @ {vp:.1f}%")

    # ── Establish recto and verso templates separately ──────────────
    # Recto and verso pages have different binding offsets and photo
    # placement. Never mirror positions between types. Find the best
    # page of each type and use its positions as the template.
    def _get_bounds(result):
        return ([c.left_vw for c in result.columns]
                + [result.columns[-1].right_vw])

    recto_template = None
    verso_template = None
    recto_best_cv = 999
    verso_best_cv = 999

    # Column count is the same across the issue — use recto count
    # (recto is more reliable as it has no sliver contamination)
    recto_num_cols = num_columns
    verso_num_cols = num_columns

    for page_num, result, prof in pass1_results:
        cv, _, nc = _score_regularity(result)
        page_type = prof.get("page_type")
        if page_type == "recto" and cv < recto_best_cv:
            # Accept if column count is close to expected
            if nc == recto_num_cols:
                recto_best_cv = cv
                recto_template = {
                    "bounds": _get_bounds(result),
                    "page": page_num,
                    "cv": cv,
                    "page_type": "recto",
                    "num_cols": recto_num_cols,
                    "pitch": pitch_info.get("recto_pitch") or pitch,
                }
        elif page_type == "verso" and cv < verso_best_cv:
            if nc == verso_num_cols:
                verso_best_cv = cv
                verso_template = {
                    "bounds": _get_bounds(result),
                    "page": page_num,
                    "cv": cv,
                    "page_type": "verso",
                    "num_cols": verso_num_cols,
                    "pitch": pitch_info.get("verso_pitch") or pitch,
                }

    if recto_template:
        print(f"  Recto template: P{recto_template['page']} CV={recto_template['cv']:.3f}")
    else:
        print(f"  Recto template: none found")
    if verso_template:
        print(f"  Verso template: P{verso_template['page']} CV={verso_template['cv']:.3f}")
    else:
        print(f"  Verso template: none found")

    # ── Pass 2: Re-process weak pages with matching template ─────────
    REGULARITY_THRESHOLD = 0.10
    pages_to_reprocess = []
    for page_num, result, prof in pass1_results:
        cv, _, nc = _score_regularity(result)
        page_type = prof.get("page_type")
        is_template = ((recto_template and page_num == recto_template["page"]) or
                       (verso_template and page_num == verso_template["page"]))
        if is_template:
            continue
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

            # Select the template matching this page's type
            page_type = prof.get("page_type")
            if page_type == "recto" and recto_template:
                template = recto_template
            elif page_type == "verso" and verso_template:
                template = verso_template
            elif recto_template:
                template = recto_template  # fallback
            elif verso_template:
                template = verso_template
            else:
                continue  # no template available

            page_out = os.path.join(output_dir, f"p{page_num}")
            # Remove old column files but preserve overlays
            if os.path.exists(page_out):
                for f in os.listdir(page_out):
                    if "_col" in f and f.endswith(".png"):
                        os.remove(os.path.join(page_out, f))
                    elif f == "page_meta.json":
                        os.remove(os.path.join(page_out, f))
            os.makedirs(page_out, exist_ok=True)

            zones = get_ad_exclusion_zones(page_ads.get(page_num, []))
            new_result = split_page(
                pdf_path, output_dir=page_out, dpi=dpi,
                expected_columns=template.get("num_cols", num_columns),
                prior_boundaries=template["bounds"],
                prior_page_type=template["page_type"],
                ad_exclusion_zones=zones,
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

    # ── Store in database ──────────────────────────────────────────
    from layout_intelligence import LayoutDB

    db = LayoutDB(db_path)

    # Clean any previous data for this issue
    conn = __import__("sqlite3").connect(db_path)
    conn.execute("DELETE FROM page_layouts WHERE year=? AND month=? AND day=?",
                 (year, month, day))
    conn.execute("DELETE FROM page_geometry WHERE year=? AND month=? AND day=?",
                 (year, month, day))
    conn.commit()
    conn.close()

    for page_num, result, prof in pass1_results:
        meta_path = os.path.join(output_dir, f"p{page_num}", "page_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            boundaries = [col["right_vw"] for col in meta["columns"]
                         if col["right_vw"] < 100]
            cv, _, _ = _score_regularity(result)
            db.record_layout(year, month, day, page_num,
                            result.num_columns, boundaries,
                            result.quality_flags,
                            round(1.0 / (1.0 + cv), 3))
        db.record_geometry(year, month, day, page_num, prof)

    db.compute_era_patterns()

    # ── Generate overlays ────────────────────────────────────────────
    print("\nGenerating overlays...")
    from PIL import Image, ImageDraw
    import fitz as _fitz

    for page_num, result, prof in pass1_results:
        pdf_path = None
        for pn, pp in pages:
            if pn == page_num:
                pdf_path = pp
                break
        if not pdf_path:
            continue

        page_out = os.path.join(output_dir, f"p{page_num}")
        doc = _fitz.open(pdf_path)
        pg = doc[0]
        pix = pg.get_pixmap(dpi=150)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.h, pix.w, pix.n)
        doc.close()

        pil = Image.fromarray(img[:, :, :3]).convert("RGBA")
        ol = Image.new("RGBA", pil.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(ol)
        ih, iw = pil.size[1], pil.size[0]

        def _vl(x_pct, color, width=2):
            x = int(x_pct / 100 * iw)
            draw.line([(x, 0), (x, ih)], fill=color, width=width)

        ta = prof.get("text_area", {})
        if ta:
            _vl(ta.get("left", 0), (255, 140, 0, 255), 3)
            _vl(ta.get("right", 100), (255, 140, 0, 255), 3)

        meta_path = os.path.join(page_out, "page_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            for col in meta["columns"]:
                _vl(col["left_vw"], (0, 100, 255, 200), 2)
                _vl(col["right_vw"], (0, 100, 255, 200), 2)

        overlay_path = os.path.join(page_out, "overlay.png")
        Image.alpha_composite(pil, ol).convert("RGB").save(overlay_path)

    print("  Done")

    # ── Update viewer data ──────────────────────────────────────────
    _update_viewer_data(db_path, "columns")

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


def _update_viewer_data(db_path, columns_dir):
    """
    Dump processed issue data from SQLite as JSON for the viewer.
    Called automatically after each issue is processed.
    """
    import sqlite3 as _sql
    conn = _sql.connect(db_path)

    # Get all issues with layouts
    issues_raw = conn.execute("""
        SELECT DISTINCT year, month, day FROM page_layouts ORDER BY year, month, day
    """).fetchall()

    issues = []
    for year, month, day in issues_raw:
        layouts = conn.execute("""
            SELECT page, num_columns, column_widths, quality_flags, confidence
            FROM page_layouts WHERE year=? AND month=? AND day=?
            ORDER BY page
        """, (year, month, day)).fetchall()

        ads = conn.execute("""
            SELECT page, cols, confidence, image_filename
            FROM detected_ads WHERE year=? AND month=? AND day=?
            ORDER BY page
        """, (year, month, day)).fetchall()

        pages = []
        for page, num_cols, widths_json, flags_json, conf in layouts:
            widths = json.loads(widths_json) if widths_json else []
            flags = json.loads(flags_json) if flags_json else []
            page_type = "recto" if page % 2 == 1 else "verso"

            # Check what files exist
            page_dir = os.path.join(columns_dir, f"{year}-{month:02d}-{day:02d}", f"p{page}")
            col_files = sorted(f for f in os.listdir(page_dir)
                              if "_col" in f and f.endswith(".png")) if os.path.exists(page_dir) else []
            has_overlay = os.path.exists(os.path.join(page_dir, "overlay.png")) if os.path.exists(page_dir) else False

            pages.append({
                "page": page,
                "page_type": page_type,
                "num_columns": num_cols,
                "widths": widths,
                "flags": flags,
                "confidence": conf,
                "col_files": col_files,
                "has_overlay": has_overlay,
            })

        ad_list = [{"page": p, "cols": c, "confidence": cf, "file": fn}
                   for p, c, cf, fn in ads]

        issue_dir = f"{year}-{month:02d}-{day:02d}"
        summary_path = os.path.join(columns_dir, issue_dir, "issue_summary.json")
        pitch = None
        if os.path.exists(summary_path):
            with open(summary_path) as f:
                s = json.load(f)
                pitch = s.get("pitch")

        issues.append({
            "year": year, "month": month, "day": day,
            "dir": issue_dir,
            "pitch": pitch,
            "n_pages": len(pages),
            "n_cols": sum(len(p["col_files"]) for p in pages),
            "n_ads": len(ad_list),
            "pages": pages,
            "ads": ad_list,
        })

    conn.close()

    # Write JSON for the viewer
    viewer_data_path = os.path.join(columns_dir, "viewer_data.json")
    with open(viewer_data_path, "w") as f:
        json.dump(issues, f, indent=2)


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
