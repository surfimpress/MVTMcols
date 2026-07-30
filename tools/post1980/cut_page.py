"""Top-level entry: cut one PDF page into modular blocks.

Outputs (all under `columns_modular/<YYYY-MM-DD>/p<N>/`):

  - <kind>-<id>.png crops for each detected region
    kinds: article_block, headline, pull_quote, display_ad, photo,
           section_banner
  - qa/p<N>_overlay.png — combined overlay render for visual QA
  - qa/p<N>_layers.json — machine-readable record of detections

Does NOT yet write to `file_assets`; that's a follow-up once the
visual output is approved.
"""
import argparse
import json
import os
import re
import sys

import fitz   # PyMuPDF
import numpy as np

# Make the package importable when invoked as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from post1980.spans import extract_spans, body_font_size
from post1980.page_layout import (
    find_masthead_bottom, find_whitespace_bands, classify_page,
)
from post1980.text_groups import (
    real_headlines, is_pull_quote, merge_headline_runs, cluster_body_columns,
)
from post1980.visual_regions import (
    extract_image_regions, extract_drawn_rects, classify_display_ads,
    detect_display_ads_classical,
)
from post1980.article_blocks import (
    assemble_articles, attach_photos, clip_overlapping, absorb_contained,
    drop_noise_articles,
)
from post1980.uncovered import find_uncovered_content
from post1980.column_grid import find_column_grid

# Classical page profiler — gives us per-page ink/paper means, dynamic
# range, and quality flags so we can pick an adaptive ink threshold
# instead of the fixed RULE_DARK_THR=130. Mirrors the STRICT/LOOSE
# pattern detect_ads.py uses for the same reason.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
from page_profile import profile_page


def _adaptive_dark_thr(profile, fallback=130):
    """Midpoint between paper baseline and ink mean (in original
    greyscale space). page_profile reports values in INVERTED space,
    so paper_mean ≈ 255 − original_paper; we convert and take the
    midpoint, then clamp to a sane range.
    """
    if profile is None:
        return fallback
    try:
        paper_inv = profile["paper_mean"]
        ink_inv = profile["ink_mean"]
        midpoint_orig = 255 - (paper_inv + ink_inv) / 2.0
        return max(70.0, min(180.0, midpoint_orig))
    except Exception:
        return fallback


# Overlay colours (RGB). Picked to be colourblind-distinct
# (per project memory feedback_colourblind_palette: avoid green/red
# alone; rely on blue/orange/black plus varied stroke).
COL = {
    "masthead":      (170, 170, 170, 150),  # filled grey — bumped opacity
    "article":       (  0, 100, 255, 230),  # blue stroke @ 0.9 alpha
    "headline":      (  0,  50, 180, 255),  # darker blue stroke
    "pull_quote":    (220, 170,   0, 230),  # ochre stroke
    "display_ad":    (220,  30,  30, 230),  # red stroke
    "photo":         (255, 150, 180, 230),  # light pink stroke — visible vs
                                            # page content (per user 2026-05-18)
    "image_only":    ( 80,  80,  80, 60),   # grey fill (whole page)
    "classifieds":   (180,  80,   0, 60),   # ochre fill (whole page)
    "uncovered":     (255, 130,   0, 230),  # orange stroke — uncovered ink-bearing region
    "grid_high":     ( 60,   0, 120, 220),  # dark purple — high-conf column boundary
    "grid_med":      (120,  60, 180, 180),  # mid purple — medium conf
    "grid_low":      (180, 130, 200, 130),  # light purple — low conf (single band)
    "grid_band":     (200, 220, 255, 80),   # faint blue fill — measurement band
    "racing_green":  (  0,  86,  59, 230),  # fundamental-grid dashed lines
                                            # — restored 2026-05-18: user
                                            # explicitly chose this colour
                                            # despite being colour-blind,
                                            # relying on the dash pattern
                                            # to distinguish; the cobalt-
                                            # blue substitute clashed with
                                            # other blue overlays.
}


def _draw_dashed_line(draw, p0, p1, fill, width=2, dash=14, gap=8):
    """Draw a dashed line from p0 to p1 (PIL has no built-in dashed)."""
    x0, y0 = p0; x1, y1 = p1
    dx = x1 - x0; dy = y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return
    step = dash + gap
    n = int(length / step) + 1
    for i in range(n):
        t0 = i * step / length
        t1 = min(t0 + dash / length, 1.0)
        if t0 >= 1.0:
            break
        sx0 = x0 + dx * t0; sy0 = y0 + dy * t0
        sx1 = x0 + dx * t1; sy1 = y0 + dy * t1
        draw.line([(sx0, sy0), (sx1, sy1)], fill=fill, width=width)


def cut_page(pdf_path, page_idx=0, out_root="columns_modular"):
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    page_w, page_h = page.rect.width, page.rect.height

    # In this corpus, each PDF file is one printed page; the actual
    # newspaper page number comes from the filename suffix
    # (e.g. 1985-02-13-03.pdf = page 3). Pages 2+ are "interior" —
    # they have section banners rather than full mastheads, so the
    # masthead-rule search zone tightens.
    _issue, page_no = issue_date_from_path(pdf_path)
    is_interior = bool(page_no and page_no >= 2)

    # Per-page profile (paper baseline, ink mean, dynamic range,
    # quality flags). Used to derive an adaptive ink threshold so
    # the row-fill / peakiness measurements are calibrated to this
    # particular page's scan rather than a fixed 130 baseline.
    try:
        profile = profile_page(pdf_path, page_number=page_idx)
    except Exception:
        profile = None
    dark_thr = _adaptive_dark_thr(profile)

    # 1A — classification
    spans = extract_spans(page)
    body = body_font_size(spans)
    page_class = classify_page(spans, page, page_w, page_h)

    layers = {
        "pdf": pdf_path,
        "page_idx": page_idx,
        "page_size": [page_w, page_h],
        "body_font_size": body,
        "page_class": page_class,
        "masthead_bottom": 0.0,
        "articles": [],
        "pull_quotes": [],
        "display_ads": [],
        "photos": [],
        "dark_thr": dark_thr,
        "page_profile_flags": (profile.get("quality_flags", [])
                                if profile else []),
        "page_profile_paper_mean": (profile.get("paper_mean")
                                     if profile else None),
        "page_profile_ink_mean": (profile.get("ink_mean")
                                   if profile else None),
        "page_profile_dyn_range": (profile.get("dynamic_range")
                                    if profile else None),
        # Text-area edges from page_profile (in pt). Drives the
        # left/right vertical lines of the fundamental grid overlay.
        "text_area_left_pt": (
            profile["text_area"]["left"] / 100.0 * page_w
            if profile and profile.get("text_area") else None
        ),
        "text_area_right_pt": (
            profile["text_area"]["right"] / 100.0 * page_w
            if profile and profile.get("text_area") else None
        ),
        "text_area_top_pt": (
            profile["text_area"]["top"] / 100.0 * page_h
            if profile and profile.get("text_area") else None
        ),
        "text_area_bottom_pt": (
            profile["text_area"]["bottom"] / 100.0 * page_h
            if profile and profile.get("text_area") else None
        ),
    }

    if page_class == "modular":
        # 1B — masthead (rule-based with text fallback). Interior
        # pages use a tighter search zone. Adaptive dark threshold
        # from page_profile so the row-fill measurement scales with
        # this page's actual paper/ink baseline.
        mast_y = find_masthead_bottom(
            spans, page_w, page_h, page=page,
            is_interior=is_interior,
            dark_thr=dark_thr,
        )
        layers["masthead_bottom"] = mast_y
        layers["page_no"] = page_no

        # 1D — display ads via the classical detector
        ads = detect_display_ads_classical(pdf_path, page_idx,
                                           page_w, page_h)
        layers["display_ads"] = [
            (a.x0, a.y0, a.x1, a.y1) for a in ads
        ]

        # 1B — body columns + real headlines
        body_spans = [s for s in spans
                      if 0.85 * body <= s.size <= 1.25 * body
                      and s.y0 > mast_y]
        cols = cluster_body_columns(spans, body, mast_y)
        raw_heads = real_headlines(spans, body, body_spans, mast_y)

        # Pull quotes are not detected separately — they're part of
        # articles (per user direction 2026-05-18). Every large-type
        # run is treated as an article headline.
        layers["pull_quotes"] = []
        heads = merge_headline_runs(raw_heads)

        # 1E — photos (image regions)
        photos = extract_image_regions(page, page_w, page_h)
        layers["photos"] = [
            (p.x0, p.y0, p.x1, p.y1) for p in photos
        ]

        # 1B+1E — article assembly
        bands = find_whitespace_bands(page, mast_y, dark_thr=dark_thr)
        layers["whitespace_bands"] = bands
        arts = assemble_articles(heads, cols, ads, page_w, page_h,
                                 whitespace_bands=bands)
        arts = clip_overlapping(arts)
        arts = absorb_contained(arts)
        # `drop_noise_articles` exists in article_blocks.py but is
        # disabled — empirically it dropped real articles whose OCR
        # text happened to be a pure-numeric ("100") or fragmented
        # alpha. The remaining spurious "of"/"s_" articles are
        # acceptable as known-noise; they overlap nothing meaningful
        # and the downstream LLM should ignore them.
        arts = attach_photos(arts, photos)

        # Column-grid detection. The grid isn't used yet to constrain
        # article bboxes — it's surfaced for visual audit first.
        # Pass display ads, photos AND headlines as obstacles so bands
        # avoid them all — running a measurement band through a head-
        # line gives a flat-dark profile (no gutter signal), through a
        # photo gives mid-grey contamination.
        obstacles = (
            layers["display_ads"]
            + [(p.x0, p.y0, p.x1, p.y1) for p in photos]
            + [(h.x0, h.y0, h.x1, h.y1) for h in heads]
        )
        col_bands, col_grid, coarse_pack, col_profiles = find_column_grid(
            page, mast_y, bands,
            obstacles,
            dark_thr=dark_thr,
            return_profiles=True,
            text_area_left_pt=layers.get("text_area_left_pt"),
            text_area_right_pt=layers.get("text_area_right_pt"),
        )
        layers["coarse_axes"] = list(coarse_pack["candidates"])
        layers["coarse_profile"] = coarse_pack["profile"]
        layers["coarse_profile_y_range"] = list(coarse_pack["y_range"])
        layers["coarse_quadrants_l1"] = coarse_pack.get("quadrants_l1", [])
        layers["coarse_quadrants_l2"] = coarse_pack.get("quadrants_l2", [])
        layers["coarse_segments_4x6"] = coarse_pack.get("segments_4x6", [])
        layers["pitch_grid"] = coarse_pack.get("pitch_grid")
        layers["scored_grid"] = coarse_pack.get("scored_grid")

        layers["column_grid_bands"] = [
            (b.y0_pt, b.y1_pt,
             b.x0_pt if b.x0_pt is not None else 0.0,
             b.x1_pt if b.x1_pt is not None else page_w)
            for b in col_bands
        ]
        layers["column_grid"] = [
            {"x_pt": g.x_pt, "confidence": g.confidence,
             "votes": g.vote_count}
            for g in col_grid
        ]
        # Per-band sampled col-mean profiles for plotting alongside
        # the overlay (so the user can see the signal that the grid
        # was derived from).
        layers["column_grid_profiles"] = col_profiles

        layers["articles"] = [
            {
                "bbox": a.bbox,
                "headline": {
                    "bbox": a.headline.bbox,
                    "size": a.headline.size,
                    "text": a.headline.text,
                },
                "columns": [
                    [c.x0_min, c.y0, c.x1_max, c.y1] for c in a.columns
                ],
                "photos": [p.bbox for p in a.photos],
            }
            for a in arts
        ]

        # Pixel-based headline detection — widen the OCR-derived
        # headline bboxes where the visual headline is broader than
        # the text-layer captured (per user direction 2026-05-18).
        # OCR on Adobe Paper Capture PDFs often truncates headlines.
        from post1980.headline_detect import (
            detect_headline_runs, widen_article_headlines,
            detect_horizontal_rules, detect_pixel_photos,
            find_closed_rectangles,
        )
        pixel_runs = detect_headline_runs(
            page, body_size_pt=body,
            mast_y=mast_y, page_h=page_h,
            text_area_left_pt=layers.get("text_area_left_pt"),
            text_area_right_pt=layers.get("text_area_right_pt"),
        )
        layers["pixel_headline_runs"] = [
            {"x0": r.x0, "y0": r.y0, "x1": r.x1, "y1": r.y1,
             "char_height_pt": r.char_height_pt,
             "n_chars": r.n_chars, "n_lines": r.n_lines}
            for r in pixel_runs
        ]
        n_widened = widen_article_headlines(layers["articles"], pixel_runs)
        layers["n_headlines_widened"] = n_widened

        # Pixel-based photo detection — on Adobe Paper Capture PDFs
        # photos are baked into the page raster and not exposed as
        # embedded image objects, so the OCR-derived photos list is
        # empty. The pixel detector finds dense moderately-dark
        # regions with low row-std (uniform fill).
        claimed_for_photo = (
            [a["bbox"] for a in (layers.get("articles") or [])
                if isinstance(a, dict) and "bbox" in a]
            + [(b[0], b[1], b[2], b[3])
                for b in (layers.get("display_ads") or [])]
            + [(p[0], p[1], p[2], p[3])
                for p in (layers.get("photos") or [])]
        )
        pixel_photos = detect_pixel_photos(
            page, mast_y=mast_y, page_h=page_h, page_w=page_w,
            claimed_bboxes=claimed_for_photo,
            text_area_left_pt=layers.get("text_area_left_pt"),
            text_area_right_pt=layers.get("text_area_right_pt"),
        )
        # Two filters on pixel-detected photos (per user direction
        # 2026-05-19):
        #   (a) drop a pixel photo that significantly overlaps an
        #       existing detected ad — the ad zone keeps its ad
        #       classification (the pixel detector spans the gap
        #       between adjacent ads).
        #   (b) if a pixel photo contains pixel-detected headlines or
        #       large body text inside, it's almost certainly an AD
        #       (with photos inside it), not a pure image. Move to
        #       display_ads instead of photos.
        def _bbox_iou_simple(a, b):
            ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
            ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
            iw = max(0.0, ix1 - ix0); ih = max(0.0, iy1 - iy0)
            inter = iw * ih
            aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
            ba = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
            union = aa + ba - inter
            return inter / union if union > 0 else 0.0

        def _contains_headline(photo_bbox, hl_runs, min_chars=4):
            for hr in hl_runs:
                if hr.n_chars < min_chars:
                    continue
                # Headline centre inside the photo bbox
                hcx = (hr.x0 + hr.x1) / 2.0
                hcy = (hr.y0 + hr.y1) / 2.0
                if (photo_bbox[0] <= hcx <= photo_bbox[2]
                        and photo_bbox[1] <= hcy <= photo_bbox[3]):
                    return True
            return False

        kept_photos = []
        for pp in pixel_photos:
            pp_bbox = (pp.x0, pp.y0, pp.x1, pp.y1)
            overlaps_ad = False
            for ad_b in (layers.get("display_ads") or []):
                if _bbox_iou_simple(pp_bbox, ad_b) >= 0.15:
                    overlaps_ad = True
                    break
            if overlaps_ad:
                continue   # ad wins
            # Headline-in-region check: pure photos don't carry text
            # at headline scale.
            if _contains_headline(pp_bbox, pixel_runs, min_chars=4):
                # Promote to display_ads instead — has text inside
                layers["display_ads"].append(pp_bbox)
                continue
            kept_photos.append(pp)
        for pp in kept_photos:
            layers["photos"].append((pp.x0, pp.y0, pp.x1, pp.y1))
        pixel_photos = kept_photos
        layers["pixel_photos"] = [
            {"x0": p.x0, "y0": p.y0, "x1": p.x1, "y1": p.y1,
             "density": p.density, "row_std": p.row_std}
            for pp in pixel_photos for p in [pp]
        ]

        # Detect all closed-rectangle frames in the page (any border
        # thickness). Used as item candidates — bordered content is
        # almost always its own item (sidebar / ad / image).
        closed_rects = find_closed_rectangles(
            page, mast_y=mast_y, page_h=page_h, page_w=page_w,
            column_grid=layers["column_grid"],
            text_area_left_pt=layers.get("text_area_left_pt"),
            text_area_right_pt=layers.get("text_area_right_pt"),
        )
        layers["closed_rectangles"] = [
            {"x0_pt": r.x0_pt, "y0_pt": r.y0_pt,
             "x1_pt": r.x1_pt, "y1_pt": r.y1_pt,
             "border_thickness_pt": r.border_thickness_pt}
            for r in closed_rects
        ]

        # Detect horizontal rules — used as merge barriers later
        h_rules = detect_horizontal_rules(
            page, mast_y=mast_y, page_h=page_h, page_w=page_w,
        )
        layers["horizontal_rules"] = [
            {"y_pt": r.y_pt, "x0_pt": r.x0_pt, "x1_pt": r.x1_pt,
             "thickness_pt": r.thickness_pt}
            for r in h_rules
        ]

        # Stage-1 snap: snap ads/photos/articles only. Uncovered comes
        # next, and uses these snapped bboxes as the claimed list — so
        # orange regions only bound what's genuinely unclaimed AFTER
        # snap (per user direction 2026-05-18).
        from post1980.column_grid import snap_obstacles_to_grid
        interim = snap_obstacles_to_grid(layers)

        # Build column + band-row grid edges for the orange scan
        sg = layers.get("scored_grid") or {}
        grid_gutters = [g["x_pt"]
                         for g in (sg.get("estimated_gutters") or [])]
        ta_l = layers.get("text_area_left_pt") or 0.0
        ta_r = layers.get("text_area_right_pt") or page_w
        col_edges = sorted(set([float(ta_l)]
                                + [float(x) for x in grid_gutters]
                                + [float(ta_r)]))
        band_centres = [(float(b[0]) + float(b[1])) / 2.0
                         for b in (layers.get("whitespace_bands") or [])]
        band_edges_list = sorted(set([float(mast_y)] + band_centres
                                      + [float(page_h)]))

        # Uncovered scan uses SNAPPED article/ad/photo bboxes as claimed
        snapped_claimed = (
            list(interim.get("display_ads") or [])
            + list(interim.get("photos") or [])
            + list(interim.get("articles") or [])
            + [tuple(pq[:4]) for pq in (layers.get("pull_quotes") or [])
                if len(pq) >= 4]
        )
        uncovered = find_uncovered_content(
            page, mast_y, snapped_claimed, page_w, page_h,
            column_x_edges=col_edges,
            band_y_edges=band_edges_list,
        )
        layers["uncovered_regions"] = uncovered

        # Stage-2 snap: now uncovered is in layers, snap all four types
        # together so the dedup pass sees the complete set.
        snapped = snap_obstacles_to_grid(layers)
        layers["snapped_display_ads"] = snapped.get("display_ads", [])
        layers["snapped_photos"] = snapped.get("photos", [])
        layers["snapped_articles"] = snapped.get("articles", [])
        layers["snapped_uncovered"] = snapped.get("uncovered_regions", [])

        # Post-snap resolution: reclassify false ads and absorb/promote
        # uncovered (orange) regions to get accurate item boundaries.
        # Pass a 100-DPI greyscale render so resolution can do pixel-
        # based ink-pattern checks instead of relying on OCR span
        # counts (OCR is unreliable on older issues).
        zoom_res = 100.0 / 72.0
        pix_res = page.get_pixmap(matrix=fitz.Matrix(zoom_res, zoom_res),
                                   colorspace=fitz.csGRAY, alpha=False)
        page_image = np.frombuffer(pix_res.samples, dtype=np.uint8) \
            .reshape(pix_res.height, pix_res.width)
        from post1980.resolution import resolve_obstacles
        resolved = resolve_obstacles(layers, spans=spans, body=body,
                                      page_image=page_image,
                                      image_zoom=zoom_res)
        layers["resolved_items"]       = resolved["items"]
        layers["resolved_articles"]    = resolved["resolved_articles"]
        layers["resolved_display_ads"] = resolved["resolved_display_ads"]
        layers["resolved_photos"]      = resolved["resolved_photos"]
        layers["resolved_pull_quotes"] = resolved["resolved_pull_quotes"]
        layers["resolved_uncovered"]   = resolved.get("resolved_uncovered", [])
        layers["resolved_dropped"]     = resolved["resolved_dropped"]
        layers["resolution_log"]       = resolved["resolution_log"]

    return doc, page, layers


def render_overlay(page, layers, out_path, dpi=100):
    """Composite overlay PNG showing every detected layer."""
    from PIL import Image, ImageDraw

    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    base_path = out_path.replace("_overlay.png", "_base.png")
    pix.save(base_path)

    img = Image.open(base_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def to_px(bbox):
        x0, y0, x1, y1 = bbox
        return (x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom)

    pc = layers["page_class"]
    if pc == "image_only":
        draw.rectangle([0, 0, img.width, img.height],
                       fill=COL["image_only"])
    elif pc == "classifieds":
        draw.rectangle([0, 0, img.width, img.height],
                       fill=COL["classifieds"])

    # Masthead band
    mast_y = layers["masthead_bottom"]
    if mast_y > 0:
        draw.rectangle([0, 0, img.width, mast_y * zoom],
                       fill=COL["masthead"])

    # Whitespace bands (faint stripes across the page)
    for band_y0, band_y1 in layers.get("whitespace_bands", []):
        draw.rectangle(
            [0, band_y0 * zoom, img.width, band_y1 * zoom],
            fill=(255, 220, 100, 40),
        )

    # Photos (drawn first, behind articles)
    for p in layers["photos"]:
        draw.rectangle(list(to_px(p)),
                       outline=COL["photo"], width=3)

    # Display ads
    for a in layers["display_ads"]:
        draw.rectangle(list(to_px(a)),
                       outline=COL["display_ad"], width=4)

    # Articles, then their headlines on top
    for art in layers["articles"]:
        draw.rectangle(list(to_px(art["bbox"])),
                       outline=COL["article"], width=4)
    for art in layers["articles"]:
        draw.rectangle(list(to_px(art["headline"]["bbox"])),
                       outline=COL["headline"], width=2)

    # Pull quotes
    for pq in layers["pull_quotes"]:
        x0, y0, x1, y1 = pq[:4]
        draw.rectangle(list(to_px((x0, y0, x1, y1))),
                       outline=COL["pull_quote"], width=3)

    # Uncovered ink-bearing regions (drawn last so they sit on top
    # and are unambiguous — these are "missed content here").
    for u in layers.get("uncovered_regions", []):
        draw.rectangle(list(to_px(u)),
                       outline=COL["uncovered"], width=5)

    # Column-grid measurement regions: bracket markers at the
    # region's actual x0/x1 (which may be page edges, OR may be
    # mid-page if the band was split into clean x-zones around ads).
    BAND_BRACKET_PX = 16
    BAND_LINE_W = 3
    band_outline = (60, 120, 200, 230)
    band_spine   = (60, 120, 200, 180)
    regions = layers.get("column_grid_bands", [])
    profiles = layers.get("column_grid_profiles", [])
    for idx, region in enumerate(regions):
        if len(region) == 2:
            y0, y1 = region
            x0_pt, x1_pt = 0, page_w
        else:
            y0, y1, x0_pt, x1_pt = region
        y0p = y0 * zoom
        y1p = y1 * zoom
        x0p = x0_pt * zoom
        x1p = x1_pt * zoom
        # Bracket markers at region edges
        draw.line([(x0p + 2, y0p), (x0p + BAND_BRACKET_PX, y0p)],
                  fill=band_outline, width=BAND_LINE_W)
        draw.line([(x0p + 2, y1p), (x0p + BAND_BRACKET_PX, y1p)],
                  fill=band_outline, width=BAND_LINE_W)
        draw.line([(x0p + 2, y0p), (x0p + 2, y1p)],
                  fill=band_spine, width=BAND_LINE_W)
        draw.line([(x1p - BAND_BRACKET_PX, y0p), (x1p - 2, y0p)],
                  fill=band_outline, width=BAND_LINE_W)
        draw.line([(x1p - BAND_BRACKET_PX, y1p), (x1p - 2, y1p)],
                  fill=band_outline, width=BAND_LINE_W)
        draw.line([(x1p - 2, y0p), (x1p - 2, y1p)],
                  fill=band_spine, width=BAND_LINE_W)

        # Plot the column-mean profile that drove valley detection
        # for this region: a "skyline" curve where high values mean
        # text columns and dips mean gutters.
        if idx < len(profiles) and profiles[idx]:
            prof = profiles[idx]
            band_h_px = y1p - y0p
            # Profile y-mapping: value 0 → bottom of band; full-scale
            # (≈80 on the inverted-greyscale col-mean, matching strong
            # body text) → top. The profile is in 0..255 range, so we
            # scale by an empirical "full" value rather than 255.
            PROFILE_FULL = 80.0
            def map_val(v):
                v = max(0.0, min(PROFILE_FULL, float(v))) / PROFILE_FULL
                return y1p - v * band_h_px
            # Polyline points
            pts = [(x_pt * zoom, map_val(v)) for (x_pt, v) in prof]
            # Fill area under the curve with very light opacity so the
            # chart shape is perceptible against busy page content. Use
            # a closed polygon down to the band's bottom edge.
            if len(pts) >= 2:
                poly_pts = list(pts) + [(pts[-1][0], y1p), (pts[0][0], y1p)]
                draw.polygon(poly_pts, fill=(40, 40, 60, 50))
            # Curve: dark slate (line on top of the fill)
            curve_col = (40, 40, 60, 230)
            for k in range(len(pts) - 1):
                draw.line([pts[k], pts[k + 1]], fill=curve_col, width=2)
            # Threshold reference lines:
            from post1980.column_grid import (
                GRID_VALLEY_MAX, GRID_CONTENT_MIN,
            )
            # Faint horizontal at valley threshold (below = gutter)
            vt_y = map_val(GRID_VALLEY_MAX)
            draw.line([(x0p + 4, vt_y), (x1p - 4, vt_y)],
                      fill=(160, 80, 80, 130), width=1)
            # Faint horizontal at content threshold (above = body text)
            ct_y = map_val(GRID_CONTENT_MIN)
            draw.line([(x0p + 4, ct_y), (x1p - 4, ct_y)],
                      fill=(80, 130, 80, 130), width=1)

    # Fundamental grid — cobalt-blue dashed lines for all four kinds.
    # Drawn last so they sit above everything else and are the
    # visual key for the page's structural framework. Per user
    # feedback 2026-05-18, racing green was imperceptible against
    # busy text — strong blue + bold strokes are far more legible.
    gl = COL["racing_green"]
    page_h_pt = layers.get("page_size", [0, 0])[1]

    # Coarse first-pass axes — thin dotted reference lines drawn BEFORE
    # the refined grid so the refined lines sit on top. These show the
    # full-page candidate axes that act as cross-corroboration.
    for cx_pt in layers.get("coarse_axes", []) or []:
        x_px = cx_pt * zoom
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          gl, width=2, dash=4, gap=10)

    # Column centrelines — verticals. Width carries confidence signal.
    for g in layers.get("column_grid", []):
        x_px = g["x_pt"] * zoom
        conf = g.get("confidence", "low")
        w = 5 if conf == "high" else (4 if conf == "medium" else 2)
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          gl, width=w)

    # Text-area left/right verticals (page-profile bounds of the
    # printed content rectangle — distinguishes content from margin)
    ta_left = layers.get("text_area_left_pt")
    ta_right = layers.get("text_area_right_pt")
    if ta_left is not None:
        _draw_dashed_line(draw, (ta_left * zoom, 0),
                          (ta_left * zoom, img.height), gl, width=5)
    if ta_right is not None:
        _draw_dashed_line(draw, (ta_right * zoom, 0),
                          (ta_right * zoom, img.height), gl, width=5)

    # Masthead bottom — horizontal line across the page
    mast_y = layers.get("masthead_bottom", 0.0)
    if mast_y > 0:
        my = mast_y * zoom
        _draw_dashed_line(draw, (0, my), (img.width, my), gl, width=5)

    # Whitespace-band horizontals: interior bands → draw at centreline;
    # bands near the bottom of the page → draw at top edge (so the
    # line is at the boundary BEFORE the bottom whitespace, not in
    # the middle of it).
    if page_h_pt:
        bottom_zone_y = page_h_pt - 200   # "near bottom" cutoff
        for (b_y0, b_y1) in layers.get("whitespace_bands", []):
            if b_y0 >= bottom_zone_y:
                line_y = b_y0           # top edge of bottom band
            else:
                line_y = (b_y0 + b_y1) / 2.0   # centreline of interior band
            yp = line_y * zoom
            _draw_dashed_line(draw, (0, yp), (img.width, yp),
                              gl, width=4)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(out_path)


def issue_date_from_path(pdf_path):
    """'/.../1985-02-13-01.pdf' -> ('1985-02-13', 1)."""
    m = re.search(r'(\d{4}-\d{2}-\d{2})-(\d{2})\.pdf$', pdf_path)
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_paths", nargs="+")
    ap.add_argument("--out-root", default="columns_modular")
    ap.add_argument("--dpi", type=int, default=100)
    args = ap.parse_args()

    for pdf in args.pdf_paths:
        if not os.path.exists(pdf):
            print(f"missing: {pdf}", file=sys.stderr)
            continue
        issue, pageno = issue_date_from_path(pdf)
        if issue is None:
            print(f"unparseable filename: {pdf}", file=sys.stderr)
            continue

        page_dir = os.path.join(args.out_root, issue, f"p{pageno}")
        qa_dir = os.path.join(page_dir, "qa")
        os.makedirs(qa_dir, exist_ok=True)

        doc, page, layers = cut_page(pdf)

        overlay_path = os.path.join(qa_dir, f"p{pageno}_overlay.png")
        render_overlay(page, layers, overlay_path, dpi=args.dpi)

        layers_path = os.path.join(qa_dir, f"p{pageno}_layers.json")
        with open(layers_path, "w") as f:
            json.dump(layers, f, indent=2, default=str)

        n_art = len(layers["articles"])
        n_pq = len(layers["pull_quotes"])
        n_ad = len(layers["display_ads"])
        n_ph = len(layers["photos"])
        print(f"{issue} p{pageno}: class={layers['page_class']} "
              f"articles={n_art} pull_quotes={n_pq} "
              f"ads={n_ad} photos={n_ph}  →  {overlay_path}")


if __name__ == "__main__":
    main()
