"""Preview machine — rapid configurable previews from cutter run output.

The cutter writes one `*_layers.json` + one `*_base.png` per page into
its `--out-root/<issue>/p<n>/qa/` directory. This module re-renders
those layers in arbitrary combinations and assembles HTML pages that
compare them.

Usage from a script:

    from preview_machine import PreviewBuilder
    pb = PreviewBuilder(
        run_dirs={"current": "/tmp/post1980_phase2r_run"},
        out_dir="preview/post1980_phase2r",
        title="Phase 2r: coarse vs refined",
    )
    # Each view = (label, list of layer names). Layer names match the
    # LAYER_RENDERERS dict below; "all" expands to every renderer.
    pb.add_view("Coarse first-pass", ["page_frame", "coarse_axes"])
    pb.add_view("Refined", ["page_frame", "column_grid", "column_grid_bands",
                             "column_grid_profiles"])
    pb.add_view("Everything", ["all"])
    pb.add_pages(["2007-02-13/p1", "2000-02-16/p1", "1995-02-15/p1"])
    pb.build()

A view's layer list is rendered on top of the page's base PNG.
Multiple views per page are laid out side-by-side in the HTML.

CLI mode:

    python3 tools/preview_machine.py \
        --run-dir /tmp/post1980_phase2r_run \
        --out preview/post1980_phase2r \
        --title "Phase 2r" \
        --views "coarse=page_frame,coarse_axes refined=page_frame,column_grid"

`--views` takes a space-separated list of `<label>=<layer1>,<layer2>...`.
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# Add tools/ to sys.path so we can reuse the post1980 dashed-line helper
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from post1980.cut_page import _draw_dashed_line, COL


# Default render DPI matches what cut_page uses for its base PNG.
RENDER_DPI = 100
ZOOM = RENDER_DPI / 72.0

# 5th-percentile (near-white floor — candidate gutter values) = bright orange
PCT_LOW_DOT = (255, 130, 0, 255)
# 95th-percentile (peak text values) = lilac
PCT_HIGH_DOT = (190, 145, 220, 255)


def _draw_dot_dash_line(draw, p0, p1, fill, width=2,
                        dash=12, dot=3, gap=6):
    """Vertical/diagonal dot-dash line: pattern is DASH, gap, DOT, gap,
    repeated. Used to mark lower-confidence refined boundaries where
    the line position is NOT corroborated by orange-dot (5th-percentile)
    clusters across multiple quadrants."""
    x0, y0 = p0; x1, y1 = p1
    dx = x1 - x0; dy = y1 - y0
    length = (dx * dx + dy * dy) ** 0.5
    if length == 0:
        return
    pattern_len = dash + gap + dot + gap
    n = int(length / pattern_len) + 1
    for i in range(n):
        base = i * pattern_len
        # Dash segment
        t0 = base / length
        t1 = min((base + dash) / length, 1.0)
        if t0 < 1.0:
            draw.line([(x0 + dx * t0, y0 + dy * t0),
                       (x0 + dx * t1, y0 + dy * t1)],
                      fill=fill, width=width)
        # Dot segment
        t0 = (base + dash + gap) / length
        t1 = min((base + dash + gap + dot) / length, 1.0)
        if t0 < 1.0:
            draw.line([(x0 + dx * t0, y0 + dy * t0),
                       (x0 + dx * t1, y0 + dy * t1)],
                      fill=fill, width=width)


def _orange_dot_corroboration(boundary_x_pt, layers, low_pct=5):
    """Mathematical version of "this line passes through a cluster of
    orange dots across several horizontal places". Counts how many
    L1+L2 quadrants have a 5th-percentile (near-white) profile value
    at or very near the boundary x. Each quadrant contributes at most
    one to the count.

    Inner x-bounds (margin-excluded, same as the dot rendering) are
    used both to compute the percentile threshold and to gate the
    sample search — a boundary outside the inner range can't be
    corroborated."""
    x_lo, x_hi = _inner_x_bounds(layers)
    if x_lo is not None and boundary_x_pt < x_lo:
        return 0
    if x_hi is not None and boundary_x_pt > x_hi:
        return 0
    count = 0
    for level_key in ("coarse_quadrants_l1", "coarse_quadrants_l2"):
        for region in (layers.get(level_key) or []):
            # Only quadrants covering this boundary's x can vote
            if not (region["x0_pt"] <= boundary_x_pt <= region["x1_pt"]):
                continue
            prof = region.get("profile") or []
            if len(prof) < 5:
                continue
            inner_vals = [v for (x, v) in prof
                          if (x_lo is None or x >= x_lo)
                          and (x_hi is None or x <= x_hi)]
            if len(inner_vals) < 2:
                continue
            thr = np.percentile(np.array(inner_vals), low_pct)
            # Find sample closest to boundary_x_pt
            closest_v = min(prof, key=lambda p: abs(p[0] - boundary_x_pt))[1]
            if closest_v <= thr:
                count += 1
    return count


def _inner_x_bounds(layers, into_column_pt=30.0):
    """Inner x range for percentile assessment — shifted inward from
    the text-area edges by `into_column_pt`. Per user direction
    2026-05-18, the page margins are excluded so the 5th-percentile
    floor reflects actual GUTTER values, not the all-white paper
    margin (which would dominate the bottom percentile and drown out
    real gutters). Cutting into the first/last column slightly is
    acceptable — we're looking for inner gutters, not the page edge.
    """
    ta_l = layers.get("text_area_left_pt")
    ta_r = layers.get("text_area_right_pt")
    if ta_l is None or ta_r is None:
        return None, None
    return float(ta_l) + into_column_pt, float(ta_r) - into_column_pt


def _percentile_dots(draw, prof_xy_values, plot_xy_pts,
                      low_pct=5, high_pct=95, radius=5,
                      x_lo_pt=None, x_hi_pt=None):
    """Plot bright-orange dots at points whose RAW value falls in the
    bottom `low_pct` percentile (near-white) or top `high_pct` percentile
    (darkest) of the profile.

    When `x_lo_pt`/`x_hi_pt` are given, ONLY points with x_pt inside
    those bounds participate in the percentile calculation AND only
    those qualifying points get dots — points in the page margin are
    ignored entirely. This excludes the all-white paper margin from
    polluting the 5th-percentile floor.
    """
    if not prof_xy_values or len(prof_xy_values) != len(plot_xy_pts):
        return
    inner_mask = [
        ((x_lo_pt is None or x_pt >= x_lo_pt) and
         (x_hi_pt is None or x_pt <= x_hi_pt))
        for (x_pt, _v) in prof_xy_values
    ]
    inner_vals = np.array(
        [v for (m, (_, v)) in zip(inner_mask, prof_xy_values) if m])
    if inner_vals.size < 2:
        return
    lo = np.percentile(inner_vals, low_pct)
    hi = np.percentile(inner_vals, high_pct)
    for ok, (_, v), (x, y) in zip(inner_mask, prof_xy_values, plot_xy_pts):
        if not ok:
            continue
        if v <= lo:
            draw.ellipse([(x - radius, y - radius),
                          (x + radius, y + radius)],
                         fill=PCT_LOW_DOT)
        elif v >= hi:
            draw.ellipse([(x - radius, y - radius),
                          (x + radius, y + radius)],
                         fill=PCT_HIGH_DOT)


# ----- Layer renderers --------------------------------------------------
# Each renderer takes (draw, img, layers, zoom) and draws onto an
# RGBA overlay. Layers is the parsed *_layers.json dict.

def _r_masthead_fill(draw, img, layers, zoom):
    """Grey fill from y=0 to masthead_bottom, indicating the masthead band."""
    mast_y = layers.get("masthead_bottom", 0.0)
    if mast_y <= 0:
        return
    draw.rectangle([(0, 0), (img.width, mast_y * zoom)],
                   fill=COL["masthead"])


def _r_masthead_line(draw, img, layers, zoom):
    mast_y = layers.get("masthead_bottom", 0.0)
    if mast_y <= 0:
        return
    my = mast_y * zoom
    _draw_dashed_line(draw, (0, my), (img.width, my),
                      COL["racing_green"], width=3, dash=14, gap=8)


def _r_text_area(draw, img, layers, zoom):
    """Vertical dashed lines at the page's text-area left/right edges."""
    ta_l = layers.get("text_area_left_pt")
    ta_r = layers.get("text_area_right_pt")
    rg = COL["racing_green"]
    if ta_l is not None:
        _draw_dashed_line(draw, (ta_l * zoom, 0),
                          (ta_l * zoom, img.height), rg, width=3, dash=14, gap=8)
    if ta_r is not None:
        _draw_dashed_line(draw, (ta_r * zoom, 0),
                          (ta_r * zoom, img.height), rg, width=3, dash=14, gap=8)


def _r_whitespace_bands(draw, img, layers, zoom):
    page_h_pt = layers.get("page_size", [0, 0])[1]
    bottom_zone_y = (page_h_pt or 0) - 200
    rg = COL["racing_green"]
    for (b_y0, b_y1) in layers.get("whitespace_bands") or []:
        # Fill (very light yellow tint)
        draw.rectangle([(0, b_y0 * zoom), (img.width, b_y1 * zoom)],
                       fill=(255, 240, 130, 50))
        line_y = b_y0 if b_y0 >= bottom_zone_y else (b_y0 + b_y1) / 2.0
        yp = line_y * zoom
        _draw_dashed_line(draw, (0, yp), (img.width, yp),
                          rg, width=3, dash=14, gap=8)


def _r_display_ads(draw, img, layers, zoom):
    col = COL["display_ad"]
    for (x0, y0, x1, y1) in layers.get("display_ads") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=4)


def _r_photos(draw, img, layers, zoom):
    col = COL["photo"]
    for (x0, y0, x1, y1) in layers.get("photos") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=4)


def _r_pull_quotes(draw, img, layers, zoom):
    col = COL["pull_quote"]
    for h in layers.get("pull_quotes") or []:
        x0, y0, x1, y1 = h[0], h[1], h[2], h[3]
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=3)


def _r_articles(draw, img, layers, zoom):
    col = COL["article"]
    for a in layers.get("articles") or []:
        x0, y0, x1, y1 = a["bbox"] if isinstance(a, dict) else a[:4]
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=3)


def _r_uncovered(draw, img, layers, zoom):
    col = COL["uncovered"]
    for (x0, y0, x1, y1) in layers.get("uncovered_regions") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=3)


def _r_coarse_axes(draw, img, layers, zoom):
    """Coarse first-pass candidate axes — dotted vertical reference lines."""
    rg = COL["racing_green"]
    for cx_pt in layers.get("coarse_axes") or []:
        x_px = cx_pt * zoom
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          rg, width=3, dash=4, gap=10)


def _r_column_grid(draw, img, layers, zoom):
    """Refined column-grid lines — always full dashed at their confidence
    width. The dot-dash style is reserved for the coarse pass per user
    direction 2026-05-18."""
    rg = COL["racing_green"]
    for g in layers.get("column_grid") or []:
        x_px = g["x_pt"] * zoom
        conf = g.get("confidence", "low")
        w = 5 if conf == "high" else (4 if conf == "medium" else 2)
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          rg, width=w, dash=14, gap=8)


def _r_pitch_grid(draw, img, layers, zoom):
    """Projected column grid from the fitted pitch model.
    Confirmed gutters (cluster within ¼ pitch) get a full dashed line;
    projected-only gutters (no nearby cluster) get a dot-dash line."""
    pg = layers.get("pitch_grid")
    if not pg:
        return
    rg = COL["racing_green"]
    for g in pg.get("gutters") or []:
        x_px = g["x_pt"] * zoom
        if g.get("confirmed"):
            _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                              rg, width=5, dash=14, gap=8)
        else:
            _draw_dot_dash_line(draw, (x_px, 0), (x_px, img.height),
                                rg, width=3)


def _r_column_grid_bands(draw, img, layers, zoom):
    """Measurement region brackets [...]."""
    BAND_LINE_W = 3
    BAND_BRACKET_PX = 18
    band_outline = (50, 100, 180, 220)
    band_spine = (50, 100, 180, 160)
    for (y0p, y1p, x0p_pt, x1p_pt) in layers.get("column_grid_bands") or []:
        y0p *= zoom; y1p *= zoom
        x0p = (x0p_pt or 0) * zoom
        x1p = (x1p_pt or 0) * zoom
        for px, side in ((x0p, +1), (x1p, -1)):
            draw.line([(px + side * 2, y0p),
                       (px + side * (2 + BAND_BRACKET_PX), y0p)],
                      fill=band_outline, width=BAND_LINE_W)
            draw.line([(px + side * 2, y1p),
                       (px + side * (2 + BAND_BRACKET_PX), y1p)],
                      fill=band_outline, width=BAND_LINE_W)
            draw.line([(px + side * 2, y0p), (px + side * 2, y1p)],
                      fill=band_spine, width=BAND_LINE_W)


def _r_column_grid_profiles(draw, img, layers, zoom):
    """Filled col-mean profile curves inside each measurement band."""
    profiles = layers.get("column_grid_profiles") or []
    regions = layers.get("column_grid_bands") or []
    PROFILE_FULL = 80.0
    for idx, prof in enumerate(profiles):
        if idx >= len(regions) or not prof:
            continue
        (y0p_pt, y1p_pt, _, _) = regions[idx]
        y0p = y0p_pt * zoom; y1p = y1p_pt * zoom
        band_h_px = y1p - y0p

        def map_val(v):
            v = max(0.0, min(PROFILE_FULL, float(v))) / PROFILE_FULL
            return y1p - v * band_h_px

        pts = [(x_pt * zoom, map_val(v)) for (x_pt, v) in prof]
        if len(pts) >= 2:
            poly_pts = list(pts) + [(pts[-1][0], y1p), (pts[0][0], y1p)]
            draw.polygon(poly_pts, fill=(40, 40, 60, 50))
        curve_col = (40, 40, 60, 230)
        for k in range(len(pts) - 1):
            draw.line([pts[k], pts[k + 1]], fill=curve_col, width=2)
        # Percentile dots are a coarse-pass diagnostic only — refined
        # bands have their own valley-scoring logic.


def _draw_region_chart(draw, img, region, zoom, profile_full=80.0,
                       inner_bounds=(None, None)):
    """Shared helper: draw one region's chart (fill+curve) + its candidate
    axes, clipped to the region's 2D footprint. Used by both L1 and L2."""
    x0_pt = region["x0_pt"]; y0_pt = region["y0_pt"]
    x1_pt = region["x1_pt"]; y1_pt = region["y1_pt"]
    prof = region.get("profile") or []
    cands = region.get("candidates") or []
    if not prof or y1_pt <= y0_pt or x1_pt <= x0_pt:
        return
    x0p = x0_pt * zoom; x1p = x1_pt * zoom
    y0p = y0_pt * zoom; y1p = y1_pt * zoom
    region_h_px = y1p - y0p
    chart_h_px = min(region_h_px - 4, 380)
    chart_y0_px = y0p + 2
    chart_y1_px = chart_y0_px + chart_h_px

    def map_val(v):
        v = max(0.0, min(profile_full, float(v))) / profile_full
        return chart_y1_px - v * chart_h_px

    # Translucent strip background
    draw.rectangle([(x0p, chart_y0_px), (x1p, chart_y1_px)],
                   fill=(255, 255, 255, 165))
    # Per-region candidates — dot-dash style to signal "coarse-pass,
    # lower confidence". Refined column-grid lines (drawn separately)
    # use the full dashed style.
    rg = COL["racing_green"]
    for cx_pt in cands:
        x_px = cx_pt * zoom
        _draw_dot_dash_line(draw, (x_px, chart_y0_px),
                            (x_px, chart_y1_px),
                            rg, width=3)
    # Profile fill + line
    pts = [(x_pt * zoom, map_val(v)) for (x_pt, v) in prof]
    if len(pts) >= 2:
        poly_pts = list(pts) + [(pts[-1][0], chart_y1_px),
                                  (pts[0][0], chart_y1_px)]
        draw.polygon(poly_pts, fill=(40, 40, 60, 70))
    curve_col = (40, 40, 60, 230)
    for k in range(len(pts) - 1):
        draw.line([pts[k], pts[k + 1]], fill=curve_col, width=2)
    # Percentile dots — 5th (near-white floor) and 95th (peak text).
    # Margin-aware: bounds passed from the caller exclude page margins.
    x_lo, x_hi = inner_bounds
    _percentile_dots(draw, prof, pts, x_lo_pt=x_lo, x_hi_pt=x_hi)
    # Region boundary (right + bottom edges)
    draw.line([(x1p, chart_y0_px), (x1p, chart_y1_px)],
              fill=(150, 150, 150, 220), width=2)
    draw.line([(x0p, chart_y1_px), (x1p, chart_y1_px)],
              fill=(150, 150, 150, 220), width=2)


def _r_coarse_quadrants_l1(draw, img, layers, zoom):
    """Level-1 quadrants — 2×2 grid below masthead. Each region has its
    own independent coarse pass and its own candidate axes."""
    bounds = _inner_x_bounds(layers)
    for region in layers.get("coarse_quadrants_l1") or []:
        _draw_region_chart(draw, img, region, zoom, inner_bounds=bounds)


def _r_coarse_quadrants_l2(draw, img, layers, zoom):
    """Level-2 sub-quadrants — 4×4 grid below masthead. Each region has
    its own independent coarse pass and its own candidate axes."""
    bounds = _inner_x_bounds(layers)
    for region in layers.get("coarse_quadrants_l2") or []:
        _draw_region_chart(draw, img, region, zoom, inner_bounds=bounds)


def _r_coarse_segments_4x6(draw, img, layers, zoom):
    """4 across × 6 down segments. Each region has its own independent
    coarse pass and its own candidate axes."""
    bounds = _inner_x_bounds(layers)
    for region in layers.get("coarse_segments_4x6") or []:
        _draw_region_chart(draw, img, region, zoom, inner_bounds=bounds)


def _r_segment_crop_marks(draw, img, layers, zoom):
    """L-shaped crop marks at the corners of every 4×6 segment, drawn
    out at the page margins so you can see segment boundaries without
    overlay clutter inside the content."""
    segments = layers.get("coarse_segments_4x6") or []
    if not segments:
        return
    mark_col = (40, 40, 60, 220)
    mark_len = 16
    mark_w = 2
    # Collect unique x and y boundaries from segment edges.
    xs = sorted({float(s["x0_pt"]) for s in segments}
                 | {float(s["x1_pt"]) for s in segments})
    ys = sorted({float(s["y0_pt"]) for s in segments}
                 | {float(s["y1_pt"]) for s in segments})
    if not xs or not ys: return
    # Vertical crop ticks at the top and bottom page margins.
    for x_pt in xs:
        x_px = x_pt * zoom
        draw.line([(x_px, 0), (x_px, mark_len)],
                  fill=mark_col, width=mark_w)
        draw.line([(x_px, img.height - mark_len), (x_px, img.height)],
                  fill=mark_col, width=mark_w)
    # Horizontal crop ticks at the left and right page margins.
    for y_pt in ys:
        y_px = y_pt * zoom
        draw.line([(0, y_px), (mark_len, y_px)],
                  fill=mark_col, width=mark_w)
        draw.line([(img.width - mark_len, y_px), (img.width, y_px)],
                  fill=mark_col, width=mark_w)


def _r_snapped_display_ads(draw, img, layers, zoom):
    """Snapped ad bboxes — drawn in the same red as display_ads but
    with a heavier stroke so the snap result is visually distinct."""
    col = COL["display_ad"]
    for (x0, y0, x1, y1) in layers.get("snapped_display_ads") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=6)


def _r_snapped_photos(draw, img, layers, zoom):
    """Snapped photo bboxes — same dark green as photos, heavier stroke."""
    col = COL["photo"]
    for (x0, y0, x1, y1) in layers.get("snapped_photos") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=6)


def _r_snapped_articles(draw, img, layers, zoom):
    """Snapped article bboxes — same article-blue as articles, heavier stroke."""
    col = COL["article"]
    for (x0, y0, x1, y1) in layers.get("snapped_articles") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=4)


def _r_snapped_uncovered(draw, img, layers, zoom):
    """Snapped uncovered/orange boxes — candidate regions for downstream
    article-detection refinements."""
    col = COL["uncovered"]
    for (x0, y0, x1, y1) in layers.get("snapped_uncovered") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=6)


def _r_resolved_uncovered(draw, img, layers, zoom):
    """Resolved uncovered/orange boxes — orange that survived
    resolution (didn't get merged or promoted), still needs handling."""
    col = COL["uncovered"]
    for (x0, y0, x1, y1) in layers.get("resolved_uncovered") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=6)


def _r_closed_rectangles(draw, img, layers, zoom):
    """Detected closed rectangles — drawn in cyan to distinguish from
    article/ad/photo bboxes. Border-thickness shown by stroke width."""
    col = (0, 180, 220, 220)
    for r in layers.get("closed_rectangles") or []:
        x0 = r["x0_pt"] * zoom; y0 = r["y0_pt"] * zoom
        x1 = r["x1_pt"] * zoom; y1 = r["y1_pt"] * zoom
        thickness_pt = r.get("border_thickness_pt", 1.0)
        # Draw with thickness reflecting the detected border
        stroke = max(2, min(8, int(round(thickness_pt * 1.5))))
        draw.rectangle([(x0, y0), (x1, y1)], outline=col, width=stroke)


def _r_horizontal_rules(draw, img, layers, zoom):
    """Detected horizontal ink rules — solid magenta line at the rule's
    y across its x-extent. Used as a vertical-merge barrier."""
    col = (180, 30, 130, 230)
    for r in layers.get("horizontal_rules") or []:
        y = r["y_pt"] * zoom
        draw.line([(r["x0_pt"] * zoom, y), (r["x1_pt"] * zoom, y)],
                  fill=col, width=3)


def _r_pixel_headlines(draw, img, layers, zoom):
    """Pixel-detected headline runs — magenta outline to distinguish
    from blue article boxes."""
    col = (200, 30, 150, 220)
    for r in layers.get("pixel_headline_runs") or []:
        draw.rectangle([(r["x0"] * zoom, r["y0"] * zoom),
                         (r["x1"] * zoom, r["y1"] * zoom)],
                       outline=col, width=3)


def _r_resolved_articles(draw, img, layers, zoom):
    """Resolved articles — thick solid blue regardless of titled/untitled.
    Per user direction 2026-05-19: in the final output every article
    looks the same; no need to distinguish synthesised-from-orange."""
    col = COL["article"]
    for (x0, y0, x1, y1) in layers.get("resolved_articles") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=6)


def _r_resolved_display_ads(draw, img, layers, zoom):
    col = COL["display_ad"]
    for (x0, y0, x1, y1) in layers.get("resolved_display_ads") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=8)


def _r_resolved_photos(draw, img, layers, zoom):
    col = COL["photo"]
    for (x0, y0, x1, y1) in layers.get("resolved_photos") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=8)


def _r_resolved_pull_quotes(draw, img, layers, zoom):
    col = COL["pull_quote"]
    for (x0, y0, x1, y1) in layers.get("resolved_pull_quotes") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=5)


def _r_resolved_dropped(draw, img, layers, zoom):
    """Dropped items — grey rectangle with diagonal × through it."""
    col = (130, 130, 130, 160)
    for (x0, y0, x1, y1) in layers.get("resolved_dropped") or []:
        draw.rectangle([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                       outline=col, width=2)
        # Diagonals
        draw.line([(x0 * zoom, y0 * zoom), (x1 * zoom, y1 * zoom)],
                  fill=col, width=2)
        draw.line([(x0 * zoom, y1 * zoom), (x1 * zoom, y0 * zoom)],
                  fill=col, width=2)


def _r_resolution_tags(draw, img, layers, zoom):
    """Small text labels at top-left of each resolved item showing the
    rule that placed it (B1/B2/C1/C2/C3/C4/D)."""
    from PIL import ImageFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except Exception:
        font = ImageFont.load_default()
    for it in layers.get("resolved_items") or []:
        rule = (it.get("evidence") or {}).get("rule", "")
        if not rule:
            continue
        # Take just the rule prefix (e.g. "B1" from "B1_ad_to_photo_...")
        tag = rule.split("_", 1)[0]
        if not tag:
            continue
        b = it["bbox"]
        x = b[0] * zoom + 4
        y = b[1] * zoom + 4
        # Background pill for legibility
        bg = (255, 255, 255, 220)
        draw.rectangle([(x - 2, y - 2), (x + 22, y + 14)], fill=bg)
        draw.text((x, y), tag, fill=(20, 20, 40, 255), font=font)


def _r_structural_grid(draw, img, layers, zoom):
    """The full structural grid we're snapping to: vertical gutters
    (from scored_grid), horizontal whitespace bands, text-area edges,
    masthead bottom. Drawn faint so it sits behind the obstacle
    boxes."""
    rg = COL["racing_green"]
    sg = layers.get("scored_grid") or {}
    # Vertical gutters
    for g in sg.get("estimated_gutters") or []:
        x_px = g["x_pt"] * zoom
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          rg, width=2, dash=10, gap=6)
    # Text-area edges
    for edge_key in ("text_area_left_pt", "text_area_right_pt"):
        v = layers.get(edge_key)
        if v is not None:
            x_px = v * zoom
            _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                              rg, width=2, dash=10, gap=6)
    # Horizontal: masthead bottom + each whitespace band edge
    mast_y = layers.get("masthead_bottom") or 0.0
    if mast_y > 0:
        y_px = mast_y * zoom
        _draw_dashed_line(draw, (0, y_px), (img.width, y_px),
                          rg, width=2, dash=10, gap=6)
    for (b_y0, b_y1) in layers.get("whitespace_bands") or []:
        for y_pt in (b_y0, b_y1):
            y_px = y_pt * zoom
            _draw_dashed_line(draw, (0, y_px), (img.width, y_px),
                              rg, width=2, dash=10, gap=6)


def _r_estimated_gutters(draw, img, layers, zoom):
    """Estimated gutters from the scored center-out analysis on 4×6
    segments. Full dashed line at racing green at width proportional
    to score (capped)."""
    sg = layers.get("scored_grid")
    if not sg:
        return
    rg = COL["racing_green"]
    for g in sg.get("estimated_gutters") or []:
        x_px = g["x_pt"] * zoom
        score = g.get("score", 0)
        w = 3 + min(int(score / 5), 4)  # scale line width with score
        _draw_dashed_line(draw, (x_px, 0), (x_px, img.height),
                          rg, width=w, dash=14, gap=8)


def _r_coarse_slabs(draw, img, layers, zoom):
    """Per-quarter col-mean profile charts + per-quarter detected axes
    from the coarse pass. Each quarter occupies its own y-range; each
    has its own profile AND its own candidate gutter axes (independent
    per quarter). The candidate axes for one quarter are drawn ONLY
    within that quarter's y-range — so you can see which quarters
    agree, which disagree, and which produce no signal at all."""
    slabs = layers.get("coarse_slabs") or []
    PROFILE_FULL = 80.0
    rg = COL["racing_green"]
    for slab in slabs:
        y0_pt = slab["y0_pt"]; y1_pt = slab["y1_pt"]
        prof = slab["profile"]
        cands = slab.get("candidates") or []
        if not prof or y1_pt <= y0_pt:
            continue
        y0p = y0_pt * zoom; y1p = y1_pt * zoom
        slab_h_px = y1p - y0p
        chart_h_px = min(slab_h_px - 4, 380)
        chart_y0_px = y0p + 2
        chart_y1_px = chart_y0_px + chart_h_px

        def map_val(v, y1=chart_y1_px, h=chart_h_px):
            v = max(0.0, min(PROFILE_FULL, float(v))) / PROFILE_FULL
            return y1 - v * h

        # Translucent white strip background so the chart is legible
        draw.rectangle([(0, chart_y0_px), (img.width, chart_y1_px)],
                       fill=(255, 255, 255, 165))
        # Per-quarter candidate axes: drawn ONLY within this quarter
        for cx_pt in cands:
            x_px = cx_pt * zoom
            _draw_dashed_line(draw, (x_px, chart_y0_px),
                              (x_px, chart_y1_px),
                              rg, width=4, dash=12, gap=6)
        # Profile fill + line
        pts = [(x_pt * zoom, map_val(v)) for (x_pt, v) in prof]
        if len(pts) >= 2:
            poly_pts = list(pts) + [(pts[-1][0], chart_y1_px),
                                      (pts[0][0], chart_y1_px)]
            draw.polygon(poly_pts, fill=(40, 40, 60, 70))
        curve_col = (40, 40, 60, 230)
        for k in range(len(pts) - 1):
            draw.line([pts[k], pts[k + 1]], fill=curve_col, width=2)
        x_lo, x_hi = _inner_x_bounds(layers)
        _percentile_dots(draw, prof, pts, x_lo_pt=x_lo, x_hi_pt=x_hi)
        # Quarter boundary line
        draw.line([(0, chart_y1_px), (img.width, chart_y1_px)],
                  fill=(150, 150, 150, 220), width=2)


def _r_coarse_profile(draw, img, layers, zoom):
    """The coarse first-pass col-mean profile chart, drawn full page width
    in the y-range it was sampled from. Filled with light opacity + a
    curve line on top — same style as the per-band profile charts."""
    prof = layers.get("coarse_profile") or []
    y_range = layers.get("coarse_profile_y_range") or [0.0, 0.0]
    if not prof or y_range[1] <= y_range[0]:
        return
    # Plot occupies a horizontal strip just below the masthead — keep
    # it 120pt tall regardless of the actual averaging range, so the
    # chart is large enough to read without obscuring the body text.
    strip_h_pt = 120.0
    strip_y0_pt = y_range[0] + 4
    strip_y1_pt = strip_y0_pt + strip_h_pt
    y0p = strip_y0_pt * zoom
    y1p = strip_y1_pt * zoom
    strip_h_px = y1p - y0p
    # Profile values are in 0..255 inverted-darkness scale; pick a
    # full-scale value that maps the typical body-text range to ~80%
    # of the strip height (page-averaged col-means rarely exceed 60).
    PROFILE_FULL = 60.0

    def map_val(v):
        v = max(0.0, min(PROFILE_FULL, float(v))) / PROFILE_FULL
        return y1p - v * strip_h_px

    pts = [(x_pt * zoom, map_val(v)) for (x_pt, v) in prof]
    # Strip background so the chart is legible against page content
    draw.rectangle([(0, y0p), (img.width, y1p)], fill=(255, 255, 255, 180))
    if len(pts) >= 2:
        poly_pts = list(pts) + [(pts[-1][0], y1p), (pts[0][0], y1p)]
        draw.polygon(poly_pts, fill=(40, 40, 60, 80))
    curve_col = (40, 40, 60, 230)
    for k in range(len(pts) - 1):
        draw.line([pts[k], pts[k + 1]], fill=curve_col, width=2)
    x_lo, x_hi = _inner_x_bounds(layers)
    _percentile_dots(draw, prof, pts, x_lo_pt=x_lo, x_hi_pt=x_hi)


# A composite "page frame" layer pulls together the always-present
# anchors so test views don't all have to list them.
def _r_page_frame(draw, img, layers, zoom):
    _r_masthead_fill(draw, img, layers, zoom)
    _r_masthead_line(draw, img, layers, zoom)
    _r_text_area(draw, img, layers, zoom)


LAYER_RENDERERS: Dict[str, Callable] = {
    "page_frame":            _r_page_frame,
    "masthead_fill":         _r_masthead_fill,
    "masthead_line":         _r_masthead_line,
    "text_area":             _r_text_area,
    "whitespace_bands":      _r_whitespace_bands,
    "display_ads":           _r_display_ads,
    "photos":                _r_photos,
    "pull_quotes":           _r_pull_quotes,
    "articles":              _r_articles,
    "uncovered":             _r_uncovered,
    "coarse_axes":           _r_coarse_axes,
    "coarse_profile":        _r_coarse_profile,
    "coarse_slabs":          _r_coarse_slabs,
    "coarse_quadrants_l1":   _r_coarse_quadrants_l1,
    "coarse_quadrants_l2":   _r_coarse_quadrants_l2,
    "coarse_segments_4x6":   _r_coarse_segments_4x6,
    "segment_crop_marks":    _r_segment_crop_marks,
    "estimated_gutters":     _r_estimated_gutters,
    "pitch_grid":            _r_pitch_grid,
    "snapped_display_ads":   _r_snapped_display_ads,
    "snapped_photos":        _r_snapped_photos,
    "snapped_articles":      _r_snapped_articles,
    "snapped_uncovered":     _r_snapped_uncovered,
    "resolved_uncovered":    _r_resolved_uncovered,
    "structural_grid":       _r_structural_grid,
    "pixel_headlines":       _r_pixel_headlines,
    "horizontal_rules":      _r_horizontal_rules,
    "closed_rectangles":     _r_closed_rectangles,
    "resolved_articles":     _r_resolved_articles,
    "resolved_display_ads":  _r_resolved_display_ads,
    "resolved_photos":       _r_resolved_photos,
    "resolved_pull_quotes":  _r_resolved_pull_quotes,
    "resolved_dropped":      _r_resolved_dropped,
    "resolution_tags":       _r_resolution_tags,
    "column_grid":           _r_column_grid,
    "column_grid_bands":     _r_column_grid_bands,
    "column_grid_profiles":  _r_column_grid_profiles,
}


ALL_LAYERS_ORDER = [
    "masthead_fill", "whitespace_bands", "articles", "pull_quotes",
    "display_ads", "photos", "uncovered", "column_grid_bands",
    "column_grid_profiles", "coarse_profile", "coarse_axes",
    "column_grid", "text_area", "masthead_line",
]


def _expand_layers(layer_spec) -> List[str]:
    """Accept 'all' as a stand-in for the full ALL_LAYERS_ORDER list."""
    if isinstance(layer_spec, str):
        layer_spec = [layer_spec]
    out = []
    for name in layer_spec:
        if name == "all":
            out.extend(ALL_LAYERS_ORDER)
        elif name in LAYER_RENDERERS:
            out.append(name)
        else:
            raise ValueError(f"unknown layer {name!r}")
    return out


def render_view(base_png: str, layers_json: str, layer_names: List[str]) -> Image.Image:
    """Render one view of one page. Returns RGB PIL image."""
    with open(layers_json) as f:
        layers = json.load(f)
    img = Image.open(base_png).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for name in _expand_layers(layer_names):
        LAYER_RENDERERS[name](draw, img, layers, ZOOM)
    return Image.alpha_composite(img, overlay).convert("RGB")


@dataclass
class _View:
    label: str
    layers: List[str]


@dataclass
class _Page:
    """Identifies a page in a run. `key` is '<issue>/p<n>', e.g. '2007-02-13/p1'."""
    key: str

    @property
    def issue(self) -> str: return self.key.split("/")[0]
    @property
    def page(self) -> str: return self.key.split("/")[1]


class PreviewBuilder:
    """Build a side-by-side comparison HTML from a run's layer JSONs."""

    def __init__(self, run_dir: str, out_dir: str, title: str = "",
                 preamble: str = ""):
        self.run_dir = run_dir
        self.out_dir = out_dir
        self.title = title or os.path.basename(out_dir)
        self.preamble = preamble
        self.views: List[_View] = []
        self.pages: List[_Page] = []
        self.per_page_notes: Dict[str, str] = {}

    def add_view(self, label: str, layers):
        self.views.append(_View(label=label, layers=list(layers)))
        return self

    def add_page(self, key: str, note: str = ""):
        self.pages.append(_Page(key=key))
        if note:
            self.per_page_notes[key] = note
        return self

    def add_pages(self, keys, notes: Optional[Dict[str, str]] = None):
        for k in keys:
            self.add_page(k, (notes or {}).get(k, ""))
        return self

    def auto_discover_pages(self):
        """Walk the run_dir and add every page found (sorted)."""
        for jp in sorted(glob.glob(
                os.path.join(self.run_dir, "*/p*/qa/*_layers.json"))):
            issue = jp.split("/")[-4]
            page = jp.split("/")[-3]
            self.add_page(f"{issue}/{page}")
        return self

    def _resolve_paths(self, page: _Page) -> Tuple[str, str]:
        qa_dir = os.path.join(self.run_dir, page.issue, page.page, "qa")
        layers_json = glob.glob(os.path.join(qa_dir, "*_layers.json"))
        base_png = glob.glob(os.path.join(qa_dir, "*_base.png"))
        if not layers_json or not base_png:
            raise FileNotFoundError(f"no layers/base for {page.key}")
        return base_png[0], layers_json[0]

    def build(self):
        if not self.views:
            raise ValueError("no views configured")
        if not self.pages:
            raise ValueError("no pages configured")

        out_imgs_dir = os.path.join(self.out_dir, "renders")
        os.makedirs(out_imgs_dir, exist_ok=True)

        rendered: Dict[Tuple[str, str], str] = {}
        for page in self.pages:
            base_png, layers_json = self._resolve_paths(page)
            for view in self.views:
                img = render_view(base_png, layers_json, view.layers)
                # Filename: <issue>-<page>__<label>.png (label slugified).
                slug = (view.label.lower()
                        .replace(" ", "-").replace("/", "-")
                        .replace("(", "").replace(")", ""))
                fname = f"{page.issue}-{page.page}__{slug}.png"
                fpath = os.path.join(out_imgs_dir, fname)
                img.save(fpath)
                rendered[(page.key, view.label)] = f"renders/{fname}"

        # Write the HTML.
        html = self._render_html(rendered)
        out_html = os.path.join(self.out_dir, "index.html")
        with open(out_html, "w") as f:
            f.write(html)
        return out_html

    def _key_html(self) -> str:
        """Inline SVG-based legend explaining colours, stroke widths,
        and line styles used across the renderers."""
        rows_colour = [
            ("Article", "#0064FF", "solid", "Blue. Detected article body."),
            ("Display ad", "#DC1E1E", "solid", "Red. Detected ad box."),
            ("Photo", "#FF96B4", "solid", "Light pink. Detected photo region."),
            ("Uncovered", "#FF8200", "solid", "Orange. Visible ink not claimed by any detection."),
            ("Structural grid",  "#00563B", "dashed",
             "Racing-green dashed. Column gutters + horizontal whitespace bands + text-area edges + masthead bottom — the targets the snap pulls to."),
            ("Coarse axis", "#00563B", "dot-dash",
             "Racing-green dot-dash. Candidate axis from the coarse first pass (less confident)."),
            ("Dropped",  "#888888", "x-out",
             "Grey rectangle with a × through it. Item removed by resolution (nested duplicate, ad-with-no-text, orange that didn't match any rule)."),
            ("Untitled article", "#0064FF", "dashed-rect",
             "Blue dashed border. Article promoted from an orange region (no headline detected — flagged for review)."),
        ]
        rows_width = [
            ("2 px", "Background reference (structural grid, dropped diagonals, profile curves)."),
            ("3 px", "Coarse / less-confident detection (coarse candidate axes, original orange / pre-snap shapes)."),
            ("4 px", "Refined-grid medium-confidence column line, pre-snap article box."),
            ("5 px", "Refined-grid high-confidence column line, pull-quote outline, structural-grid emphasis."),
            ("6 px", "Snapped box (display ad, photo, article, uncovered)."),
            ("8 px", "Final resolved box (the boldest — the cutter's confident answer)."),
        ]

        def _sample_line(stroke, style):
            """24×16 SVG with one horizontal line in the given style/colour."""
            attrs = f'stroke="{stroke}" stroke-width="4" fill="none"'
            if style == "solid":
                pat = ""
            elif style == "dashed":
                pat = ' stroke-dasharray="6,3"'
            elif style == "dot-dash":
                pat = ' stroke-dasharray="6,2,1,2"'
            elif style == "x-out":
                return (f'<svg width="60" height="16">'
                        f'<rect x="2" y="2" width="56" height="12" '
                        f'stroke="{stroke}" stroke-width="2" fill="none"/>'
                        f'<line x1="2" y1="2" x2="58" y2="14" '
                        f'stroke="{stroke}" stroke-width="2"/>'
                        f'<line x1="2" y1="14" x2="58" y2="2" '
                        f'stroke="{stroke}" stroke-width="2"/>'
                        f'</svg>')
            elif style == "dashed-rect":
                return (f'<svg width="60" height="16">'
                        f'<rect x="2" y="2" width="56" height="12" '
                        f'stroke="{stroke}" stroke-width="3" fill="none" '
                        f'stroke-dasharray="5,3"/></svg>')
            else:
                pat = ""
            return (f'<svg width="60" height="16">'
                    f'<line x1="2" y1="8" x2="58" y2="8" {attrs}{pat}/></svg>')

        def _sample_width(px):
            return (f'<svg width="60" height="16">'
                    f'<line x1="2" y1="8" x2="58" y2="8" '
                    f'stroke="#1a1a1a" stroke-width="{px}" fill="none"/></svg>')

        parts = ['<details class="key" open>',
                  '<summary>Key — colours, line styles, stroke widths</summary>']
        parts.append('<h4>Colour & style</h4>')
        parts.append('<table>')
        for name, colour, style, desc in rows_colour:
            parts.append(
                f'<tr><td class="sample">{_sample_line(colour, style)}</td>'
                f'<td><b>{name}</b>: {desc}</td></tr>'
            )
        parts.append('</table>')
        parts.append('<h4>Stroke width — what it conveys</h4>')
        parts.append('<table>')
        for px_label, desc in rows_width:
            px = int(px_label.split()[0])
            parts.append(
                f'<tr><td class="sample">{_sample_width(px)}</td>'
                f'<td><b>{px_label}</b> — {desc}</td></tr>'
            )
        parts.append('</table>')
        parts.append('<h4>Resolution tags (B / C / D)</h4>')
        parts.append(
            '<p style="font-size:12px;color:var(--muted);margin:4px 0 0;">'
            'Small labels on resolved boxes show the rule that placed them:<br>'
            '<b>B1</b> = ad reclassified as photo (or dropped because a photo overlaps it).<br>'
            '<b>B2</b> = ad merged into a nearby article (boxed-article case).<br>'
            '<b>C1</b> = orange dropped as noise.<br>'
            '<b>C2</b> = orange attached to a photo as caption.<br>'
            '<b>C3</b> = orange merged into an adjacent article (orphan column).<br>'
            '<b>C4</b> = orange promoted to untitled article (headline missed).<br>'
            '<b>C5</b> = orange dropped (no rule matched).<br>'
            '<b>D</b> = item dropped as nested inside another after resolution.'
            '</p>')
        parts.append('</details>')
        return "".join(parts)

    def _render_html(self, rendered):
        n_cols = len(self.views)
        col_template = "1fr " * n_cols
        parts = ["""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>""", self.title, """</title>
<style>
  :root { --bg: #f7f6f3; --fg: #1a1a1a; --muted: #555; --rule: #d9d4c8; }
  html, body { background: var(--bg); color: var(--fg); margin: 0;
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", sans-serif;
    line-height: 1.5; }
  body { max-width: 1900px; margin: 0 auto; padding: 24px 28px 60px; }
  h1 { margin-top: 0; font-weight: 600; font-size: 22px; }
  h2 { font-weight: 600; font-size: 17px; margin: 32px 0 8px;
       padding-bottom: 4px; border-bottom: 1px solid var(--rule); }
  p.preamble { max-width: 82ch; color: var(--muted); font-size: 14px; }
  .pair { display: grid; grid-template-columns: """, col_template, """;
          gap: 12px; margin: 12px 0 24px; align-items: start; }
  .col { display: flex; flex-direction: column; }
  .col label { font-size: 11px; color: var(--muted); font-weight: 500;
               margin-bottom: 4px; letter-spacing: .02em; text-transform: uppercase; }
  .col img { width: 100%; height: auto; border: 1px solid var(--rule);
             background: white; }
  .note { font-size: 12px; color: var(--muted); margin: 4px 0 0; }
  details.key { margin: 8px 0 24px; max-width: 82ch; }
  details.key summary { cursor: pointer; font-weight: 600; font-size: 14px;
                        color: var(--fg); padding: 6px 0; }
  details.key table { font-size: 12px; border-collapse: collapse;
                      margin-top: 8px; }
  details.key td { padding: 5px 12px; border-bottom: 1px solid var(--rule);
                   vertical-align: middle; }
  details.key td.sample { width: 90px; min-width: 90px; }
  details.key td.sample svg { display: block; }
  details.key h4 { margin: 16px 0 4px; font-weight: 600;
                   font-size: 13px; color: var(--muted);
                   text-transform: uppercase; letter-spacing: .05em; }
</style>
</head>
<body>
<h1>""", self.title, """</h1>"""]
        if self.preamble:
            parts.append(f'<p class="preamble">{self.preamble}</p>')
        parts.append(self._key_html())
        for page in self.pages:
            parts.append(f"<h2>{page.issue} {page.page}</h2>")
            note = self.per_page_notes.get(page.key)
            if note:
                parts.append(f'<p class="note">{note}</p>')
            parts.append('<div class="pair">')
            for view in self.views:
                rel = rendered.get((page.key, view.label), "")
                parts.append(
                    f'<div class="col"><label>{view.label}</label>'
                    f'<img src="{rel}"></div>'
                )
            parts.append('</div>')
        parts.append("</body></html>\n")
        return "".join(parts)


def _parse_views_arg(views_str: str) -> List[Tuple[str, List[str]]]:
    out = []
    for spec in views_str.split():
        if "=" not in spec:
            raise ValueError(f"view spec must be label=layer,layer: {spec!r}")
        label, csv = spec.split("=", 1)
        out.append((label.replace("_", " "), csv.split(",")))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True,
                    help="cutter output root (contains <issue>/p<n>/qa/...)")
    ap.add_argument("--out", required=True,
                    help="preview output directory (e.g. preview/post1980_phase2r)")
    ap.add_argument("--title", default="")
    ap.add_argument("--preamble", default="")
    ap.add_argument("--views", required=True,
                    help='space-separated "label=layer,layer ..." specs '
                         '(use _ for spaces in label)')
    ap.add_argument("--pages", default="",
                    help='comma-separated "<issue>/<page>" keys; '
                         'omit to auto-discover')
    args = ap.parse_args()

    pb = PreviewBuilder(run_dir=args.run_dir, out_dir=args.out,
                        title=args.title, preamble=args.preamble)
    for (label, layers) in _parse_views_arg(args.views):
        pb.add_view(label, layers)
    if args.pages:
        pb.add_pages(args.pages.split(","))
    else:
        pb.auto_discover_pages()
    out = pb.build()
    print(out)


if __name__ == "__main__":
    main()
