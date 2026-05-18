"""Column-grid detection for modular post-1980 pages.

The premise (per user direction 2026-05-17): although the *reading
order* is no longer column-flowing in this period, the *physical
column grid* is still regular — articles are stacked into bounded
rectangles, but those rectangles snap to a page-level column grid.
Knowing the grid is useful even without using it for reading-order:

  - Article bbox edges can be snapped to grid lines so a bbox doesn't
    bisect a column of text.
  - The grid gives confidence in cuts: a detected article whose right
    edge aligns to a grid line is more credible than one ending mid-
    column.

Signal: in this period the column separator is **a gutter of white
space**, not a printed rule. The vertical column profile across a
horizontal slab of body text shows a sequence of dark/light/dark
("U-shape") — text-gutter-text-gutter-text. We borrow the classical
`find_columns.find_column_boundaries` valley-detection logic (Pass 3
there) but apply it to **adaptively placed measurement bands** —
slabs of the page that are below the masthead, above any whitespace
band, not inside ads, and contain enough body text to give a clean
profile.

The grid is the consensus across multiple bands. A boundary x-position
is "high confidence" when ≥3 bands agree; "medium" when 2 agree;
"low" when only 1 found it.

Returns a list of `GridBoundary` objects (x in points, confidence).
"""
import os
import sys
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

import fitz
import numpy as np


# Add the repo root for importing render_grey from pdf_utils
_repo = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# Knobs — these are starting points; would be retuned on a wider sample.
GRID_DPI = 300                # render DPI for the column profile
GRID_BAND_MIN_HEIGHT_PT = 40  # measurement band must be at least this tall
GRID_BAND_MAX_HEIGHT_PT = 90  # taller than this and the band starts
                              # straddling article rows or hitting ads
GRID_MAX_BANDS = 10           # cap on regions to measure (perf) — more
                              # generous because we may split per-band
                              # into multiple x-zones
GRID_MIN_ZONE_WIDTH_PT = 150  # an x-zone needs at least this much width
                              # to produce ≥1 column gutter. Narrow-
                              # column pages (e.g. 2007-02-13 p3) have
                              # columns around 100pt wide so a 150pt
                              # strip can fit two narrow columns + one
                              # gutter. Lower than 150 starts catching
                              # half-column wide whitespace strips that
                              # don't contain a real gutter.
GRID_EDGE_MARGIN_PT = 50      # reject valleys this close to the actual
                              # PAGE edges (left/right paper margins).
                              # Region clip-edges that abut an obstacle
                              # are NOT suppressed — article/photo/ad
                              # edges typically sit on the page-grid
                              # column lines, so a valley right at an
                              # obstacle boundary is a real gutter, not
                              # a clip artefact.
GRID_RUN_OVER_PT = 12.0       # let each measurement region extend this
                              # far INTO adjacent obstacles. Per user
                              # direction 2026-05-17: article edges
                              # have no border, just a gutter against
                              # the next column. If we stop the measure-
                              # ment at the obstacle edge exactly, the
                              # gutter sits right at the clip boundary
                              # and gets discarded. Extending 12pt past
                              # the boundary puts the gutter solidly
                              # inside the measurement region.
# Profile scale: raw inverted greyscale col-mean (0..255), matching the
# classical pre-1980 find_columns.py approach. We do NOT binarise and
# we do NOT vertically blur — keeping the full greyscale means a text
# column's column-mean naturally averages text strokes with the
# line-spacing gaps between them, so text sits ~30-80 and gutters stay
# near 0 without any extra processing.
GRID_NEAR_WHITE_CAP = 8.0     # clip near-white profile values to 0 so
                              # troughs have a flat zero floor (instead
                              # of bouncing on scanner noise / faint
                              # marks). 8/255 ≈ 3% inverted-darkness.
GRID_VALLEY_MAX = 8.0         # a gutter column averages below this on
                              # the (post-clip) profile — i.e. it must
                              # genuinely flatline to white, per the
                              # user's "if there's no gap, it's not a
                              # gutter" rule.
GRID_CONTENT_MIN = 35.0       # flanks must rise to at least this on the
                              # profile to count as text-on-both-sides
                              # (≈14% inverted-darkness — comfortably
                              # above body text's natural ~30-80 range).
GRID_MIN_VALLEY_WIDTH_PX = 4  # gutter must be wider than this
GRID_CLUSTER_TOL_PT = 18.0    # boundaries within this pt cluster as one
GRID_DROP_LOW_CONF = True     # only emit boundaries with ≥2 band votes
                              # — single-band detections are too noisy


@dataclass
class MeasurementBand:
    """A horizontal slab used to measure column structure.

    `x0_pt`/`x1_pt` default to None (= full page width). When a y-band
    has ads in the middle of the page, we split the band into multiple
    regions covering only the clean x-zones (e.g. left half, right
    half) so we can still find column gutters in whichever half is
    ad-free.
    """
    y0_pt: float
    y1_pt: float
    reason: str
    x0_pt: Optional[float] = None
    x1_pt: Optional[float] = None


@dataclass
class GridBoundary:
    """A column boundary in the page's grid."""
    x_pt: float
    confidence: str   # 'high', 'medium', 'low'
    vote_count: int   # how many measurement bands found it
    bands_found: List[int] = field(default_factory=list)


def pick_measurement_bands(page_h, masthead_bottom, whitespace_bands,
                           ads_bboxes, page_w=None,
                           max_bands=GRID_MAX_BANDS):
    """Place measurement regions that avoid detected ads.

    For each candidate y-band:
      1. Find ads that overlap the band's y-range.
      2. Subtract their x-ranges from the full page width [0, page_w].
        That gives clean x-zones — could be the full width if no ads
        at this y, or LEFT half if right side has an ad, or two
        separate zones if there's an ad in the middle.
      3. For each clean x-zone wider than GRID_MIN_ZONE_WIDTH_PT
        (so it can fit at least one inner column + a gutter), emit
        one MeasurementBand region (y-band × x-zone).

    No "abstain" path now — even ad-saturated pages yield SOME usable
    regions wherever a clean zone exists.
    """
    content_top = masthead_bottom + 20
    content_bot = page_h - 50
    if content_bot <= content_top + GRID_BAND_MIN_HEIGHT_PT:
        return []
    if page_w is None:
        page_w = 1.0e6   # effectively unlimited

    content_h = content_bot - content_top
    n_seeds = 10
    band_h = GRID_BAND_MAX_HEIGHT_PT
    seeds = [content_top + content_h * (i + 0.5) / n_seeds for i in range(n_seeds)]

    chosen = []
    for s in seeds:
        b_y0 = s - band_h / 2
        b_y1 = s + band_h / 2
        if b_y0 < content_top:
            b_y0 = content_top; b_y1 = b_y0 + band_h
        if b_y1 > content_bot:
            b_y1 = content_bot; b_y0 = b_y1 - band_h
        if b_y1 - b_y0 < GRID_BAND_MIN_HEIGHT_PT:
            continue

        # Find ads overlapping this band's y-range
        ads_in_band = [ad for ad in ads_bboxes
                        if not (ad[3] <= b_y0 or ad[1] >= b_y1)]
        clean_zones = _compute_clean_x_zones(0.0, page_w, ads_in_band)

        for (zx0, zx1) in clean_zones:
            if (zx1 - zx0) < GRID_MIN_ZONE_WIDTH_PT:
                continue
            chosen.append(MeasurementBand(
                y0_pt=b_y0, y1_pt=b_y1,
                x0_pt=zx0, x1_pt=zx1,
                reason=f"y={b_y0:.0f}-{b_y1:.0f},x={zx0:.0f}-{zx1:.0f}",
            ))
            if len(chosen) >= max_bands:
                break
        if len(chosen) >= max_bands:
            break

    chosen.sort(key=lambda b: (b.y0_pt, b.x0_pt or 0))
    return chosen


def _compute_clean_x_zones(x0, x1, ads, run_over_pt=GRID_RUN_OVER_PT):
    """Return list of (zx0, zx1) clean x-intervals within [x0, x1]
    that don't overlap any ad's x-range, with each obstacle shrunk by
    `run_over_pt` on each side so the resulting zones extend slightly
    into the obstacles. Article/photo edges sit on the page-grid
    column lines, so letting the measurement zone overlap a few pt of
    the obstacle keeps the gutter inside the measured region instead
    of right at its clip edge.
    """
    intervals = [(x0, x1)]
    for ad in ads:
        ax0, _ay0, ax1, _ay1 = ad
        # Shrink the obstacle by run_over_pt on each side. Tiny
        # obstacles (< 2*run_over_pt wide) collapse to nothing —
        # they're so narrow that the clean zones span across them
        # anyway, which is fine.
        ax0_eff = ax0 + run_over_pt
        ax1_eff = ax1 - run_over_pt
        if ax0_eff >= ax1_eff:
            continue
        new = []
        for (cx0, cx1) in intervals:
            if ax1_eff <= cx0 or ax0_eff >= cx1:
                new.append((cx0, cx1))
                continue
            if ax0_eff > cx0:
                new.append((cx0, min(ax0_eff, cx1)))
            if ax1_eff < cx1:
                new.append((max(ax1_eff, cx0), cx1))
        intervals = new
    return intervals


def find_valleys_in_band(page, band, ads_bboxes, dark_thr=130,
                          return_profile=False):
    """Run valley detection on one measurement region.

    The region is `band` (y0_pt, y1_pt) optionally clipped to an x-zone
    (x0_pt, x1_pt). Valleys (gutters) are found within the clipped
    region; their x-positions are translated back to page coordinates.

    Returns a list of (x_pt, depth_score) for each gutter found.
    """
    page_w_pt = page.rect.width
    zoom = GRID_DPI / 72.0

    # Clip to the region's x and y ranges. Defaults to full page width.
    x0_pt = band.x0_pt if band.x0_pt is not None else 0.0
    x1_pt = band.x1_pt if band.x1_pt is not None else page_w_pt

    clip = fitz.Rect(x0_pt, band.y0_pt, x1_pt, band.y1_pt)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False,
                          clip=clip)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width).copy()
    H, W = img.shape

    # Mask obstacle pixels — only the actual 2D footprint of the
    # obstacle within this band, not the full band height. A headline
    # crossing the top of the band must not zero out the column-mean
    # for the body text below it. With run-over zones, obstacles
    # extend slightly into the band's clean x-zone; masking their
    # interior to white means the col-mean still reads "near zero"
    # over the obstacle's interior, which doesn't confuse the valley
    # detector (no flank rises high enough to qualify as content).
    for ad in ads_bboxes:
        ax0, ay0, ax1, ay1 = ad
        if ay1 <= band.y0_pt or ay0 >= band.y1_pt:
            continue
        local_ax0 = max(0.0, ax0 - x0_pt)
        local_ax1 = max(0.0, ax1 - x0_pt)
        local_ay0 = max(0.0, ay0 - band.y0_pt)
        local_ay1 = max(0.0, ay1 - band.y0_pt)
        cx0 = max(0, int(round(local_ax0 * zoom)))
        cx1 = min(W, int(round(local_ax1 * zoom)))
        cy0 = max(0, int(round(local_ay0 * zoom)))
        cy1 = min(H, int(round(local_ay1 * zoom)))
        if cx1 > cx0 and cy1 > cy0:
            img[cy0:cy1, cx0:cx1] = 255

    # Inverted greyscale column-mean — same shape as classical
    # find_columns. Dark text → high values, white paper → 0.
    # No binarisation: the full greyscale naturally averages text
    # strokes with the line-spacing gaps in a text column, so we
    # don't need a vertical blur to bridge those gaps.
    inv = 255.0 - img.astype(np.float32)
    col_means = inv.mean(axis=0)

    # Clip near-white values to 0 so genuine gutters have a flat zero
    # floor in the profile (per user direction 2026-05-17: shallow
    # non-zero dips were being misread as gutters; clipping suppresses
    # scanner-noise bumps so only real troughs survive).
    col_means = np.where(col_means < GRID_NEAR_WHITE_CAP, 0.0, col_means)

    # Smooth with a small window so single-pixel noise doesn't trip us
    win = max(3, int(zoom * 1.5))
    kernel = np.ones(win) / win
    smoothed = np.convolve(col_means, kernel, mode="same")

    # Find valleys: contiguous runs of smoothed < GRID_VALLEY_MAX_FRAC
    # with content (>= GRID_CONTENT_MIN_FRAC) on both sides. For each
    # valley, locate the precise content-edges on the left and right
    # (where col_means crosses GRID_CONTENT_MIN_FRAC) with subpixel
    # interpolation, and report the midpoint between those edges as
    # the grid boundary. This gives a more precise centre than the
    # simple midpoint of the below-threshold run.
    valleys = []
    i = 0
    while i < W:
        if smoothed[i] >= GRID_VALLEY_MAX:
            i += 1
            continue
        j = i
        while j < W and smoothed[j] < GRID_VALLEY_MAX:
            j += 1
        vw = j - i
        if vw < GRID_MIN_VALLEY_WIDTH_PX:
            i = j
            continue
        flank_w = max(vw, 12)
        left = smoothed[max(0, i - flank_w):i]
        right = smoothed[j:min(W, j + flank_w)]
        if (len(left) == 0 or left.max() < GRID_CONTENT_MIN):
            i = j
            continue
        if (len(right) == 0 or right.max() < GRID_CONTENT_MIN):
            i = j
            continue

        # Precise edge location with subpixel interpolation.
        # Scan from valley inward toward content on each side until
        # we cross GRID_CONTENT_MIN ascending.
        left_edge_px = _find_edge_crossing(
            smoothed, start_px=i, direction=-1,
            threshold=GRID_CONTENT_MIN, max_scan=flank_w,
        )
        right_edge_px = _find_edge_crossing(
            smoothed, start_px=j - 1, direction=+1,
            threshold=GRID_CONTENT_MIN, max_scan=flank_w,
        )
        if left_edge_px is None or right_edge_px is None:
            i = j
            continue
        # Midline = average of the two edges
        cx_px = (left_edge_px + right_edge_px) / 2.0

        # Page-edge suppression only — reject valleys close to the
        # paper margin. Region clip-edges that abut an obstacle are
        # NOT suppressed: per user direction, the gutter between an
        # article and an adjacent ad/photo lives right at the obstacle
        # boundary, and that boundary IS a real grid line.
        page_x_pt = x0_pt + cx_px / zoom
        if (page_x_pt < GRID_EDGE_MARGIN_PT
                or page_x_pt > (page_w_pt - GRID_EDGE_MARGIN_PT)):
            i = j
            continue

        # Depth = max(content_around) - min(valley)
        content_around = float(max(left.max() if len(left) else 0.0,
                                    right.max() if len(right) else 0.0))
        valley_min = float(smoothed[i:j].min())
        depth = content_around - valley_min
        # Translate region-local px → page-pt by adding the region's
        # x-origin (0.0 if region spans full page width).
        x_pt = x0_pt + cx_px / zoom
        valleys.append((x_pt, depth))
        i = j

    if return_profile:
        # Sample the smoothed profile at ~150 points evenly across
        # the region for plotting alongside the overlay.
        n_samples = min(150, W)
        step = max(1, W // n_samples)
        profile = [
            (x0_pt + px / zoom, float(smoothed[px]))
            for px in range(0, W, step)
        ]
        return valleys, profile
    return valleys


def _find_edge_crossing(profile, start_px, direction, threshold, max_scan):
    """Scan `profile` from start_px in `direction` (-1 left, +1 right)
    looking for the first pixel where the value crosses threshold
    ASCENDING. Returns subpixel-interpolated x or None.
    """
    n = len(profile)
    end_px = start_px + direction * max_scan
    end_px = max(0, min(n - 1, end_px))
    prev = start_px
    px = start_px + direction
    while (direction > 0 and px <= end_px) or (direction < 0 and px >= end_px):
        if profile[px] >= threshold:
            # Linear interp between profile[prev] (below) and profile[px]
            v_prev = float(profile[prev])
            v_curr = float(profile[px])
            if v_curr - v_prev > 0:
                t = (threshold - v_prev) / (v_curr - v_prev)
                return prev + t * (px - prev)
            return float(px)
        prev = px
        px += direction
    return None


def consensus_grid(per_band_valleys, tol_pt=GRID_CLUSTER_TOL_PT):
    """Cluster the per-band valley lists into a consensus grid.

    Each band contributes some x-positions; cluster across bands so
    nearby positions become one boundary. The number of bands that
    found it = vote count, which maps to confidence.
    """
    all_obs = []
    for band_idx, valleys in enumerate(per_band_valleys):
        for (x_pt, depth) in valleys:
            all_obs.append((x_pt, depth, band_idx))
    all_obs.sort(key=lambda o: o[0])

    grid = []
    cluster = []
    for obs in all_obs:
        if not cluster:
            cluster.append(obs)
            continue
        if obs[0] - cluster[-1][0] <= tol_pt:
            cluster.append(obs)
        else:
            grid.append(_summarise_cluster(cluster, len(per_band_valleys)))
            cluster = [obs]
    if cluster:
        grid.append(_summarise_cluster(cluster, len(per_band_valleys)))
    if GRID_DROP_LOW_CONF:
        grid = [g for g in grid if g.confidence in ("high", "medium")]
    return grid


def _summarise_cluster(cluster, n_bands):
    xs = [o[0] for o in cluster]
    bands_found = sorted(set(o[2] for o in cluster))
    n_votes = len(bands_found)
    if n_bands >= 3 and n_votes >= 3:
        conf = "high"
    elif n_votes >= 2:
        conf = "medium"
    else:
        conf = "low"
    return GridBoundary(
        x_pt=float(np.mean(xs)),
        confidence=conf,
        vote_count=n_votes,
        bands_found=bands_found,
    )


def find_column_grid(page, masthead_bottom, whitespace_bands, ads_bboxes,
                     dark_thr=130, return_profiles=False):
    """Detect the column grid for a modular page.

    Returns (bands, grid) or (bands, grid, profiles) when
    return_profiles=True. `profiles[i]` is a list of (x_pt, value)
    pairs sampled from the smoothed col-mean curve for `bands[i]`.
    """
    page_h = page.rect.height
    page_w = page.rect.width
    bands = pick_measurement_bands(page_h, masthead_bottom,
                                    whitespace_bands, ads_bboxes,
                                    page_w=page_w)
    per_band = []
    profiles = []
    for b in bands:
        if return_profiles:
            valleys, prof = find_valleys_in_band(
                page, b, ads_bboxes, dark_thr, return_profile=True,
            )
            profiles.append(prof)
        else:
            valleys = find_valleys_in_band(page, b, ads_bboxes, dark_thr)
        per_band.append(valleys)
    grid = consensus_grid(per_band)
    if return_profiles:
        return bands, grid, profiles
    return bands, grid
