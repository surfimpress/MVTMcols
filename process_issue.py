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

from split_page import split_page, PageResult, extract_columns, _save_metadata
from page_profile import profile_page
from detect_ads import detect_ads, extract_ad_images, store_ads, get_ad_exclusion_zones
from page_context import build_context
from column_pipeline import detect_strips, cluster_boundaries, place_columns


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

    # ── Pass 1: Detect boundaries (new pipeline) ──────────────────
    # Pass 1 only detects and clusters boundaries. It does NOT place
    # columns — that happens in pass 2 after pitch is established.
    # This prevents the default pitch from poisoning the results.
    print("Pass 1: Detecting boundaries...")
    pass1_detections = {}   # page_num → clustered boundaries
    page_profiles = {}
    page_contexts = {}

    for page_num, pdf_path in pages:
        prof = profile_page(pdf_path)
        page_profiles[page_num] = prof
        ads = page_ads.get(page_num, [])

        ctx = build_context(page_num, year, db_path=db_path,
                           profile=prof, ads=ads)
        page_contexts[page_num] = ctx

        raw = detect_strips(pdf_path, ctx, dpi=dpi)
        clustered = cluster_boundaries(raw)
        pass1_detections[page_num] = clustered

        n_det = len(clustered)
        n_ads = len(ads)
        ad_note = f" [{n_ads} ads]" if n_ads else ""
        p2_note = " [P2 editorial]" if ctx.is_page_2 and ctx.page_2_template else ""
        print(f"  P{page_num} ({ctx.page_type}): {n_det} boundaries detected{ad_note}{p2_note}")

    # ── Establish pitch from detected boundaries ────────────────────
    # Compute pitch directly from the gaps between detected boundaries.
    # Use all pages, prefer recto (no sliver contamination).
    all_pitches = []
    for page_num, clustered in pass1_detections.items():
        if len(clustered) < 3:
            continue
        positions = sorted(b["x_pct"] for b in clustered)
        gaps = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        # Filter to plausible column widths (8-16%)
        plausible = [g for g in gaps if 8 < g < 16]
        if plausible:
            page_pitch = float(np.median(plausible))
            page_type = page_contexts[page_num].page_type
            all_pitches.append((page_pitch, page_num, page_type, len(plausible)))

    if not all_pitches:
        print("  Could not establish pitch — no plausible boundaries detected")
        return {"error": "no_pitch"}

    # Prefer recto pages (no sliver contamination)
    recto_pitches = [(p, pn, n) for p, pn, pt, n in all_pitches if pt == "recto"]
    if recto_pitches:
        # Weight by number of plausible gaps (more = more reliable)
        recto_pitches.sort(key=lambda x: -x[2])
        pitch = round(float(np.median([p for p, _, _ in recto_pitches])), 1)
        grounding_pages = [pn for _, pn, _ in recto_pitches[:2]]
    else:
        all_pitches.sort(key=lambda x: -x[3])
        pitch = round(float(np.median([p for p, _, _, _ in all_pitches])), 1)
        grounding_pages = [pn for _, pn, _, _ in all_pitches[:2]]

    # Column count: N detected boundaries = N+1 columns.
    # Use recto pages only (no sliver contamination) and take
    # the median boundary count. This avoids verso pages inflating
    # the count with sliver boundaries.
    recto_counts = []
    for page_num, clustered in pass1_detections.items():
        if len(clustered) >= 3 and page_contexts[page_num].page_type == "recto":
            recto_counts.append(len(clustered))

    if recto_counts:
        median_boundaries = round(float(np.median(recto_counts)))
        num_columns = median_boundaries + 1
        num_columns = max(3, min(8, num_columns))
    elif any(len(c) >= 3 for c in pass1_detections.values()):
        # Fallback to all pages if no recto data
        all_counts = [len(c) for c in pass1_detections.values() if len(c) >= 3]
        num_columns = round(float(np.median(all_counts))) + 1
        num_columns = max(3, min(8, num_columns))
    else:
        num_columns = 7

    print(f"\nPitch: {pitch:.1f}% from {num_columns} columns "
          f"(grounding pages: {grounding_pages})")

    # ── Page 2 editorial template ───────────────────────────────────
    from layout_intelligence import LayoutDB
    _db = LayoutDB(db_path)
    p2_template = _db.get_template("page2_editorial_wide", 2, year)
    if p2_template:
        _db.update_template_range("page2_editorial_wide", 2, year)
        print(f"  Page 2 editorial template: {p2_template['year_start']}-{p2_template['year_end']}")

    # ── Pass 2: Place columns with established pitch ─────────────────
    # Now we know the pitch, rebuild context for every page and run
    # placement. Detected boundaries from pass 1 are reused.
    print(f"\nPass 2: Placing columns with pitch={pitch:.1f}%...")
    pass1_results = []

    for page_num, pdf_path in pages:
        prof = page_profiles[page_num]
        ads = page_ads.get(page_num, [])
        clustered = pass1_detections[page_num]

        # Build context WITH the established pitch
        ctx = build_context(page_num, year, db_path=db_path,
                           profile=prof, ads=ads,
                           issue_pitch=pitch, issue_columns=num_columns)

        # Place columns using detected boundaries + context
        final = place_columns(clustered, ctx)

        # Extract columns
        page_out = os.path.join(output_dir, f"p{page_num}")
        if os.path.exists(page_out):
            for f in os.listdir(page_out):
                if ("_col" in f and f.endswith(".png")) or f == "page_meta.json":
                    os.remove(os.path.join(page_out, f))
        os.makedirs(page_out, exist_ok=True)

        columns = extract_columns(pdf_path, final, 0, dpi, page_out)
        result = PageResult(
            pdf_path=pdf_path, page_number=0, dpi=dpi,
            page_width_px=0, page_height_px=0,
            num_columns=len(columns), columns=columns,
            detection_row=[], quality_flags=[],
            error=None, elapsed_seconds=0,
        )
        _save_metadata(result, os.path.join(page_out, "page_meta.json"))

        cv, _, nc = _score_regularity(result)
        widths = " ".join(f"{c.width_vw:.0f}%" for c in result.columns)
        p2_note = " [P2 editorial]" if ctx.is_page_2 and ctx.page_2_template else ""
        print(f"  P{page_num}: {nc}c [{widths}] CV={cv:.3f}{p2_note}")

        pass1_results.append((page_num, result, prof))

    # ── Pass 3: Cross-page consistency check ─────────────────────────
    # Check that leftmost column positions are consistent within
    # recto and verso groups. Outliers get reassessed.
    recto_lefts = []
    verso_lefts = []
    for page_num, result, prof in pass1_results:
        if not result.columns:
            continue
        left = result.columns[0].left_vw
        page_type = prof.get("page_type")
        if page_type == "recto":
            recto_lefts.append((page_num, left, result, prof))
        elif page_type == "verso":
            verso_lefts.append((page_num, left, result, prof))

    outliers_fixed = 0
    for group_name, group in [("recto", recto_lefts), ("verso", verso_lefts)]:
        if len(group) < 3:
            continue
        lefts = [g[1] for g in group]
        median_left = float(np.median(lefts))
        # An outlier is >5% from the median
        for page_num, left, result, prof in group:
            if abs(left - median_left) > 5.0:
                # This page's left edge is an outlier — reassess
                pdf_path = None
                for pn, pp in pages:
                    if pn == page_num:
                        pdf_path = pp
                        break
                if not pdf_path:
                    continue

                # Re-run with the established pitch
                ads = page_ads.get(page_num, [])
                ctx = build_context(page_num, year, db_path=db_path,
                                   profile=prof, ads=ads,
                                   issue_pitch=pitch, issue_columns=num_columns)
                clustered = pass1_detections.get(page_num, [])
                final = place_columns(clustered, ctx)

                page_out = os.path.join(output_dir, f"p{page_num}")
                if os.path.exists(page_out):
                    for f in os.listdir(page_out):
                        if ("_col" in f and f.endswith(".png")) or f == "page_meta.json":
                            os.remove(os.path.join(page_out, f))
                os.makedirs(page_out, exist_ok=True)

                new_columns = extract_columns(pdf_path, final, 0, dpi, page_out)
                new_result = PageResult(
                    pdf_path=pdf_path, page_number=0, dpi=dpi,
                    page_width_px=0, page_height_px=0,
                    num_columns=len(new_columns), columns=new_columns,
                    detection_row=[], quality_flags=["pass3_outlier_fix"],
                    error=None, elapsed_seconds=0,
                )
                _save_metadata(new_result, os.path.join(page_out, "page_meta.json"))

                if new_result.columns:
                    new_left = new_result.columns[0].left_vw
                    if abs(new_left - median_left) < abs(left - median_left):
                        # Update the result
                        for i, (pn, r, p) in enumerate(pass1_results):
                            if pn == page_num:
                                pass1_results[i] = (pn, new_result, p)
                                break
                        outliers_fixed += 1

    if outliers_fixed:
        print(f"\nPass 3: Fixed {outliers_fixed} left-edge outliers")

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
            # Include left edge of first column + right edges of all columns
            boundaries = [meta["columns"][0]["left_vw"]]
            boundaries += [col["right_vw"] for col in meta["columns"]]
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

        # Save raw page image (no overlay lines) for the SVG viewer
        raw_path = os.path.join(page_out, "page_raw.png")
        if not os.path.exists(raw_path):
            pil.convert("RGB").save(raw_path)

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

            # Get geometry for this page
            geom = conn.execute("""
                SELECT text_left, text_right FROM page_geometry
                WHERE year=? AND month=? AND day=? AND page=?
            """, (year, month, day, page)).fetchone()
            text_area = {"left": geom[0], "right": geom[1]} if geom else None

            pages.append({
                "page": page,
                "page_type": page_type,
                "num_columns": num_cols,
                "widths": widths,
                "flags": flags,
                "confidence": conf,
                "col_files": col_files,
                "has_overlay": has_overlay,
                "text_area": text_area,
            })

        ad_list = [{"page": p, "cols": c, "confidence": cf, "file": fn,
                     "x_pct": x, "y_pct": y, "w_pct": w, "h_pct": bh}
                   for p, c, cf, fn, x, y, w, bh in
                   conn.execute("""
                       SELECT page, cols, confidence, image_filename,
                              x_pct, y_pct, w_pct, h_pct
                       FROM detected_ads WHERE year=? AND month=? AND day=?
                       ORDER BY page
                   """, (year, month, day)).fetchall()]

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

    # Add global stats
    total_gazette_pages = conn.execute(
        "SELECT COUNT(*) FROM files WHERE file_type='pdf'"
    ).fetchone()[0] if False else 0
    # Re-open for stats
    conn2 = _sql.connect(db_path)
    total_gazette_pages = conn2.execute(
        "SELECT COUNT(*) FROM files WHERE file_type='pdf'"
    ).fetchone()[0]
    total_processed = conn2.execute(
        "SELECT COUNT(DISTINCT year||'-'||month||'-'||day||'-'||page) FROM page_layouts"
    ).fetchone()[0]
    total_ads = conn2.execute("SELECT COUNT(*) FROM detected_ads").fetchone()[0]
    conn2.close()

    viewer_data = {
        "total_gazette_pages": total_gazette_pages,
        "total_processed": total_processed,
        "total_ads": total_ads,
        "pct_done": round(total_processed / total_gazette_pages * 100, 2)
            if total_gazette_pages > 0 else 0,
        "issues": issues,
    }

    # Write JSON for the viewer
    viewer_data_path = os.path.join(columns_dir, "viewer_data.json")
    with open(viewer_data_path, "w") as f:
        json.dump(viewer_data, f, indent=2)


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
