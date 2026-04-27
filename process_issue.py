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
import sqlite3
import subprocess
import time
from contextlib import closing

import numpy as np

from split_page import PageResult, extract_columns, _save_metadata
from page_profile import profile_page
from detect_ads import (detect_ads, detect_single_col_ads,
                        extract_ad_images)
from page_context import build_context
from column_pipeline import detect_strips, cluster_boundaries, place_columns
from db_writer import DBWriter, DirectDBWriter


def download_issue(year, month, day, db_path="data/mvtm.db",
                   download_dir=None):
    """
    Download all PDF pages for an issue from Google Drive.

    Returns list of (page_number, pdf_path) tuples.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("""
            SELECT page, drive_id, directory_path FROM files
            WHERE year=? AND month=? AND day=? AND file_type='pdf'
            ORDER BY page
        """, (year, month, day)).fetchall()

    if not rows:
        print(f"No pages found for {year}-{month:02d}-{day:02d}")
        return []

    if download_dir is None:
        download_dir = f"/tmp/issue_{year}-{month:02d}-{day:02d}"
    os.makedirs(download_dir, exist_ok=True)

    pages = []
    cached_count = 0
    for page_num, drive_id, dpath in rows:
        fname = dpath.split("/")[-1]
        pdf_path = os.path.join(download_dir, fname)

        # Skip download if a valid PDF is already cached on disk. The
        # source files in Drive don't change once published, so a cached
        # %PDF- magic-byte match is enough — no need to re-fetch and
        # re-validate every run. Saves ~5–10s per issue on warm runs.
        if os.path.exists(pdf_path):
            try:
                with open(pdf_path, "rb") as f:
                    if f.read(5) == b"%PDF-":
                        pages.append((page_num, pdf_path))
                        cached_count += 1
                        continue
            except OSError:
                pass  # fall through to re-download

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

    if cached_count:
        print(f"  ({cached_count} of {len(rows)} pages from cache)")

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
                  download_dir=None, dpi=450, writer: DBWriter = None,
                  skip_aggregates: bool = False):
    """
    Process all pages of an issue with two-pass pitch establishment.

    Pass 1: Independent detection on every page
    Pass 2: Establish pitch from best pages, re-process weak ones

    Args:
        year, month, day: Issue date
        output_dir: Where to save column PNGs (default: columns/YYYY-MM-DD/)
        db_path: SQLite database path. Used for reads (build_context,
                 _update_viewer_data) regardless of which writer is in use.
        download_dir: Where to cache downloaded PDFs
        dpi: Render resolution
        writer: DBWriter instance for routing every write through. Default
                None → a DirectDBWriter is constructed against db_path,
                preserving standalone behaviour. The parallel batch driver
                passes a ProxyDBWriter that fans writes into a coordinator.
        skip_aggregates: When True, skip cross-issue aggregate steps
                (compute_era_patterns, _update_viewer_data). The batch
                driver runs these once at end-of-batch instead of per-issue.

    Returns:
        dict with pitch, num_columns, page_results, grounding_pages
    """
    t0 = time.time()

    if writer is None:
        writer = DirectDBWriter(db_path)

    if output_dir is None:
        output_dir = f"columns/{year}-{month:02d}-{day:02d}"

    # ── Download ─────────────────────────────────────────────────────
    print(f"Downloading {year}-{month:02d}-{day:02d}...")
    pages = download_issue(year, month, day, db_path, download_dir)
    if not pages:
        return {"error": "no_pages_found"}

    print(f"  {len(pages)} pages downloaded")

    # ── Clean previous ad data for this issue ───────────────────────
    writer.delete_issue_ads(year, month, day)

    # ── Ad detection (before column detection) ─────────────────────
    print("Detecting display ads...")
    page_ads = {}  # page_num → list of ad dicts
    page_single_col_ads = {}  # page_num → list of single-col ad dicts
    page_profiles = {}  # page_num → profile dict (computed once, reused in Pass 1/2)
    total_ads = 0

    ads_dir = os.path.join(output_dir, "ads")
    os.makedirs(ads_dir, exist_ok=True)

    for page_num, pdf_path in pages:
        prof = profile_page(pdf_path)
        page_profiles[page_num] = prof
        ads = detect_ads(pdf_path, column_pitch=None, page_profile=prof)
        if ads:
            ad_out = os.path.join(ads_dir, f"p{page_num}")
            ads_with_images = extract_ad_images(pdf_path, ads, ad_out, dpi=dpi)
            # extract_ad_images stamps each ad with a uuid; store_ads
            # writes the uuid to the DB. No round-trip needed — workers
            # in the parallel pipeline won't have to wait for SQLite to
            # assign auto-increment ids before continuing.
            writer.store_ads(year, month, day, page_num, ads_with_images)
            page_ads[page_num] = ads_with_images
            total_ads += len(ads)
            ad_desc = ", ".join(str(a["cols"]) + "col" for a in ads)
            print(f"  P{page_num}: {len(ads)} ads ({ad_desc})")

        # Single-column display ads — sibling pass to multi-col.
        # Extracted as images alongside multi-col ads (with sc_ad
        # filename prefix to avoid collision) and stored in the same
        # detected_ads table (cols=1 distinguishes them). Not used as
        # ad_zones for boundary detection.
        sc_ads = detect_single_col_ads(pdf_path, multi_col_ads=ads,
                                       page_profile=prof)
        if sc_ads:
            ad_out = os.path.join(ads_dir, f"p{page_num}")
            sc_with_images = extract_ad_images(pdf_path, sc_ads, ad_out,
                                               dpi=dpi, name_prefix="sc_ad")
            writer.store_ads(year, month, day, page_num, sc_with_images)
            page_single_col_ads[page_num] = sc_with_images
            print(f"  P{page_num}: {len(sc_ads)} single-col ads")

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
    page_strip_profiles = {}  # page_num → strip darkness profiles for viewer
    page_dark_thresholds = {} # page_num → darkness threshold used for detection
    page_contexts = {}

    for page_num, pdf_path in pages:
        prof = page_profiles[page_num]  # P4: reuse profile from ad-detection loop
        ads = page_ads.get(page_num, [])

        ctx = build_context(page_num, year, db_path=db_path,
                           profile=prof, ads=ads)
        page_contexts[page_num] = ctx

        raw, strip_profiles, dark_thresh = detect_strips(pdf_path, ctx, dpi=dpi)
        clustered = cluster_boundaries(raw,
            strip_profiles=strip_profiles, ad_zones=ctx.ad_zones)
        pass1_detections[page_num] = clustered
        page_strip_profiles[page_num] = strip_profiles
        page_dark_thresholds[page_num] = dark_thresh

        n_det = len(clustered)
        n_ads = len(ads)
        ad_note = f" [{n_ads} ads]" if n_ads else ""
        p2_note = " [P2 editorial]" if ctx.is_page_2 and ctx.page_2_template else ""
        print(f"  P{page_num} ({ctx.page_type}): {n_det} boundaries detected{ad_note}{p2_note}")

    # ── Establish pitch from detected boundaries ────────────────────
    # Compute pitch from the gaps between detected boundaries.
    # Keep recto and verso separate — photography differences affect
    # the apparent pitch. Guard against missed boundaries by treating
    # gaps > 1.5× the initial median as doubled gaps (divide by 2).
    recto_gaps = []
    verso_gaps = []
    for page_num, clustered in pass1_detections.items():
        if len(clustered) < 3:
            continue
        positions = sorted(b["x_pct"] for b in clustered)
        gaps = [positions[i+1] - positions[i] for i in range(len(positions)-1)]
        # Filter to plausible single-column widths (8-16%)
        plausible = [g for g in gaps if 8 < g < 16]
        if not plausible:
            continue
        initial_median = float(np.median(plausible))
        # Now re-examine all gaps: if a gap is ~2x the median, treat
        # it as two pitches (a missed boundary)
        for g in gaps:
            if 8 < g < initial_median * 1.4:
                # Normal single-pitch gap
                target = recto_gaps if page_contexts[page_num].page_type == "recto" else verso_gaps
                target.append(g)
            elif initial_median * 1.4 <= g < initial_median * 2.5:
                # Likely a doubled gap (missed boundary) — halve it
                target = recto_gaps if page_contexts[page_num].page_type == "recto" else verso_gaps
                target.append(g / 2)

    if not recto_gaps and not verso_gaps:
        print("  Could not establish pitch — no plausible boundaries detected")
        return {"error": "no_pitch"}

    # Compute pitch per page type, keep full precision (2 decimals)
    recto_pitch = round(float(np.median(recto_gaps)), 2) if recto_gaps else None
    verso_pitch = round(float(np.median(verso_gaps)), 2) if verso_gaps else None
    # Overall pitch — prefer recto (cleaner signal), fall back to verso
    pitch = recto_pitch or verso_pitch

    # Column count from recto pages (no sliver contamination)
    recto_counts = []
    for page_num, clustered in pass1_detections.items():
        if len(clustered) >= 3 and page_contexts[page_num].page_type == "recto":
            recto_counts.append(len(clustered))
    if recto_counts:
        median_boundaries = round(float(np.median(recto_counts)))
        num_columns = median_boundaries + 1
        num_columns = max(3, min(8, num_columns))
    elif any(len(c) >= 3 for c in pass1_detections.values()):
        all_counts = [len(c) for c in pass1_detections.values() if len(c) >= 3]
        num_columns = round(float(np.median(all_counts))) + 1
        num_columns = max(3, min(8, num_columns))
    else:
        num_columns = 7

    grounding_pages = [pn for pn in pass1_detections
                       if len(pass1_detections[pn]) >= 3
                       and page_contexts[pn].page_type == "recto"][:2]
    if not grounding_pages:
        grounding_pages = [pn for pn in pass1_detections if len(pass1_detections[pn]) >= 3][:2]

    pitch_note = f"recto={recto_pitch}" if recto_pitch else ""
    if verso_pitch:
        pitch_note += f" verso={verso_pitch}" if pitch_note else f"verso={verso_pitch}"
    print(f"\nPitch: {pitch}% from {num_columns} columns "
          f"({pitch_note}, grounding: {grounding_pages})")

    # ── Page 2 editorial template ───────────────────────────────────
    from layout_intelligence import LayoutDB
    _db = LayoutDB(db_path)
    p2_template = _db.get_template("page2_editorial_wide", 2, year)
    if p2_template:
        print(f"  Page 2 editorial template: {p2_template['year_start']}-{p2_template['year_end']}")

    # ── Pass 2 Phase A: Place columns (per page, no extraction) ──────
    # Now we know the pitch, rebuild context for every page and run
    # placement. Detected boundaries from pass 1 are reused.
    #
    # Phase A is intentionally lightweight: place_columns + cheap edge
    # validation. No PNG extraction and no body_text/headline detection
    # happen here, because Pass 3 below may rewrite the boundaries to
    # reconcile cross-page left-edge alignment. Doing detection here
    # would bind body_text/headlines to pre-pass-3 boundaries that
    # later disagree with the page_layouts row — the Frankenstein state
    # observed in the 1947 batch (page_analysis.json showed N body
    # charts, page_meta.json showed N+2 columns).
    print(f"\nPass 2: Placing columns with pitch={pitch:.1f}%...")
    page_layouts = {}
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

        # Validate: drop edge "columns" that are implausibly empty
        # (margin slivers or right-side ad strips that the boundary
        # detector mistook for content columns). Cheap ink-only check;
        # the richer body-text-aware validator runs in Phase C below.
        try:
            from validate_columns import validate_edge_columns
            final, dropped = validate_edge_columns(final, pdf_path)
            for side, ink, med, ratio in dropped:
                print(f"  P{page_num}: dropped empty {side} column "
                      f"(ink {ink:.1f} = {ratio*100:.0f}% of median {med:.1f})")
        except Exception as e:
            print(f"  P{page_num}: edge-col validation skipped ({e})")

        page_layouts[page_num] = {
            'pdf_path': pdf_path,
            'ctx': ctx,
            'prof': prof,
            'final': final,
            'quality_flags': [],
        }

    # ── Pass 3: Cross-page consistency check (boundaries only) ───────
    # Check that leftmost column positions are consistent within recto
    # and verso groups. Outliers get reassessed. This pass updates
    # boundaries in `page_layouts` only — no extraction yet, so the
    # subsequent Phase C work always operates on the reconciled
    # boundary set (no stale page_analysis.json possible).
    recto_pages = []
    verso_pages = []
    for page_num, layout in page_layouts.items():
        if not layout['final'] or len(layout['final']) < 2:
            continue
        left = layout['final'][0]['x_pct']
        page_type = layout['prof'].get('page_type')
        if page_type == "recto":
            recto_pages.append((page_num, left))
        elif page_type == "verso":
            verso_pages.append((page_num, left))

    outliers_fixed = 0
    for _group_name, group in [("recto", recto_pages), ("verso", verso_pages)]:
        if len(group) < 3:
            continue
        lefts = [g[1] for g in group]
        median_left = float(np.median(lefts))
        # An outlier is >5% from the median
        for page_num, left in group:
            if abs(left - median_left) > 5.0:
                layout = page_layouts[page_num]
                ads = page_ads.get(page_num, [])
                ctx = build_context(page_num, year, db_path=db_path,
                                   profile=layout['prof'], ads=ads,
                                   issue_pitch=pitch, issue_columns=num_columns)
                clustered = pass1_detections.get(page_num, [])
                new_final = place_columns(clustered, ctx)
                if new_final and len(new_final) >= 2:
                    new_left = new_final[0]['x_pct']
                    if abs(new_left - median_left) < abs(left - median_left):
                        layout['final'] = new_final
                        layout['ctx'] = ctx
                        layout['quality_flags'] = ['pass3_outlier_fix']
                        outliers_fixed += 1

    if outliers_fixed:
        print(f"\nPass 3: Fixed {outliers_fixed} left-edge outliers")

    # ── Pass 2 Phase C: Extract columns + run detectors (per page) ───
    # Runs on FINAL boundaries (after Pass 3 reconciliation), so
    # body_text, headlines, and the saved analysis are guaranteed
    # consistent with page_meta.json and the page_layouts DB row.
    pass1_results = []
    for page_num, pdf_path in pages:
        layout = page_layouts.get(page_num)
        if not layout:
            continue
        prof = layout['prof']
        ctx = layout['ctx']
        final = layout['final']
        quality_flags = list(layout.get('quality_flags', []))
        page_t0 = time.time()

        # Extract columns
        page_out = os.path.join(output_dir, f"p{page_num}")
        if os.path.exists(page_out):
            for f in os.listdir(page_out):
                if ("_col" in f and f.endswith(".png")) or f == "page_meta.json":
                    os.remove(os.path.join(page_out, f))
        os.makedirs(page_out, exist_ok=True)

        ads_with_uuids = [
            {"uuid": a["uuid"], "x_pct": a["x_pct"], "y_pct": a["y_pct"],
             "x_end_pct": a["x_end_pct"], "y_end_pct": a["y_end_pct"]}
            for a in (page_ads.get(page_num, []) +
                      page_single_col_ads.get(page_num, []))
            if "uuid" in a
        ]
        columns = extract_columns(pdf_path, final, 0, dpi, page_out,
                                  ads_with_uuids=ads_with_uuids)
        result = PageResult(
            pdf_path=pdf_path, page_number=0, dpi=dpi,
            page_width_px=0, page_height_px=0,
            num_columns=len(columns), columns=columns,
            detection_row=[], quality_flags=quality_flags,
            error=None, elapsed_seconds=0,
        )
        # page_meta.json is saved later, after the post-detection
        # validator has had a chance to drop phantom edge columns.

        # Save analysis data for the viewer (profile chart + strip profiles
        # + raw detected boundary positions before placement)
        import time as _time
        analysis = {
            "run_id": int(_time.time()),  # iteration identifier
        }
        profile_chart = prof.get("profile_chart")
        if profile_chart:
            analysis["profile_chart"] = profile_chart
            analysis["shadow_thresh"] = prof.get("shadow_thresh", 0)
            analysis["content_floor"] = prof.get("content_floor", 0)
            analysis["paper_baseline"] = prof.get("paper_baseline", 0)
        strips = page_strip_profiles.get(page_num)
        if strips:
            analysis["strip_profiles"] = strips
            analysis["darkness_threshold"] = page_dark_thresholds.get(page_num, 60)
            # Ad exclusion zones used during detection (x1%, x2%, y1%, y2%)
            if ctx.ad_zones:
                analysis["ad_exclusion_zones"] = [
                    {"x1": z[0], "x2": z[1], "y1": z[2], "y2": z[3]}
                    for z in ctx.ad_zones
                ]
        # Composite strip chart: sum of all strip values, with ad zones zeroed.
        # Shows the combined signal across all strips at each x position.
        if strips and len(strips) > 0:
            # Build a common x-axis from the first strip's profile
            ref_profile = strips[0]["profile"]
            composite = [{"pct": p["pct"], "val": 0.0} for p in ref_profile]
            ad_zones_list = ctx.ad_zones  # [(x1,x2,y1,y2), ...]
            for strip in strips:
                sp = strip["profile"]
                y1 = strip["y_start_pct"]
                y2 = strip["y_end_pct"]
                # Check if this strip is blocked by any ad at each x
                for i, pt in enumerate(sp):
                    if i >= len(composite):
                        break
                    x_pct = pt["pct"]
                    in_ad = any(
                        az[0] < x_pct < az[1] and az[2] < y2 and az[3] > y1
                        for az in ad_zones_list
                    )
                    if not in_ad:
                        composite[i]["val"] += pt["val"]
            # Round values
            for c in composite:
                c["val"] = round(c["val"], 1)
            analysis["composite_profile"] = composite

        # Raw clustered boundary positions — what the detector actually found,
        # before pitch projection or grid snapping.
        raw = pass1_detections.get(page_num, [])
        if raw:
            analysis["detected_boundaries"] = [
                {"pct": b["x_pct"], "score": b.get("weighted_score", 0)}
                for b in raw
            ]

        # Headline detection — multi-column headlines identified by
        # gutter-fill analysis
        gutter_fills_for_lt = None
        try:
            from detect_headlines import detect_headlines
            boundary_pcts = [b["x_pct"] for b in raw] if raw else []
            if len(boundary_pcts) >= 3:
                r2 = prof.get("r2", {})
                headlines, hl_analysis = detect_headlines(
                    pdf_path, boundary_pcts,
                    ad_zones=ctx.ad_zones,
                    r2_top_pct=r2.get("top"),
                    r2_bottom_pct=r2.get("bottom"))
                if headlines:
                    analysis["headlines"] = headlines
                if hl_analysis:
                    analysis["headline_chart"] = hl_analysis.get("headline_chart")
                    analysis["gutter_fills"] = hl_analysis.get("gutter_fills")
                    gutter_fills_for_lt = hl_analysis.get("gutter_fills")
        except Exception:
            pass

        # Body text detection — runs after column placement
        try:
            from detect_body_text import detect_body_text
            meta_cols = []
            for c in result.columns:
                meta_cols.append({
                    'index': c.index,
                    'left_vw': c.left_vw,
                    'right_vw': c.right_vw,
                })
            r2_prof = prof.get("r2", {})
            body_regions, body_charts, blur_img, h_rules, large_type = \
                detect_body_text(pdf_path, meta_cols,
                    r2_top_pct=r2_prof.get("top"),
                    r2_bottom_pct=r2_prof.get("bottom"),
                    gutter_fills=gutter_fills_for_lt,
                    ad_zones=ctx.ad_zones)
            if body_regions:
                analysis["body_text"] = body_regions
            if body_charts:
                analysis["body_text_charts"] = body_charts
            if h_rules:
                analysis["h_rules"] = h_rules
            if large_type:
                analysis["large_type"] = large_type
            if blur_img is not None:
                from PIL import Image as _PILImg
                blur_path = os.path.join(page_out, "body_blur.png")
                _PILImg.fromarray(blur_img).save(blur_path)
        except Exception:
            pass

        # ── Post-detection edge-column validation (v2) ───────────────
        # v1 (Phase A) drops obviously empty edges by ink alone.
        # v2 catches scan-bleed/edge-rule "columns" that have ink
        # but fail every detector: no body text, no ad coverage,
        # no headline. See validate_columns.validate_columns_v2.
        try:
            from validate_columns import validate_columns_v2
            ads_for_v2 = list(page_ads.get(page_num, [])) + \
                         list(page_single_col_ads.get(page_num, []))
            new_final, dropped_idx, drop_log = validate_columns_v2(
                final,
                analysis.get("body_text", []),
                ads_for_v2,
                analysis.get("headlines", []),
                text_area=prof.get("text_area"),
            )
            if dropped_idx:
                original_n = len(final) - 1
                survivors = [i for i in range(original_n)
                             if i not in dropped_idx]
                idx_remap = {old: new for new, old in enumerate(survivors)}

                # Re-extract PNGs against new boundaries
                final = new_final
                for f in os.listdir(page_out):
                    if "_col" in f and f.endswith(".png"):
                        os.remove(os.path.join(page_out, f))
                columns = extract_columns(pdf_path, final, 0, dpi,
                                          page_out,
                                          ads_with_uuids=ads_with_uuids)

                # Filter and renumber per-column data in analysis
                def _filter_renumber(items):
                    out = []
                    for it in items:
                        ci = it.get("col_idx")
                        if ci is None or ci in dropped_idx:
                            continue
                        new_it = dict(it)
                        new_it["col_idx"] = idx_remap[ci]
                        out.append(new_it)
                    return out

                for k in ("body_text", "body_text_charts",
                         "h_rules", "large_type"):
                    if k in analysis:
                        analysis[k] = _filter_renumber(analysis[k])

                quality_flags = list(quality_flags)
                for side, _sig in drop_log:
                    quality_flags.append(f"col_v2_drop_{side}")
                result = PageResult(
                    pdf_path=pdf_path, page_number=0, dpi=dpi,
                    page_width_px=0, page_height_px=0,
                    num_columns=len(columns), columns=columns,
                    detection_row=[], quality_flags=quality_flags,
                    error=None, elapsed_seconds=0,
                )
                sides_summary = [d[0] for d in drop_log]
                print(f"  P{page_num}: v2 dropped phantom edge "
                      f"col(s) {sides_summary} "
                      f"(orig idx {sorted(dropped_idx)})")
        except Exception as e:
            print(f"  P{page_num}: v2 validation skipped ({e})")

        # Save page_meta.json with the final (post-v2) boundaries.
        _save_metadata(result, os.path.join(page_out, "page_meta.json"))

        # Single-column display ads (sibling layer to multi-col ads)
        sc = page_single_col_ads.get(page_num, [])
        if sc:
            analysis["single_col_ads"] = sc

        if analysis:
            with open(os.path.join(page_out, "page_analysis.json"), "w") as f:
                json.dump(analysis, f)

        cv, _, nc = _score_regularity(result)
        widths = " ".join(f"{c.width_vw:.0f}%" for c in result.columns)
        p2_note = " [P2 editorial]" if ctx.is_page_2 and ctx.page_2_template else ""
        page_dt = time.time() - page_t0
        print(f"  P{page_num}: {nc}c [{widths}] CV={cv:.3f}{p2_note} ({page_dt:.1f}s)")

        pass1_results.append((page_num, result, prof))

    # ── Store in database ──────────────────────────────────────────
    # Clean any previous data for this issue
    writer.delete_issue_layouts(year, month, day)

    for page_num, result, prof in pass1_results:
        meta_path = os.path.join(output_dir, f"p{page_num}", "page_meta.json")
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                meta = json.load(f)
            # Include left edge of first column + right edges of all columns
            boundaries = [meta["columns"][0]["left_vw"]]
            boundaries += [col["right_vw"] for col in meta["columns"]]
            cv, _, _ = _score_regularity(result)
            writer.record_layout(year, month, day, page_num,
                                 result.num_columns, boundaries,
                                 result.quality_flags,
                                 round(1.0 / (1.0 + cv), 3))
        writer.record_geometry(year, month, day, page_num, prof)

    if not skip_aggregates:
        # compute_era_patterns is a cross-issue aggregate. The parallel
        # batch driver runs it once at end-of-batch instead.
        from layout_intelligence import LayoutDB
        LayoutDB(db_path).compute_era_patterns()

    # ── Generate overlays ────────────────────────────────────────────
    print("\nGenerating overlays...")
    from PIL import Image
    import fitz as _fitz

    for page_num, _result, _prof in pass1_results:
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
        pil.convert("RGB").save(raw_path)

        # ── overlay.png generation (disabled) ──────────────────────
        # Pre-SVG-viewer dev-validation artifact: a baked-in raster of
        # text-area edges and column rules drawn on top of page_raw.png.
        # The page_viewer now renders these dynamically as toggleable SVG
        # layers over page_raw.png, so the static raster is redundant —
        # ~1.9MB and ~190ms per page for a fallback that never triggers
        # (page_viewer.html only refs overlay.png in an `imgEl.onerror`
        # branch). Kept commented for re-enabling if a static overlay
        # image is ever needed offline (e.g. emailing a quick visual to
        # a non-viewer audience).
        # ol = Image.new("RGBA", pil.size, (0, 0, 0, 0))
        # draw = ImageDraw.Draw(ol)
        # ih, iw = pil.size[1], pil.size[0]
        #
        # def _vl(x_pct, color, width=2):
        #     x = int(x_pct / 100 * iw)
        #     draw.line([(x, 0), (x, ih)], fill=color, width=width)
        #
        # ta = prof.get("text_area", {})
        # if ta:
        #     _vl(ta.get("left", 0), (255, 140, 0, 255), 3)
        #     _vl(ta.get("right", 100), (255, 140, 0, 255), 3)
        #
        # meta_path = os.path.join(page_out, "page_meta.json")
        # if os.path.exists(meta_path):
        #     with open(meta_path) as f:
        #         meta = json.load(f)
        #     for col in meta["columns"]:
        #         _vl(col["left_vw"], (0, 100, 255, 200), 2)
        #         _vl(col["right_vw"], (0, 100, 255, 200), 2)
        #
        # overlay_path = os.path.join(page_out, "overlay.png")
        # Image.alpha_composite(pil, ol).convert("RGB").save(overlay_path)

    print("  Done")

    # ── Update viewer data ──────────────────────────────────────────
    if not skip_aggregates:
        # _update_viewer_data reads ALL issues from the DB to rebuild the
        # listing JSON. The parallel batch driver runs it once at
        # end-of-batch instead of per-issue.
        _update_viewer_data(db_path, "columns")

    # ── Summary ──────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")
    print(f"Issue: {year}-{month:02d}-{day:02d}")
    print(f"Pitch: {pitch:.1f}%, {num_columns} columns")
    print(f"Grounding: pages {grounding_pages}")
    for page_num, result, _prof in pass1_results:
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

        pages = []
        for page, num_cols, widths_json, flags_json, conf in layouts:
            widths = json.loads(widths_json) if widths_json else []
            flags = json.loads(flags_json) if flags_json else []
            page_type = "recto" if page % 2 == 1 else "verso"

            # Check what files exist
            page_dir = os.path.join(columns_dir, f"{year}-{month:02d}-{day:02d}", f"p{page}")
            col_files = sorted(f for f in os.listdir(page_dir)
                              if "_col" in f and f.endswith(".png")) if os.path.exists(page_dir) else []
            has_page_raw = os.path.exists(os.path.join(page_dir, "page_raw.png")) if os.path.exists(page_dir) else False

            # Get geometry for this page
            geom = conn.execute("""
                SELECT r2_left, r2_right, r3_left, r3_right,
                       text_left, text_right FROM page_geometry
                WHERE year=? AND month=? AND day=? AND page=?
            """, (year, month, day, page)).fetchone()
            if geom:
                r2 = {"left": geom[0], "right": geom[1]}
                r3 = {"left": geom[2], "right": geom[3]}
                text_area = {"left": geom[4], "right": geom[5]}
            else:
                r2 = r3 = text_area = None

            pages.append({
                "page": page,
                "page_type": page_type,
                "num_columns": num_cols,
                "widths": widths,
                "flags": flags,
                "confidence": conf,
                "col_files": col_files,
                "has_page_raw": has_page_raw,
                "r2": r2,
                "r3": r3,
                "text_area": text_area,
            })

        # Read body_text per page for ad false-positive filtering.
        # Body-text-shaped FPs from the ad detector (squarish 2-col
        # clippings of body text columns) overlap heavily with the
        # body_text regions written by detect_body_text. High-confidence
        # ads (strong borders, rect_ratio > 0.85) are trusted as-is and
        # never filtered.
        body_text_by_page = {}
        for page, *_ in layouts:
            page_dir = os.path.join(
                columns_dir, f"{year}-{month:02d}-{day:02d}", f"p{page}")
            pa_path = os.path.join(page_dir, "page_analysis.json")
            if os.path.exists(pa_path):
                try:
                    with open(pa_path) as f:
                        body_text_by_page[page] = json.load(f).get("body_text", [])
                except Exception:
                    body_text_by_page[page] = []
            else:
                body_text_by_page[page] = []

        def _is_body_text_fp(page, conf_, x, y, w, h):
            """True if a low-confidence ad rectangle is mostly covered
            by body_text regions and spans 2+ columns. Only `low`
            confidence (rect_ratio < 0.70) is filtered — medium and
            high confidence have a real border and are trusted, even
            when they contain body-text-like internal content."""
            if conf_ != "low":
                return False
            bt = body_text_by_page.get(page) or []
            if not bt:
                return False
            ad_x2, ad_y2 = x + w, y + h
            if w <= 0 or h <= 0:
                return False
            ad_area = w * h
            total_overlap = 0.0
            cols_overlapping = set()
            for r in bt:
                ox1 = max(x, r["x1_pct"]); ox2 = min(ad_x2, r["x2_pct"])
                oy1 = max(y, r["y1_pct"]); oy2 = min(ad_y2, r["y2_pct"])
                if ox2 > ox1 and oy2 > oy1:
                    total_overlap += (ox2 - ox1) * (oy2 - oy1)
                    cols_overlapping.add(r.get("col_idx"))
            return (total_overlap / ad_area) > 0.5 and len(cols_overlapping) >= 2

        ad_rows = conn.execute("""
            SELECT uuid, page, cols, confidence, image_filename,
                   x_pct, y_pct, w_pct, h_pct
            FROM detected_ads WHERE year=? AND month=? AND day=?
            ORDER BY page, uuid
        """, (year, month, day)).fetchall()

        ad_list = []
        for ad_uuid, p, c, cf, fn, x, y, w, bh in ad_rows:
            if _is_body_text_fp(p, cf, x, y, w, bh):
                continue
            ad_list.append({"uuid": ad_uuid, "page": p, "cols": c,
                            "confidence": cf, "file": fn,
                            "x_pct": x, "y_pct": y,
                            "w_pct": w, "h_pct": bh})

        issue_dir = f"{year}-{month:02d}-{day:02d}"
        summary_path = os.path.join(columns_dir, issue_dir, "issue_summary.json")
        pitch = None
        if os.path.exists(summary_path):
            with open(summary_path) as f:
                s = json.load(f)
                pitch = s.get("pitch")

        # Get last processed timestamp
        last_ran = conn.execute("""
            SELECT MAX(created_at) FROM page_layouts
            WHERE year=? AND month=? AND day=?
        """, (year, month, day)).fetchone()
        last_ran_str = last_ran[0] if last_ran and last_ran[0] else None

        issues.append({
            "year": year, "month": month, "day": day,
            "dir": issue_dir,
            "pitch": pitch,
            "last_ran": last_ran_str,
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
        json.dump(viewer_data, f, separators=(",", ":"))


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
