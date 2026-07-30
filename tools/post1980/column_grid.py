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
GRID_MAX_BANDS = 20           # cap on regions to measure (perf). 20
                              # accommodates clear-run y-zones packed
                              # with multiple full-width bands plus
                              # narrow-zone bands for obstructed areas.
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
GRID_NEAR_WHITE_CAP = 15.0    # clip near-white profile values to 0 so
                              # troughs have a flat zero floor.
                              # Empirically (2007-p1 measurement, May
                              # 17) real gutters sit anywhere from
                              # 0–15 on the raw inverted-greyscale
                              # col-mean — text-descender bleed and
                              # scanner noise push the floor up from
                              # zero. Column-interior word-spacing
                              # minima are ≥25, leaving a clear ≥10pt
                              # safety gap between gutter and noise.
GRID_VALLEY_MAX = 15.0        # a gutter column averages below this on
                              # the (post-clip) profile. Matches the
                              # near-white cap so the visual profile
                              # still flatlines at zero through real
                              # gutters.
GRID_CONTENT_MIN = 35.0       # flanks must rise to at least this on the
                              # profile to count as text-on-both-sides
                              # (≈14% inverted-darkness — comfortably
                              # above body text's natural ~30-80 range).
GRID_MIN_VALLEY_WIDTH_PX = 4  # gutter must be wider than this
GRID_CLUSTER_TOL_PT = 25.0    # boundaries within this pt cluster as one.
                              # Was 18; raised after observing multiple
                              # close detections (e.g. 284/288, 919/963,
                              # 963/966 on 1990-p1 band 4) that are
                              # plainly the same gutter measured slightly
                              # differently. Real columns are ≥80pt
                              # apart so 25pt is safe.
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
    """Place measurement regions, preferring clear-run y-zones.

    Approach (per user direction 2026-05-18):

      1. Find contiguous y-ranges where the page is "full-width clear"
         — no obstacle blocks more than 20% of page width at any y in
         the range. These are the slabs of body text that run all the
         way across the page; the canonical signal for the underlying
         column grid lives here.
      2. Pack multiple full-width bands into each clear-run y-range
         (tighter spacing than the old fixed-seed approach), so each
         page-grid gutter gets corroborating votes from several bands.
      3. For the y-ranges that ARE obstructed (between/around photos
         and ads), fall back to seeded narrow bands clipped to clean
         x-zones — that recovers a partial signal from each half of a
         photo-divided page.
    """
    content_top = masthead_bottom + 20
    content_bot = page_h - 50
    if content_bot <= content_top + GRID_BAND_MIN_HEIGHT_PT:
        return []
    if page_w is None:
        page_w = 1.0e6   # effectively unlimited

    band_h = GRID_BAND_MAX_HEIGHT_PT
    full_threshold = 0.80 * page_w   # zone must cover ≥80% page width

    # Scan vertically at 10pt resolution to find clear-run y-zones.
    def _max_clean_at_y(y):
        ads_at_y = [ad for ad in ads_bboxes if ad[1] <= y <= ad[3]]
        zones = _compute_clean_x_zones(0.0, page_w, ads_at_y)
        return max(((z1 - z0) for (z0, z1) in zones), default=0.0)

    step = 10.0
    full_runs = []
    run_start = None
    y = content_top
    while y <= content_bot:
        if _max_clean_at_y(y) >= full_threshold:
            if run_start is None:
                run_start = y
        else:
            if run_start is not None:
                full_runs.append((run_start, y - step))
                run_start = None
        y += step
    if run_start is not None:
        full_runs.append((run_start, content_bot))

    chosen = []

    # Step 2: pack bands into each clear-run y-zone.
    # Use 50% overlap between consecutive bands so adjacent bands
    # share half their y-extent; this maximises vote count per gutter
    # within the run while keeping each band's y-extent independent
    # enough that they're not measuring identical pixels.
    band_spacing = band_h / 2.0
    for (r0, r1) in full_runs:
        run_h = r1 - r0
        if run_h < GRID_BAND_MIN_HEIGHT_PT:
            continue
        if run_h <= band_h:
            chosen.append(MeasurementBand(
                y0_pt=r0, y1_pt=r1,
                x0_pt=0.0, x1_pt=page_w,
                reason=f"clear-run y={r0:.0f}-{r1:.0f} (single band)",
            ))
            continue
        n_bands = max(1, int((run_h - band_h) / band_spacing) + 1)
        for i in range(n_bands):
            b_y0 = r0 + i * band_spacing
            b_y1 = b_y0 + band_h
            if b_y1 > r1:
                b_y1 = r1
                b_y0 = max(r0, b_y1 - band_h)
            chosen.append(MeasurementBand(
                y0_pt=b_y0, y1_pt=b_y1,
                x0_pt=0.0, x1_pt=page_w,
                reason=f"clear-run y={r0:.0f}-{r1:.0f} band {i+1}/{n_bands}",
            ))
            if len(chosen) >= max_bands:
                break
        if len(chosen) >= max_bands:
            break

    # Step 3: in obstructed y-zones (gaps between clear-runs), seed
    # narrow bands clipped to whatever clean x-zone exists. This is
    # the old behaviour preserved for the half-page-divided regions.
    obstructed_zones = []
    cursor = content_top
    for (r0, r1) in full_runs:
        if cursor < r0:
            obstructed_zones.append((cursor, r0))
        cursor = r1
    if cursor < content_bot:
        obstructed_zones.append((cursor, content_bot))

    for (oz0, oz1) in obstructed_zones:
        zone_h = oz1 - oz0
        if zone_h < GRID_BAND_MIN_HEIGHT_PT:
            continue
        # Seed bands across the obstructed zone — one every ~150pt of
        # vertical extent. A tall obstructed slab (e.g. a page with
        # no clear-run zones at all) needs multiple seeds because an
        # individual y might happen to land where all clean x-zones
        # are too narrow; another y a bit higher or lower may have a
        # usable strip.
        n_seeds = max(1, int(round(zone_h / 150.0)))
        for k in range(n_seeds):
            s = oz0 + zone_h * (k + 0.5) / n_seeds
            b_y0 = max(oz0, s - band_h / 2)
            b_y1 = min(oz1, b_y0 + band_h)
            if b_y0 < oz0: b_y0 = oz0
            if b_y1 - b_y0 < GRID_BAND_MIN_HEIGHT_PT:
                continue
            ads_in_band = [ad for ad in ads_bboxes
                            if not (ad[3] <= b_y0 or ad[1] >= b_y1)]
            clean_zones = _compute_clean_x_zones(0.0, page_w, ads_in_band)
            for (zx0, zx1) in clean_zones:
                if (zx1 - zx0) < GRID_MIN_ZONE_WIDTH_PT:
                    continue
                chosen.append(MeasurementBand(
                    y0_pt=b_y0, y1_pt=b_y1,
                    x0_pt=zx0, x1_pt=zx1,
                    reason=f"obstructed y={b_y0:.0f}-{b_y1:.0f},x={zx0:.0f}-{zx1:.0f}",
                ))
                if len(chosen) >= max_bands:
                    break
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
    # Loose search threshold: any dip below this is a CANDIDATE for
    # scoring. Not a pass/fail filter — just bounds the search space.
    # Quality of each candidate is graded by data-derived features,
    # not by a hard threshold cutoff.
    SEARCH_THR = 30.0

    valleys = []
    i = 0
    while i < W:
        if smoothed[i] >= SEARCH_THR:
            i += 1
            continue
        j = i
        while j < W and smoothed[j] < SEARCH_THR:
            j += 1
        vw = j - i
        if vw < GRID_MIN_VALLEY_WIDTH_PX:
            i = j
            continue

        # --- Quality scoring (continuous, not pass/fail) -----------
        # Two independent evidence features, each scaled to [0, 1]:
        #
        #   flat_score   = width of the flat-zero floor (after clip)
        #                  normalised against an 8px reference
        #   flank_score  = strength of the WEAKER text flank, normalised
        #                  against a 50-on-the-profile reference (a
        #                  representative body-text col-mean)
        #
        # quality = flat_score * flank_score → a single dip with strong
        # flat-zero floor and strong text on both sides scores ~1.0;
        # a gentle V-dip with weak flanks scores < 0.2. Multiplicative
        # so a missing feature kills the score (high-grey-base "valleys"
        # that the user flagged as suspicious).
        flat_zero_px = float((col_means[i:j] == 0.0).sum())
        flat_score = min(flat_zero_px / 8.0, 1.0)
        flank_w = max(vw, 12)
        left = smoothed[max(0, i - flank_w):i]
        right = smoothed[j:min(W, j + flank_w)]
        left_max = float(left.max()) if len(left) else 0.0
        right_max = float(right.max()) if len(right) else 0.0
        flank_min = min(left_max, right_max)
        flank_score = min(flank_min / 50.0, 1.0)
        quality = flat_score * flank_score
        if quality < 0.05:
            # Lower bound on emission — keeps tiny noise out of the
            # cluster step. Anything above gets passed up for scoring.
            i = j
            continue

        # Precise edge location with subpixel interpolation, using the
        # flank-max as the reference threshold (instead of a fixed
        # constant). This adapts to per-page contrast.
        edge_thr = max(flank_min * 0.5, 15.0)
        left_edge_px = _find_edge_crossing(
            smoothed, start_px=i, direction=-1,
            threshold=edge_thr, max_scan=flank_w,
        )
        right_edge_px = _find_edge_crossing(
            smoothed, start_px=j - 1, direction=+1,
            threshold=edge_thr, max_scan=flank_w,
        )
        if left_edge_px is None or right_edge_px is None:
            i = j
            continue
        cx_px = (left_edge_px + right_edge_px) / 2.0

        # Page-edge suppression: reject valleys close to the paper
        # margin. Region clip-edges that abut an obstacle are NOT
        # suppressed — those boundaries ARE real grid lines.
        page_x_pt = x0_pt + cx_px / zoom
        if (page_x_pt < GRID_EDGE_MARGIN_PT
                or page_x_pt > (page_w_pt - GRID_EDGE_MARGIN_PT)):
            i = j
            continue

        x_pt = x0_pt + cx_px / zoom
        valleys.append((x_pt, quality))
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


def consensus_grid(per_band_valleys, bands=None, page_w=None,
                   tol_pt=GRID_CLUSTER_TOL_PT, coarse_candidates=None):
    """Cluster the per-band valley lists into a consensus grid.

    Each band contributes some x-positions; cluster across bands so
    nearby positions become one boundary. Votes are weighted by band
    width — a band spanning ≥80% of page width counts as 2 votes,
    narrower bands count as 1.

    Per user direction 2026-05-18, a coarse first-pass over the whole
    page (no obstacle awareness) gives independent candidate axes from
    the paper's underlying grid. A refined cluster that aligns with a
    coarse candidate gets a +1 vote bonus. This boosts real page-grid
    gutters (which appear in both passes) and lets locally-significant
    gutters that DON'T appear in the coarse pass — e.g. narrow-
    classifieds-only gutters at the bottom of 1990-p1 — fall short of
    the high-confidence threshold.
    """
    all_obs = []
    for band_idx, valleys in enumerate(per_band_valleys):
        for (x_pt, depth) in valleys:
            all_obs.append((x_pt, depth, band_idx))
    all_obs.sort(key=lambda o: o[0])

    band_weights = _band_weights(bands, page_w)

    grid = []
    cluster = []
    for obs in all_obs:
        if not cluster:
            cluster.append(obs)
            continue
        if obs[0] - cluster[-1][0] <= tol_pt:
            cluster.append(obs)
        else:
            grid.append(_summarise_cluster(cluster, len(per_band_valleys),
                                            band_weights, coarse_candidates,
                                            tol_pt))
            cluster = [obs]
    if cluster:
        grid.append(_summarise_cluster(cluster, len(per_band_valleys),
                                        band_weights, coarse_candidates,
                                        tol_pt))
    if GRID_DROP_LOW_CONF:
        grid = [g for g in grid if g.confidence in ("high", "medium")]
    return grid


def coarse_axis_candidates(page, mast_y, page_w, page_h, dark_thr=130,
                            return_profile=False,
                            text_area_left_pt=None,
                            text_area_right_pt=None):
    """First-pass full-page scan for candidate column axes.

    Per user direction 2026-05-18: do a crude assessment of page
    gutters BEFORE the obstacle-aware refined pass. Where this first
    pass aligns with refined detections, that's a strong cross-
    corroboration signal.

    Method:
      1. Render the whole page in greyscale at low DPI.
      2. Mask the masthead band (white) so display type doesn't
         dominate the col-means.
      3. Activity-weight y-rows by their col-value std (per the pre-
         1980 abs-sum technique): text-rich rows have high variance
         (alternating text-column / gutter / text-column), photo and
         whitespace rows have low variance. The most-active 50% of
         rows is the slab where the column grid is most legible.
      4. Inverted col-mean across that slab. Find deep local minima
         with sufficient peak-to-trough delta — they're the candidate
         page-grid gutters.

    Returns a list of x_pt values (likely 4–6 candidates on a typical
    body-text page). Liberal: false positives are fine because the
    refined pass filters via its own thresholds and consensus.
    """
    zoom = 150 / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width).copy()
    H, W = img.shape
    mast_px = max(0, int(round((mast_y or 0) * zoom)))
    if 0 < mast_px < H:
        img[:mast_px, :] = 255

    inv = 255.0 - img.astype(np.float32)

    # Recursive 2×2 quadrant grids (per user direction 2026-05-18).
    # Each region restricted in BOTH x and y so the col-mean for one
    # region isn't contaminated by what's happening elsewhere on the
    # page (a photo on the right doesn't kill column signal on the
    # left). Level 1: 2×2 = 4 quadrants. Level 2: 4×4 = 16 sub-
    # quadrants. Each region runs an independent coarse pass and is
    # surfaced as its own data — nothing merged, nothing discarded.
    content_top_px = mast_px or 0
    content_h_px = H - content_top_px

    def _build_grid(nx, ny):
        """Build nx×ny grid of regions, each as (x0px, y0px, x1px, y1px)."""
        regions = []
        col_w = W // nx
        row_h = content_h_px // ny
        for iy in range(ny):
            for ix in range(nx):
                x0 = ix * col_w
                x1 = (ix + 1) * col_w if ix < nx - 1 else W
                y0 = content_top_px + iy * row_h
                y1 = content_top_px + (iy + 1) * row_h if iy < ny - 1 else H
                regions.append((x0, y0, x1, y1))
        return regions

    quadrants_l1 = _build_grid(2, 2) if content_h_px > 0 else []
    quadrants_l2 = _build_grid(4, 4) if content_h_px > 0 else []
    # 4 across × 6 down — finer-grained for the per-segment scoring
    # pass (per user direction 2026-05-18). Wider aspect than 4×4
    # because newspaper pages are vertically stacked into rows of
    # articles; more y-bands captures more independent observations.
    segments_4x6 = _build_grid(4, 6) if content_h_px > 0 else []

    # Detection now uses the SAME criterion as the orange-dot rendering
    # (per user direction 2026-05-18): values at or below the 5th
    # percentile of margin-excluded inner col-means. Contiguous runs
    # of below-threshold pixels collapse to one candidate at their
    # midpoint. So each "orange-dot cluster" visible on the chart
    # becomes exactly one candidate axis — detection and visualisation
    # match by construction.
    win = max(3, int(zoom * 1.5))
    margin_px = int(round(GRID_EDGE_MARGIN_PT * zoom))
    page_w_px = W

    # Inner range in PAGE pixels: text_area edges shifted inward by
    # 30pt (so the percentile reflects gutters, not paper margins).
    inner_into_pt = 30.0
    if text_area_left_pt is not None:
        inner_lo_px = int(round((text_area_left_pt + inner_into_pt) * zoom))
    else:
        inner_lo_px = margin_px
    if text_area_right_pt is not None:
        inner_hi_px = int(round((text_area_right_pt - inner_into_pt) * zoom))
    else:
        inner_hi_px = page_w_px - margin_px

    def _detect_in_cm(cm, x_origin_px=0):
        region_w = len(cm)
        smoothed = np.convolve(cm, np.ones(win) / win, mode="same")
        # Convert page-pixel inner range to region-local pixels.
        lo_local = max(0, inner_lo_px - x_origin_px)
        hi_local = min(region_w, inner_hi_px - x_origin_px)
        if hi_local - lo_local < 5:
            return [], smoothed
        thr = float(np.percentile(smoothed[lo_local:hi_local], 5))
        # Find contiguous runs at or below threshold within inner range
        cands = []
        i = lo_local
        while i < hi_local:
            if smoothed[i] > thr:
                i += 1; continue
            j = i
            while j < hi_local and smoothed[j] <= thr:
                j += 1
            if (j - i) >= 2:
                mid_px = (i + j) / 2.0
                page_px = x_origin_px + mid_px
                cands.append(page_px / zoom)
            i = j
        return cands, smoothed

    # Aggregate (full page) col-mean for the cross-quarter summary.
    col_means = inv[(mast_px or 0):].mean(axis=0) if mast_px is not None \
        else inv.mean(axis=0)
    candidates, smoothed = _detect_in_cm(col_means)

    if return_profile:
        # Sample the aggregate smoothed profile.
        n_samples = min(200, W)
        step = max(1, W // n_samples)
        profile = [
            (px / zoom, float(smoothed[px]))
            for px in range(0, W, step)
        ]
        profile_y_range = (float(mast_y or 0), float(page_h))

        def _measure_regions(regions):
            """Run independent coarse pass for each (x0_px, y0_px,
            x1_px, y1_px) region. Returns list of dicts with bbox in
            page-pt coords + per-region profile + per-region candidates."""
            out = []
            for (x0_px, y0_px, x1_px, y1_px) in regions:
                if x1_px <= x0_px or y1_px <= y0_px:
                    continue
                region_cm = inv[y0_px:y1_px, x0_px:x1_px].mean(axis=0)
                region_cands, region_smoothed = _detect_in_cm(
                    region_cm, x_origin_px=x0_px,
                )
                # Sample the profile at ~1pt resolution — unified across
                # all regions and pages (per user direction 2026-05-18).
                rw = len(region_cm)
                if rw == 0:
                    continue
                rstep = max(1, int(round(zoom)))   # ≈ 1pt per sample
                samples = [
                    ((x0_px + px) / zoom, float(region_smoothed[px]))
                    for px in range(0, rw, rstep)
                ]
                out.append({
                    "x0_pt": x0_px / zoom,
                    "y0_pt": y0_px / zoom,
                    "x1_pt": x1_px / zoom,
                    "y1_pt": y1_px / zoom,
                    "profile": samples,
                    "candidates": region_cands,
                })
            return out

        quadrants_l1_data = _measure_regions(quadrants_l1)
        quadrants_l2_data = _measure_regions(quadrants_l2)
        segments_4x6_data = _measure_regions(segments_4x6)
        return (candidates, profile, profile_y_range,
                quadrants_l1_data, quadrants_l2_data, segments_4x6_data)
    return candidates


def fit_pitch_grid(coarse_l1, coarse_l2,
                    page_w_pt, text_area_left_pt=None,
                    text_area_right_pt=None):
    """Fit a regular column grid from the orange-dot positions across
    every coarse quadrant.

    No fixed thresholds: every parameter is derived from the data.

      1. Gather every profile point that's at or below the 5th
         percentile of its quadrant's margin-excluded inner values.
         Each such point is one "vote" for a gutter near that x.
      2. Cluster all votes by x. Bandwidth = 25th percentile of
         nearest-neighbour distances of the votes (a small fraction
         of the typical inter-cluster gap, so each visible cluster
         coalesces but distinct clusters stay separate).
      3. Compute adjacent-cluster differences. The smaller half of
         these is the modal pitch — the bigger half are gaps over
         missed gutters. Median of the smaller half = pitch.
      4. Anchor at the cluster with the most votes. Project the grid
         forward and backward by pitch increments inside the page's
         text area.
      5. Each projected gutter is CONFIRMED if a cluster falls within
         ¼ pitch of it, PROJECTED-ONLY otherwise.

    Returns a dict with `pitch`, `anchor`, `bandwidth`, `clusters`
    (each {x_pt, support}), and `gutters` (each {x_pt, confirmed,
    nearest_cluster_dist}). Returns None when there aren't enough
    votes to fit a grid.
    """
    inner_into_pt = 30.0
    x_lo = (text_area_left_pt + inner_into_pt) if text_area_left_pt is not None else 0.0
    x_hi = (text_area_right_pt - inner_into_pt) if text_area_right_pt is not None else float(page_w_pt)

    # 1. Gather ONE vote per (quadrant × detected gutter): the center
    # of each contiguous below-5th-percentile run within a quadrant's
    # inner range. Each vote also carries the run's width so we can
    # use the typical gutter width as our clustering bandwidth.
    votes = []   # list of (center_x, run_width)
    for region_list in (coarse_l1 or [], coarse_l2 or []):
        for region in region_list:
            prof = region.get("profile") or []
            if len(prof) < 5:
                continue
            inner = [(x, v) for (x, v) in prof if x_lo <= x <= x_hi]
            if len(inner) < 2:
                continue
            vals = np.array([v for (_, v) in inner])
            thr = float(np.percentile(vals, 5))
            n = len(inner)
            i = 0
            while i < n:
                if inner[i][1] > thr:
                    i += 1; continue
                j = i
                while j < n and inner[j][1] <= thr:
                    j += 1
                xs = [inner[k][0] for k in range(i, j)]
                if len(xs) >= 1:
                    center = float(np.mean(xs))
                    width = float(xs[-1] - xs[0]) if len(xs) > 1 else 1.0
                    votes.append((center, width))
                i = j

    if len(votes) < 6:
        return None

    # 2. Bandwidth: derive from the bimodal split in nearest-neighbour
    # distances between votes. Within-cluster NNs are small (a few pt
    # of cross-quadrant drift); between-cluster NNs are large (the
    # inter-gutter pitch, hundreds of pt). The biggest *ratio* jump in
    # the sorted NN list marks the natural break.
    sorted_centers = sorted(v[0] for v in votes)
    nn = sorted(b - a for a, b in zip(sorted_centers, sorted_centers[1:])
                if b > a)
    if len(nn) < 2:
        return None
    # Find the index in sorted nn where the ratio nn[i+1]/nn[i] is
    # maximised — that's the most pronounced break.
    eps = 0.5
    ratios = [nn[i + 1] / max(nn[i], eps) for i in range(len(nn) - 1)]
    if not ratios:
        return None
    break_idx = int(np.argmax(ratios))
    bw = float(nn[break_idx]) * 1.5   # bandwidth includes the largest
                                       # intra-cluster NN with a 1.5×
                                       # tolerance.
    if bw < 2.0:
        bw = 2.0

    # 3. Cluster votes by x. Each cluster is one detected gutter
    # supported by N quadrants (the size of the cluster).
    sorted_votes = sorted(votes, key=lambda v: v[0])
    clusters = []
    current = [sorted_votes[0][0]]
    for (cx, _w) in sorted_votes[1:]:
        if cx - current[-1] <= bw:
            current.append(cx)
        else:
            clusters.append((float(np.mean(current)), len(current)))
            current = [cx]
    clusters.append((float(np.mean(current)), len(current)))

    if len(clusters) < 3:
        return None

    cluster_xs = np.array([c[0] for c in clusters])
    cluster_w = np.array([c[1] for c in clusters])

    # 4. Pitch by mode-of-differences (your approach):
    #   (a) take consecutive differences between sorted vote x's
    #   (b) drop the tiny ones (within-cluster drift, ≤ bw)
    #   (c) round to a bucket so similar differences bin together
    #   (d) the mode of those rounded differences = pitch
    #
    # Physical constraint: a newspaper page has at most 8 columns
    # side by side, so the pitch is at least text-area-width / 8 — a
    # firm lower bound that rejects degenerate sub-pitch modes.
    sorted_votes_x = sorted(v[0] for v in votes)
    consecutive_diffs = [b - a for a, b in zip(sorted_votes_x,
                                                 sorted_votes_x[1:])
                         if b - a > 0]
    # Drop "tiny" (within-cluster) diffs — anything ≤ bw.
    medium_diffs = [d for d in consecutive_diffs if d > bw]
    if not medium_diffs:
        return None
    # Bucket size: a tenth of the median between-cluster diff. This
    # is data-derived — small enough to keep distinct pitches separate,
    # big enough that small jitter in the same physical pitch binds.
    bucket = max(2.0, float(np.median(medium_diffs)) / 10.0)
    text_area_w = max(1.0, x_hi - x_lo)
    min_pitch = text_area_w / 8.0   # ≥ 1 column out of max 8
    max_pitch = text_area_w          # ≤ the entire content width
    rounded = [round(d / bucket) * bucket for d in medium_diffs
               if min_pitch <= d <= max_pitch]
    if not rounded:
        # No diff in physical range — fall back to the smallest
        # acceptable diff that exists.
        rounded = [round(d / bucket) * bucket for d in medium_diffs
                   if d >= min_pitch]
    if not rounded:
        return None
    from collections import Counter
    counts = Counter(rounded)
    pitch = float(max(counts, key=lambda p: counts[p]))

    # Anchor at the cluster with the strongest support that LIES on
    # the discovered grid (i.e. some other cluster sits ~pitch away).
    # Falls back to the strongest cluster if no such alignment exists.
    anchor_idx = int(np.argmax(cluster_w))
    anchor = float(cluster_xs[anchor_idx])

    # Project — but no more than 8 columns means at most 7 inner
    # gutters within the text area. Cap projection accordingly.
    max_gutters = 7
    projected_xs = []
    x = anchor
    while x > x_lo and len(projected_xs) < max_gutters:
        projected_xs.append(x)
        x -= pitch
    x = anchor + pitch
    while x < x_hi and len(projected_xs) < max_gutters:
        projected_xs.append(x)
        x += pitch
    projected_xs.sort()

    # 5. Annotate each projected gutter with confirmation status.
    confirm_tol = pitch * 0.25
    annotated = []
    for px in projected_xs:
        nearest_idx = int(np.argmin(np.abs(cluster_xs - px)))
        dist = float(abs(cluster_xs[nearest_idx] - px))
        annotated.append({
            "x_pt": float(px),
            "confirmed": bool(dist <= confirm_tol),
            "nearest_cluster_x": float(cluster_xs[nearest_idx]),
            "nearest_cluster_dist": dist,
        })

    return {
        "pitch": pitch,
        "anchor": anchor,
        "bandwidth": bw,
        "clusters": [{"x_pt": float(x), "support": int(w)}
                      for (x, w) in zip(cluster_xs, cluster_w)],
        "gutters": annotated,
    }


def snap_obstacles_to_grid(layers):
    """Snap detected obstacle bboxes (ads, photos) to the structural
    grid: vertical gutters + horizontal whitespace bands + text-area
    edges + masthead bottom + page bottom.

    Per user direction 2026-05-18: the vertical grid is now reliable
    enough to use as a snap target so detected items align logically
    to the page's grid. Tolerance is ⅓ of the column pitch for x and
    a moderate constant for y (band positions are sparser).

    Returns a dict keyed by layer name (display_ads, photos) → list of
    snapped bboxes (x0, y0, x1, y1) in pt, in the same order as the
    input lists.
    """
    sg = layers.get("scored_grid") or {}
    grid_x_pts = [g["x_pt"] for g in (sg.get("estimated_gutters") or [])]
    pitch = sg.get("pitch") or 200.0
    bands = layers.get("whitespace_bands") or []
    grid_y_pts = []
    for (b_y0, b_y1) in bands:
        grid_y_pts.extend([float(b_y0), float(b_y1)])
    x_lo = layers.get("text_area_left_pt")
    x_hi = layers.get("text_area_right_pt")
    mast_y = layers.get("masthead_bottom") or 0.0
    page_size = layers.get("page_size") or [0.0, 0.0]
    page_h = float(page_size[1]) if len(page_size) > 1 else 0.0

    x_targets = sorted(set([float(x) for x in grid_x_pts]
                           + ([float(x_lo)] if x_lo is not None else [])
                           + ([float(x_hi)] if x_hi is not None else [])))
    y_targets = sorted(set(grid_y_pts
                           + ([float(mast_y)] if mast_y else [])
                           + ([page_h] if page_h else [])))

    # Conservative tolerance for ads/photos; more assertive for articles
    # (per user direction 2026-05-18: be assertive with article snap).
    tol_x = float(pitch) / 3.0
    tol_y = 30.0
    tol_x_assertive = float(pitch) / 2.0
    tol_y_assertive = 50.0

    def _snap(val, targets, tol):
        if not targets:
            return val
        nearest = min(targets, key=lambda t: abs(t - val))
        if abs(nearest - val) <= tol:
            return float(nearest)
        return float(val)

    def _snap_bbox(b, tx=tol_x, ty=tol_y):
        x0, y0, x1, y1 = b[0], b[1], b[2], b[3]
        return (_snap(x0, x_targets, tx),
                _snap(y0, y_targets, ty),
                _snap(x1, x_targets, tx),
                _snap(y1, y_targets, ty))

    out = {}
    for key in ("display_ads", "photos"):
        out[key] = [_snap_bbox(b) for b in (layers.get(key) or [])]
    # Articles get the assertive tolerance — they're our biggest items
    # and most worth aligning to the grid.
    out["articles"] = [
        _snap_bbox(a["bbox"], tx=tol_x_assertive, ty=tol_y_assertive)
        for a in (layers.get("articles") or [])
        if isinstance(a, dict) and "bbox" in a
    ]
    # Uncovered regions — orange "something to be matched here" boxes.
    # Also assertive snap; downstream they're candidates for article
    # detection refinements.
    out["uncovered_regions"] = [
        _snap_bbox(b, tx=tol_x_assertive, ty=tol_y_assertive)
        for b in (layers.get("uncovered_regions") or [])
    ]
    # Trim ads and articles back to the content area: x in
    # [text_area_left, text_area_right], y in [masthead_bottom, page_h].
    # text_area_top/bottom_pt from page_profile is the bbox of central
    # body text — too restrictive for trim bounds (it would clip out
    # legitimate top-and-bottom-of-page articles). Drop any item whose
    # trim collapses to zero area.
    mast_y_v = layers.get("masthead_bottom") or 0.0

    # Bottom footer margin: 20pt above page bottom. Newspapers
    # typically reserve a small footer band (folio + dateline).
    FOOTER_MARGIN_PT = 20.0

    def _trim_and_filter(bboxes):
        if x_lo is None or x_hi is None:
            return bboxes
        y_top = float(mast_y_v)
        y_bot = float(page_h or 1e6) - FOOTER_MARGIN_PT
        kept = []
        for b in bboxes:
            t = _trim_xy(b, float(x_lo), float(x_hi), y_top, y_bot)
            if t[2] > t[0] and t[3] > t[1]:
                kept.append(t)
        return kept

    out["display_ads"] = _trim_and_filter(out["display_ads"])
    out["articles"] = _trim_and_filter(out["articles"])
    # Post-snap dedup: drop nested items. If A contains B (with small
    # slack), drop B regardless of type.
    out = _drop_nested(out)
    return out


def _trim_x(bbox, x_lo, x_hi):
    """Clamp bbox x-range to [x_lo, x_hi]. Returns a new tuple."""
    x0, y0, x1, y1 = bbox
    return (max(float(x0), x_lo), float(y0),
            min(float(x1), x_hi), float(y1))


def _trim_xy(bbox, x_lo, x_hi, y_lo, y_hi):
    """Clamp bbox to [x_lo, x_hi] × [y_lo, y_hi]."""
    x0, y0, x1, y1 = bbox
    return (max(float(x0), x_lo), max(float(y0), y_lo),
            min(float(x1), x_hi), min(float(y1), y_hi))


def _bbox_contains(outer, inner, slack=2.0):
    return (outer[0] - slack <= inner[0]
            and outer[1] - slack <= inner[1]
            and outer[2] + slack >= inner[2]
            and outer[3] + slack >= inner[3])


def _drop_nested(groups):
    """Drop items whose bbox is wholly contained by another.

    Default rule: drop the smaller (contained) item.

    EXCEPTION (per user direction): when an article is contained
    inside a display_ad, drop the AD instead — that's the
    "boxed-article whose frame got detected as an ad" case. All other
    containment uses the default rule (ads can contain photos as
    part of their content; photos can contain captions; etc).
    """
    flat = []
    for key, items in groups.items():
        for idx, bbox in enumerate(items):
            t = tuple(bbox)
            area = max(0.0, (t[2] - t[0])) * max(0.0, (t[3] - t[1]))
            flat.append({"key": key, "bbox": t, "area": area})

    drop = set()
    n = len(flat)
    for i in range(n):
        if i in drop:
            continue
        for j in range(i + 1, n):
            if j in drop:
                continue
            ai, aj = flat[i], flat[j]
            i_in_j = _bbox_contains(aj["bbox"], ai["bbox"]) and ai["area"] < aj["area"]
            j_in_i = _bbox_contains(ai["bbox"], aj["bbox"]) and aj["area"] < ai["area"]
            if not (i_in_j or j_in_i):
                continue
            container, contained, c_idx, b_idx = (
                (aj, ai, j, i) if i_in_j else (ai, aj, i, j))
            # Exception: article inside ad → drop the ad
            if (contained["key"] == "articles"
                    and container["key"] == "display_ads"):
                drop.add(c_idx)
                if c_idx == i:
                    break
            else:
                drop.add(b_idx)
    rebuilt = {k: [] for k in groups}
    for i, item in enumerate(flat):
        if i not in drop:
            rebuilt[item["key"]].append(item["bbox"])
    return rebuilt


def _column_count_prior(cluster_x, x_lo, x_hi):
    """Small additive bonus when cluster x aligns with a 6/7/8-column
    grid hypothesis. Newspapers in this period are nearly always 6,
    7, or 8 columns — using that as a soft prior makes alignment
    with a plausible grid contribute toward a cluster's overall score.

    Priors per user direction 2026-05-18:
        6 columns → +0.5  (most common)
        7 columns → +0.1  (rarer)
        8 columns → +0.2  (next-most common after 6)

    Anchored at the text-area inner-left edge: gutter positions at
    x_lo + i * (text_area_w / n_cols) for i = 1..n_cols-1.
    """
    text_area_w = max(1.0, x_hi - x_lo)
    bonus = 0.0
    for (n_cols, weight) in ((6, 0.5), (7, 0.1), (8, 0.2)):
        col_w = text_area_w / n_cols
        tol = col_w * 0.10   # within 10% of a column width = aligned
        for i in range(1, n_cols):
            gutter_x = x_lo + i * col_w
            if abs(cluster_x - gutter_x) <= tol:
                bonus += weight
                break
    return bonus


def score_and_fit_from_segments(segments, ads_bboxes, page_w_pt,
                                  text_area_left_pt=None,
                                  text_area_right_pt=None):
    """Per-segment cluster scoring + page-wide grid fit.

    Scoring rules (per user direction 2026-05-18):
        +5: cluster of ≥3 dots with a flat-ish floor near zero (typical
            gutter)
        +4: cluster at the bottom of a big peak-to-trough drop
        -3: cluster INTERIOR to an ad (boundary clusters do count)
        -4: cluster INTERIOR to a photo (boundary clusters do count)

    Pitch: mode-of-differences across in-text-area clusters (same
    approach as fit_pitch_grid), capped by the "max 8 columns" bound.

    Center-out vertical analysis: starting from the cluster nearest the
    page centre, accumulate scores up and down at the same x (within ¼
    pitch). Then step outward by ±pitch alternately, repeating.

    Returns a dict with `pitch`, `clusters` (each annotated with score),
    `vertical_scores` (per x-column), and `estimated_gutters`.
    """
    inner_into_pt = 30.0
    x_lo = (text_area_left_pt + inner_into_pt) if text_area_left_pt is not None else 0.0
    x_hi = (text_area_right_pt - inner_into_pt) if text_area_right_pt is not None else float(page_w_pt)

    photos = []   # placeholder — caller passes only ads via ads_bboxes
                  # combined obstacles list. We don't separate here for
                  # the score; in-ad/in-photo distinction is hard to
                  # recover without separate lists, so we treat any
                  # obstacle interior as -3 (close enough for the
                  # ranking step).

    # 1. For each segment, find clusters of below-5th-percentile profile
    # points and score each cluster. Each segment also gets a
    # "text-likeness" weight (per user direction 2026-05-18 on image
    # contamination): a segment whose 5th-percentile floor sits close
    # to zero has a real gutter present (text-like); a photo segment's
    # floor stays mid-grey (its scores are damped accordingly). This
    # is data-derived — the floor's distance from zero IS the signal.
    all_clusters = []
    for seg_idx, seg in enumerate(segments or []):
        prof = seg.get("profile") or []
        if len(prof) < 5:
            continue
        inner = [(x, v) for (x, v) in prof if x_lo <= x <= x_hi]
        if len(inner) < 2:
            continue
        vals = np.array([v for (_, v) in inner])
        thr_low = float(np.percentile(vals, 5))
        peak = float(vals.max())
        y_center = (seg["y0_pt"] + seg["y1_pt"]) / 2.0
        # Text-likeness: 1.0 when the floor is at zero (clean gutter
        # present), trends to 0 as the floor rises toward the peak.
        if peak > 0:
            text_likeness = max(0.0, 1.0 - (thr_low / peak))
        else:
            text_likeness = 0.0

        # Find contiguous below-threshold runs within the segment
        n = len(inner)
        i = 0
        while i < n:
            if inner[i][1] > thr_low:
                i += 1; continue
            j = i
            while j < n and inner[j][1] <= thr_low:
                j += 1
            xs = [inner[k][0] for k in range(i, j)]
            vs = [inner[k][1] for k in range(i, j)]
            n_dots = len(xs)
            center = float(np.mean(xs))
            cluster_x_lo = float(min(xs)) if xs else center
            cluster_x_hi = float(max(xs)) if xs else center
            run_min = float(min(vs)) if vs else 0.0
            run_max = float(max(vs)) if vs else 0.0
            run_drop = peak - run_min       # max-to-trough drop
            # Width over which the run holds close to its own minimum
            flat_ratio = (run_max - run_min) / max(peak, 1.0)

            # Base score:
            #   +5 cluster of ≥3 dots with flat-ish floor
            #   +4 cluster at the bottom of a big drop
            score = 0.0
            if n_dots >= 3 and flat_ratio < 0.25:
                score = 5.0
            elif run_drop > 0.5 * peak:
                score = 4.0

            # Ad/photo interior penalty — but boundary OK. 6pt inside
            # the edge counts as interior.
            in_obstacle_score = 0.0
            for (ax0, ay0, ax1, ay1) in ads_bboxes or []:
                if (ax0 + 6 <= center <= ax1 - 6
                        and ay0 + 6 <= y_center <= ay1 - 6):
                    in_obstacle_score = -3.0
                    break
            score += in_obstacle_score

            # Column-count prior: small bonus for alignment with a
            # 6 / 7 / 8 column-grid hypothesis. Newspapers are 6/8/7
            # columns in that order of likelihood.
            score += _column_count_prior(center, x_lo, x_hi)

            # Ad/photo edge alignment (per user direction 2026-05-18):
            # if an obstacle edge falls just outside this cluster on
            # either side, the cluster is almost certainly a real
            # gutter — that obstacle started or ended at this column
            # boundary. Strong boost.
            edge_tol = 6.0
            edge_boost = 0.0
            for (ax0, ay0, ax1, ay1) in ads_bboxes or []:
                if ay1 <= seg["y0_pt"] or ay0 >= seg["y1_pt"]:
                    continue
                # Ad's left edge near the cluster's RIGHT side?
                # (ad sits in the column to the right of the gutter)
                if abs(ax0 - cluster_x_hi) <= edge_tol:
                    edge_boost = 6.0
                    break
                # Ad's right edge near the cluster's LEFT side?
                # (ad sits in the column to the left of the gutter)
                if abs(ax1 - cluster_x_lo) <= edge_tol:
                    edge_boost = 6.0
                    break
            score += edge_boost

            # Multiply by the segment's text-likeness — a photo
            # segment contributes far less, even if its profile
            # happens to dip in random places.
            score *= text_likeness

            all_clusters.append({
                "seg_idx": seg_idx,
                "x_pt": center,
                "x_lo": cluster_x_lo,
                "x_hi": cluster_x_hi,
                "y_center_pt": y_center,
                "n_dots": n_dots,
                "drop": run_drop,
                "flat_ratio": flat_ratio,
                "score": score,
                "in_obstacle": in_obstacle_score < 0,
            })
            i = j

    if not all_clusters:
        return None

    # 2. Estimate pitch via tolerant-GCD search on pairwise distances
    # (per user direction 2026-05-18). Real gutters lie at multiples
    # of pitch P, so every pairwise distance is k * P for some
    # positive integer k. We search candidate P values in the
    # physical range [text_area_w / 8, text_area_w / 2] (max 8 columns,
    # min 3) and pick the P that explains the most distances as
    # integer multiples within tolerance. Breaks ties using the 6/8/7
    # column prior.
    pitch_clusters = [c for c in all_clusters if not c["in_obstacle"]]
    if len(pitch_clusters) < 3:
        return None
    cluster_x_list = sorted(c["x_pt"] for c in pitch_clusters)
    text_area_w = max(1.0, x_hi - x_lo)

    distances = []
    for i in range(len(cluster_x_list)):
        for j in range(i + 1, len(cluster_x_list)):
            d = cluster_x_list[j] - cluster_x_list[i]
            distances.append(d)
    # Filter the trivial extremes: too small (within-cluster noise) and
    # too big (longer than the inner text area, can't be a pitch).
    distances = [d for d in distances if 30.0 < d < text_area_w]
    if not distances:
        return None

    min_pitch = text_area_w / 8.0
    max_pitch = text_area_w / 2.0   # at least 3 columns means pitch ≤ w/2

    # 1pt-step candidate pitches across the physical range.
    best_score = -1.0
    best_pitch = None
    for p_test_int in range(int(round(min_pitch)), int(round(max_pitch)) + 1):
        p_test = float(p_test_int)
        tol = p_test * 0.08   # 8% of pitch — accommodates a bit of drift
        explained = 0
        for d in distances:
            k = round(d / p_test)
            if k >= 1 and abs(d - k * p_test) <= tol:
                explained += 1
        score = explained / max(1, len(distances))
        # Column-count prior tiebreak: 6 cols (text_area_w / 6) preferred,
        # then 8, then 7. Quantify how close p_test is to each ideal pitch.
        prior_bonus = 0.0
        for (n_cols, w) in ((6, 0.5), (7, 0.1), (8, 0.2)):
            ideal_p = text_area_w / n_cols
            rel_off = abs(p_test - ideal_p) / ideal_p
            if rel_off < 0.10:
                prior_bonus += w * (1.0 - rel_off * 10)
                break
        total = score + prior_bonus * 0.01   # prior is a tiebreaker only
        if total > best_score:
            best_score = total
            best_pitch = p_test
    if best_pitch is None:
        return None
    pitch = float(best_pitch)

    # 3. Centre-out vertical column analysis.
    # Group clusters by x-bin of width pitch/4 — these are putative
    # columns. For each column, sum the scores across all its clusters
    # (vertical accumulation, "above and below at same x").
    bin_size = pitch / 4.0
    page_centre_x = (x_lo + x_hi) / 2.0
    page_centre_y = (
        max(s["y0_pt"] for s in segments) + min(s["y0_pt"] for s in segments)
    ) / 2.0 if segments else 0.0

    # Map each cluster to a bin keyed by round(x_pt / bin_size)
    bins = {}   # bin_key -> {centre_x, total_score, members}
    for c in all_clusters:
        bin_key = int(round(c["x_pt"] / bin_size))
        if bin_key not in bins:
            bins[bin_key] = {"centre_x": c["x_pt"],
                              "total_score": 0,
                              "members": []}
        bins[bin_key]["members"].append(c)
        bins[bin_key]["total_score"] += c["score"]
        # Centre-of-mass: average of cluster x's
        bins[bin_key]["centre_x"] = float(np.mean(
            [m["x_pt"] for m in bins[bin_key]["members"]]
        ))

    # 4. Try MANY anchors — centre-out, left-to-right, right-to-left,
    # and every positive-score bin as a potential anchor. For each,
    # project the grid by pitch and sum the supporting bin scores.
    # The anchor whose projection captures the highest total score
    # wins. An image-heavy middle no longer corrupts the result —
    # an edge anchor will outscore it.
    max_gutters = 7
    quarter_pitch = pitch / 4.0

    # Geometric validity: a gutter at x requires a full column-width
    # of room (with some slack) between it and each text-area edge,
    # otherwise it can't be a real inter-column boundary. Per user
    # direction 2026-05-18: a "gutter" 60pt from the page edge when
    # pitch is 220pt is geometrically impossible.
    edge_safe_lo = x_lo + pitch * 0.6
    edge_safe_hi = x_hi - pitch * 0.6

    def _walk_from_anchor(anchor_x):
        accepted = []
        for direction in (-1, 1):
            x = anchor_x if direction == -1 else anchor_x + pitch
            steps = 0
            while x_lo < x < x_hi and steps < max_gutters:
                if not (edge_safe_lo <= x <= edge_safe_hi):
                    x += direction * pitch
                    steps += 1
                    continue
                # Find the best-scoring bin within ¼ pitch
                best_local = None
                for b in bins.values():
                    if abs(b["centre_x"] - x) <= quarter_pitch:
                        if (best_local is None
                                or b["total_score"] > best_local["total_score"]):
                            best_local = b
                if best_local and best_local["total_score"] > 0:
                    accepted.append(best_local["centre_x"])
                x += direction * pitch
                steps += 1
        return sorted(set(accepted))

    anchor_candidates = []
    # Centre-out: nearest-to-centre bin
    centre_sorted = sorted(bins.values(),
                            key=lambda b: abs(b["centre_x"] - page_centre_x))
    if centre_sorted:
        anchor_candidates.append(("centre", centre_sorted[0]["centre_x"]))
    # Left-to-right: leftmost positive-score bin
    pos_bins = [b for b in bins.values() if b["total_score"] > 0]
    if pos_bins:
        leftmost = min(pos_bins, key=lambda b: b["centre_x"])
        rightmost = max(pos_bins, key=lambda b: b["centre_x"])
        strongest = max(pos_bins, key=lambda b: b["total_score"])
        anchor_candidates.append(("left", leftmost["centre_x"]))
        anchor_candidates.append(("right", rightmost["centre_x"]))
        anchor_candidates.append(("strongest", strongest["centre_x"]))

    best_total = -1.0
    best_run = []
    best_strategy = None
    for (strategy, anchor_x) in anchor_candidates:
        run = _walk_from_anchor(anchor_x)
        # Total = sum of scores of supporting bins
        run_total = 0.0
        for x in run:
            for b in bins.values():
                if abs(b["centre_x"] - x) < 1.0:
                    run_total += b["total_score"]
                    break
        if run_total > best_total:
            best_total = run_total
            best_run = run
            best_strategy = strategy

    estimated_gutters = []
    for x in best_run:
        for b in bins.values():
            if abs(b["centre_x"] - x) < 1.0:
                estimated_gutters.append({
                    "x_pt": float(x),
                    "score": float(b["total_score"]),
                    "members": len(b["members"]),
                })
                break

    # 5. Line-position refinement step (disabled in pipeline per user
    # direction 2026-05-18; kept here for potential future use). The
    # vertical grid from steps 1–4 is the canonical output. To re-
    # enable, restore the block below.
    #
    #   refine_window = pitch * 0.25
    #   cluster_half_widths = [(c["x_hi"] - c["x_lo"]) / 2.0
    #                           for c in all_clusters
    #                           if c["x_hi"] > c["x_lo"]]
    #   half_gutter_w = float(np.median(cluster_half_widths)) \
    #       if cluster_half_widths else 4.0
    #   half_gutter_w = max(2.0, min(15.0, half_gutter_w))
    #   refined_gutters = []
    #   for g in estimated_gutters:
    #       g_x = g["x_pt"]
    #       evidence = []
    #       for c in all_clusters:
    #           if abs(c["x_pt"] - g_x) <= refine_window and c["score"] > 0:
    #               evidence.append((c["x_pt"], c["score"]))
    #       for (ax0, ay0, ax1, ay1) in ads_bboxes or []:
    #           if abs(ax0 - g_x) <= refine_window:
    #               evidence.append((float(ax0) - half_gutter_w, 4.0))
    #           if abs(ax1 - g_x) <= refine_window:
    #               evidence.append((float(ax1) + half_gutter_w, 4.0))
    #       if evidence:
    #           total_w = sum(w for (_, w) in evidence)
    #           new_x = sum(x * w for (x, w) in evidence) / total_w
    #           refined_gutters.append({"x_pt": float(new_x),
    #                                    "score": g["score"],
    #                                    "members": g["members"],
    #                                    "original_x_pt": g_x})
    #       else:
    #           refined_gutters.append(g)
    #   estimated_gutters = refined_gutters

    return {
        "pitch": pitch,
        "n_segments": len(segments) if segments else 0,
        "clusters": all_clusters,
        "bins": [{"x_pt": b["centre_x"],
                   "score": float(b["total_score"]),
                   "n_members": len(b["members"])}
                  for b in sorted(bins.values(), key=lambda b: b["centre_x"])],
        "estimated_gutters": estimated_gutters,
        "anchor_strategy": best_strategy,
    }


def _band_weights(bands, page_w):
    """Each band's vote weight — 2 if it spans ≥80% of page width, else 1."""
    if not bands or not page_w:
        return None
    weights = []
    for b in bands:
        x0 = b.x0_pt if b.x0_pt is not None else 0.0
        x1 = b.x1_pt if b.x1_pt is not None else page_w
        weights.append(2 if (x1 - x0) >= 0.80 * page_w else 1)
    return weights


def _summarise_cluster(cluster, n_bands, band_weights=None,
                       coarse_candidates=None, tol_pt=GRID_CLUSTER_TOL_PT):
    """Score-based confidence: sum per-detection quality scores
    (0..1 each), weight by band width, add a coarse-alignment bonus.

    A strong single-band detection (flat-zero + strong flanks, full-
    width band) scores ~1.5 by itself → medium. Two strong detections
    or one strong + coarse alignment → high. Many weak detections
    (high-grey-base "valleys") only compound slowly.

    Position is computed by quality-weighted mean of detections, so
    high-quality observations pull the centre toward the true gutter.
    """
    xs = [o[0] for o in cluster]
    qualities = [o[1] for o in cluster]
    band_idxs = [o[2] for o in cluster]
    bands_found = sorted(set(band_idxs))

    # Sum quality scores, scaled by band width (full-width band ×1.5
    # because its measurement spans a larger evidence range).
    total_evidence = 0.0
    for (x_pt, q, bi) in cluster:
        w_mult = (band_weights[bi] / 2.0 + 0.5) if band_weights else 1.0
        # band_weights gives 2 for full-width, 1 for narrow → 1.5 and 1.0
        total_evidence += q * w_mult

    # Quality-weighted position
    if sum(qualities) > 0:
        cluster_x = float(sum(x * q for (x, q) in zip(xs, qualities))
                          / sum(qualities))
    else:
        cluster_x = float(np.mean(xs))

    # Coarse alignment bonus: corroboration from independent full-page
    # measurement. +0.5 to total evidence — significant but not enough
    # alone to qualify (need at least one refined detection too).
    aligned_coarse = False
    if coarse_candidates:
        if any(abs(cluster_x - cx) <= tol_pt for cx in coarse_candidates):
            total_evidence += 0.5
            aligned_coarse = True

    # Score-based confidence categorisation. Tuned so a single
    # near-perfect detection (quality ≈ 1.0) from a full-width band
    # (× 1.5) hits "medium" by itself, and two such detections push
    # to "high".
    if total_evidence >= 2.5:
        conf = "high"
    elif total_evidence >= 1.2:
        conf = "medium"
    else:
        conf = "low"

    return GridBoundary(
        x_pt=cluster_x,
        confidence=conf,
        vote_count=int(round(total_evidence * 10)),  # keep field for JSON,
                                                     # encode score × 10
        bands_found=bands_found,
    )


def find_column_grid(page, masthead_bottom, whitespace_bands, ads_bboxes,
                     dark_thr=130, return_profiles=False,
                     text_area_left_pt=None, text_area_right_pt=None):
    """Detect the column grid for a modular page.

    Two-stage pipeline:
      1. Coarse first-pass over the full page (no obstacle awareness)
         finds candidate axes from the paper's underlying grid.
      2. Refined per-band pass measures clean x-zones and clear-run
         y-zones. The consensus step boosts clusters that align with
         a coarse candidate (+1 vote), so real page-grid gutters that
         the coarse pass also saw get pushed past the high-confidence
         threshold while locally-significant gutters that the coarse
         pass missed are demoted.

    Returns:
      `(bands, grid, coarse)` always, plus `profiles` as a 4th element
      when return_profiles=True.
    """
    page_h = page.rect.height
    page_w = page.rect.width
    (coarse, coarse_profile, coarse_y_range,
     coarse_l1, coarse_l2, coarse_4x6) = coarse_axis_candidates(
        page, masthead_bottom, page_w, page_h,
        dark_thr=dark_thr, return_profile=True,
        text_area_left_pt=text_area_left_pt,
        text_area_right_pt=text_area_right_pt,
    )
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
    grid = consensus_grid(per_band, bands=bands, page_w=page_w,
                          coarse_candidates=coarse)
    pitch_grid = fit_pitch_grid(
        coarse_l1, coarse_l2, page_w,
        text_area_left_pt=text_area_left_pt,
        text_area_right_pt=text_area_right_pt,
    )
    # Per-segment scoring & center-out grid analysis on the 4×6 grid.
    scored_grid = score_and_fit_from_segments(
        coarse_4x6, ads_bboxes, page_w,
        text_area_left_pt=text_area_left_pt,
        text_area_right_pt=text_area_right_pt,
    )
    coarse_pack = {
        "candidates": coarse,
        "profile": coarse_profile,
        "y_range": coarse_y_range,
        "quadrants_l1": coarse_l1,
        "quadrants_l2": coarse_l2,
        "segments_4x6": coarse_4x6,
        "pitch_grid": pitch_grid,
        "scored_grid": scored_grid,
    }
    if return_profiles:
        return bands, grid, coarse_pack, profiles
    return bands, grid, coarse_pack
