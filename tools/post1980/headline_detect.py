"""Pixel-based headline detection for post-1980 newspaper pages.

The OCR text layer on Adobe Paper Capture PDFs is unreliable — on the
1995-10-18 sample the headline 'Student of James Naismith visits town
to pay tribute' came through as 'town to. visits pay .' with a bbox
that covered only ~2 of the 3 visual columns. That breaks the
headline-aware article-growth rule in resolution.

This detector works on the rendered raster directly. It finds
connected components of dark pixels whose vertical extent is at least
1.5× the body font size — i.e. headline characters — and clusters
them into headline runs (multi-line headlines become one run).

The output is intended to AUGMENT the OCR-derived headlines: each
article's headline bbox can be widened to match the pixel detector's
extent when the OCR version is shorter than the visual reality.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional

import fitz
import numpy as np

try:
    from scipy.ndimage import label as _cc_label
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


@dataclass
class PixelHeadlineRun:
    """A detected headline region from pixel analysis."""
    x0: float
    y0: float
    x1: float
    y1: float
    char_height_pt: float    # estimated character height
    n_chars: int             # number of tall connected components
    n_lines: int             # estimated line count


def detect_headline_runs(page,
                          body_size_pt: float,
                          mast_y: float,
                          page_h: float,
                          dpi: int = 100,
                          dark_thr: int = 130,
                          text_area_left_pt: Optional[float] = None,
                          text_area_right_pt: Optional[float] = None,
                          min_size_factor: float = 1.4,
                          footer_margin_pt: float = 20.0,
                          ) -> List[PixelHeadlineRun]:
    """Detect headline runs by pixel analysis.

    Args:
        page: fitz.Page object
        body_size_pt: body font size in pt (from body_font_size)
        mast_y: masthead bottom in pt — content below this is scanned
        page_h: page height in pt
        dpi: render resolution (100 is fine for headline-scale features)
        dark_thr: pixel < this is "ink"
        text_area_left_pt / right_pt: horizontal text-area edges in pt
        min_size_factor: a component qualifies as a headline character
            when its height ≥ body_size_pt × this factor. 1.4 is loose
            enough for sub-headlines, tight enough to exclude descenders
            and body-line wraps.
        footer_margin_pt: skip the bottom-margin band

    Returns:
        List of PixelHeadlineRun in y-order.
    """
    if not _HAS_SCIPY:
        return []
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    H, W = img.shape

    # Restrict scan window: below masthead, above footer, inside text area
    y_lo = max(0, int(round((mast_y or 0) * zoom)))
    y_hi = min(H, int(round((page_h - footer_margin_pt) * zoom)))
    x_lo = (0 if text_area_left_pt is None
             else max(0, int(round(text_area_left_pt * zoom))))
    x_hi = (W if text_area_right_pt is None
             else min(W, int(round(text_area_right_pt * zoom))))
    if y_hi <= y_lo or x_hi <= x_lo:
        return []

    region = img[y_lo:y_hi, x_lo:x_hi]
    dark = region < dark_thr

    labels, n = _cc_label(dark)
    if n == 0:
        return []

    # Use np.unique to get pixel counts per label efficiently
    flat_labels = labels.ravel()
    sizes = np.bincount(flat_labels)

    # Per-component bbox via argmin/argmax on coords
    # (scipy.ndimage.find_objects gives us slices for free)
    from scipy.ndimage import find_objects
    slices = find_objects(labels)

    body_h_px = max(1.0, body_size_pt * zoom)
    headline_thr_px = body_h_px * min_size_factor

    headline_chars = []
    for lbl_idx, sl in enumerate(slices):
        if sl is None:
            continue
        ys, xs = sl
        h_px = ys.stop - ys.start
        w_px = xs.stop - xs.start
        if h_px < headline_thr_px:
            continue
        # Reject very wide, very thin or very thick solid bars (rule
        # lines, image edges). A real headline character has roughly
        # similar width and height — aspect ratio between 0.1 and 6.
        aspect = w_px / max(1, h_px)
        if aspect > 6.0 or aspect < 0.10:
            continue
        # Reject very dense components (solid black rectangles)
        comp_pixels = sizes[lbl_idx + 1]
        bbox_area = h_px * w_px
        density = comp_pixels / max(1, bbox_area)
        if density > 0.85:
            continue
        # Reject very tall components (whole-page rules, illustrations)
        if h_px > body_h_px * 6:
            continue
        headline_chars.append({
            "y0_px": ys.start,
            "y1_px": ys.stop,
            "x0_px": xs.start,
            "x1_px": xs.stop,
            "h_px": h_px,
            "cx_px": (xs.start + xs.stop) / 2.0,
            "cy_px": (ys.start + ys.stop) / 2.0,
        })

    if not headline_chars:
        return []

    # ----- Group characters into headline runs ---------------------------
    # First group into LINES: characters whose y-overlap is significant.
    line_band = body_h_px * 0.6   # half a body line = same y-row
    headline_chars.sort(key=lambda c: c["cy_px"])
    lines: List[List[dict]] = []
    for c in headline_chars:
        attached = False
        for ln in lines:
            ln_cy = np.mean([m["cy_px"] for m in ln])
            ln_h = np.mean([m["h_px"] for m in ln])
            if abs(c["cy_px"] - ln_cy) < line_band + ln_h * 0.3:
                ln.append(c)
                attached = True
                break
        if not attached:
            lines.append([c])

    # Within each line, characters that are far apart horizontally form
    # SEPARATE headlines (e.g. two parallel headlines side by side).
    # Split each line by horizontal proximity.
    inter_char_max = body_h_px * 2.5   # headline letter-space cutoff
    line_runs: List[List[dict]] = []
    for ln in lines:
        ln.sort(key=lambda c: c["x0_px"])
        current = [ln[0]]
        for c in ln[1:]:
            prev_x1 = max(m["x1_px"] for m in current)
            if c["x0_px"] - prev_x1 <= inter_char_max:
                current.append(c)
            else:
                line_runs.append(current)
                current = [c]
        line_runs.append(current)

    # Now merge line-runs into multi-line headline blocks:
    # consecutive line-runs whose x-extents overlap by ≥ 40% AND whose
    # y-gap is ≤ 1.5 × headline character height belong to the same
    # headline.
    line_runs.sort(key=lambda lr: min(c["cy_px"] for c in lr))
    blocks: List[List[List[dict]]] = []
    for lr in line_runs:
        lr_x0 = min(c["x0_px"] for c in lr)
        lr_x1 = max(c["x1_px"] for c in lr)
        lr_y0 = min(c["y0_px"] for c in lr)
        lr_h = float(np.median([c["h_px"] for c in lr]))
        attached = False
        for blk in blocks:
            prev_lr = blk[-1]
            p_x0 = min(c["x0_px"] for c in prev_lr)
            p_x1 = max(c["x1_px"] for c in prev_lr)
            p_y1 = max(c["y1_px"] for c in prev_lr)
            ov = max(0, min(lr_x1, p_x1) - max(lr_x0, p_x0))
            min_w = min(lr_x1 - lr_x0, p_x1 - p_x0)
            ratio = ov / max(1, min_w)
            y_gap = lr_y0 - p_y1
            if ratio >= 0.4 and y_gap <= lr_h * 1.5:
                blk.append(lr)
                attached = True
                break
        if not attached:
            blocks.append([lr])

    runs: List[PixelHeadlineRun] = []
    for blk in blocks:
        all_chars = [c for lr in blk for c in lr]
        x0 = float(min(c["x0_px"] for c in all_chars) + x_lo) / zoom
        x1 = float(max(c["x1_px"] for c in all_chars) + x_lo) / zoom
        y0 = float(min(c["y0_px"] for c in all_chars) + y_lo) / zoom
        y1 = float(max(c["y1_px"] for c in all_chars) + y_lo) / zoom
        char_h_pt = float(np.median([c["h_px"] for c in all_chars])) / zoom
        runs.append(PixelHeadlineRun(
            x0=x0, y0=y0, x1=x1, y1=y1,
            char_height_pt=char_h_pt,
            n_chars=len(all_chars),
            n_lines=len(blk),
        ))
    runs.sort(key=lambda r: r.y0)
    return runs


@dataclass
class PixelPhoto:
    """A detected photo region from pixel analysis."""
    x0: float
    y0: float
    x1: float
    y1: float
    density: float
    row_std: float


def detect_pixel_photos(page,
                         mast_y: float,
                         page_h: float,
                         page_w: float,
                         claimed_bboxes=None,
                         dpi: int = 100,
                         tile_size_pt: float = 30.0,
                         min_tile_density: float = 0.45,
                         min_size_pt: float = 80.0,
                         footer_margin_pt: float = 20.0,
                         text_area_left_pt: Optional[float] = None,
                         text_area_right_pt: Optional[float] = None,
                         ) -> List[PixelPhoto]:
    """Detect photo regions from the rendered raster via tile-density.

    Algorithm: divide the page into 30pt tiles, measure dark-pixel
    density per tile, mark high-density tiles (>0.45) as photo-like,
    cluster contiguous photo tiles. Tile granularity sidesteps the
    "text regions look dense too" problem because text has dense
    rows alternating with empty rows — average density per 30pt tile
    is moderate (~0.25). Real photo tiles average ≥ 0.45.

    Args:
        claimed_bboxes: regions already classified — excluded from
            photo detection (snapped article/ad bboxes).

    Returns list of PixelPhoto, sorted by y.
    """
    if not _HAS_SCIPY:
        return []
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width).copy()
    H, W = img.shape
    dark = img < 130

    # Search window
    y_lo = max(0, int(round((mast_y or 0) * zoom)))
    y_hi = min(H, int(round((page_h - footer_margin_pt) * zoom)))
    x_lo = (0 if text_area_left_pt is None
             else max(0, int(round(text_area_left_pt * zoom))))
    x_hi = (W if text_area_right_pt is None
             else min(W, int(round(text_area_right_pt * zoom))))

    # Tile size in pixels
    tile_px = max(8, int(round(tile_size_pt * zoom)))
    # Number of full tiles within the search window
    n_ty = (y_hi - y_lo) // tile_px
    n_tx = (x_hi - x_lo) // tile_px
    if n_ty < 2 or n_tx < 2:
        return []

    # Per-tile density (fraction of dark pixels)
    tile_density = np.zeros((n_ty, n_tx), dtype=np.float32)
    for ty in range(n_ty):
        py0 = y_lo + ty * tile_px
        py1 = py0 + tile_px
        for tx in range(n_tx):
            px0 = x_lo + tx * tile_px
            px1 = px0 + tile_px
            tile_density[ty, tx] = float(dark[py0:py1, px0:px1].mean())

    photo_mask = tile_density > min_tile_density

    # Mask out claimed regions at the tile level (any tile whose
    # centre falls inside a claimed bbox is excluded).
    if claimed_bboxes:
        for (cx0, cy0, cx1, cy1) in claimed_bboxes:
            for ty in range(n_ty):
                py = y_lo + (ty + 0.5) * tile_px
                if py < cy0 or py > cy1:
                    continue
                for tx in range(n_tx):
                    px = x_lo + (tx + 0.5) * tile_px
                    if px < cx0 or px > cx1:
                        continue
                    photo_mask[ty, tx] = False

    labels, n = _cc_label(photo_mask)
    if n == 0:
        return []

    from scipy.ndimage import find_objects
    slices = find_objects(labels)
    min_tiles_per_dim = max(2, int(round(min_size_pt / tile_size_pt)))

    photos: List[PixelPhoto] = []
    for lbl_idx, sl in enumerate(slices):
        if sl is None:
            continue
        tys, txs = sl
        n_th = tys.stop - tys.start
        n_tw = txs.stop - txs.start
        if n_th < min_tiles_per_dim or n_tw < min_tiles_per_dim:
            continue
        # Convert tile coords back to pixel/pt coords
        py0 = y_lo + tys.start * tile_px
        py1 = y_lo + tys.stop * tile_px
        px0 = x_lo + txs.start * tile_px
        px1 = x_lo + txs.stop * tile_px
        # Stats on the actual pixel region
        region = dark[py0:py1, px0:px1]
        density = float(region.mean())
        row_density = region.mean(axis=1)
        row_std = float(row_density.std())
        photos.append(PixelPhoto(
            x0=px0 / zoom, y0=py0 / zoom,
            x1=px1 / zoom, y1=py1 / zoom,
            density=density, row_std=row_std,
        ))
    photos.sort(key=lambda p: p.y0)
    return photos


@dataclass
class ClosedRectangle:
    """A rectangle formed by 4 rules (top, bottom, left, right)."""
    x0_pt: float
    y0_pt: float
    x1_pt: float
    y1_pt: float
    border_thickness_pt: float


def _find_long_h_runs(dark, min_run_px):
    """For each row, find ALL contiguous dark runs at or above
    min_run_px. Returns list of (y, x_lo, x_hi, run_len) tuples.
    Multiple side-by-side bordered items can produce multiple long
    runs in the same row — keep all of them, not just the longest."""
    H, W = dark.shape
    out = []
    for y in range(H):
        row = dark[y]
        padded = np.concatenate(([False], row, [False]))
        edges = np.diff(padded.astype(np.int8))
        starts = np.where(edges == 1)[0]
        ends = np.where(edges == -1)[0]
        if len(starts) == 0:
            continue
        run_lens = ends - starts
        for s, e, ln in zip(starts, ends, run_lens):
            if ln >= min_run_px:
                out.append((y, int(s), int(e), int(ln)))
    return out


def _find_long_v_runs(dark, min_run_px):
    """All vertical runs ≥ min_run_px per column (multiple stacked
    bordered items can produce multiple long runs in the same x)."""
    H, W = dark.shape
    out = []
    for x in range(W):
        col = dark[:, x]
        padded = np.concatenate(([False], col, [False]))
        edges = np.diff(padded.astype(np.int8))
        starts = np.where(edges == 1)[0]
        ends = np.where(edges == -1)[0]
        if len(starts) == 0:
            continue
        run_lens = ends - starts
        for s, e, ln in zip(starts, ends, run_lens):
            if ln >= min_run_px:
                out.append((x, int(s), int(e), int(ln)))
    return out


def _group_lines(runs, max_thickness_px=10, gap_tolerance=3,
                  min_overlap_frac: float = 0.5):
    """Group runs (perp_idx, lo, hi, length) into rules.

    Two runs join the same group iff:
      - their perpendicular indices are within gap_tolerance (e.g.
        adjacent or near-adjacent rows for a horizontal rule), AND
      - their [lo, hi] ranges overlap by at least min_overlap_frac of
        the shorter range (so two horizontal rules at the same y but
        in different x-ranges become separate rules).

    Returns list of (perp_start, perp_end, lo, hi) tuples for groups
    whose thickness (perp_end - perp_start + 1) ≤ max_thickness_px.
    """
    if not runs:
        return []
    # Sort by perp_idx, then lo
    sorted_runs = sorted(runs, key=lambda r: (r[0], r[1]))
    # Open groups: dicts with perp_start, perp_end, lo, hi, last_perp.
    open_groups: list = []
    closed: list = []

    def _overlap_ok(a_lo, a_hi, b_lo, b_hi):
        ov = min(a_hi, b_hi) - max(a_lo, b_lo)
        if ov <= 0:
            return False
        shorter = min(a_hi - a_lo, b_hi - b_lo)
        if shorter <= 0:
            return False
        return ov >= shorter * min_overlap_frac

    for perp, lo, hi, _ln in sorted_runs:
        # Close groups whose last_perp is too far behind
        still_open = []
        for g in open_groups:
            if perp - g["last_perp"] > gap_tolerance:
                closed.append(g)
            else:
                still_open.append(g)
        open_groups = still_open
        # Find a matching open group
        joined = False
        for g in open_groups:
            if _overlap_ok(g["lo"], g["hi"], lo, hi):
                g["perp_end"] = max(g["perp_end"], perp)
                g["last_perp"] = perp
                g["lo"] = min(g["lo"], lo)
                g["hi"] = max(g["hi"], hi)
                joined = True
                break
        if not joined:
            open_groups.append({
                "perp_start": perp, "perp_end": perp,
                "last_perp": perp, "lo": lo, "hi": hi,
            })

    closed.extend(open_groups)
    out = []
    for g in closed:
        thickness = g["perp_end"] - g["perp_start"] + 1
        if thickness <= max_thickness_px:
            out.append((g["perp_start"], g["perp_end"], g["lo"], g["hi"]))
    return out


def _longest_true_run(bool_arr: np.ndarray) -> int:
    """Longest contiguous True run in a 1-D boolean array."""
    if bool_arr.size == 0:
        return 0
    padded = np.concatenate(([False], bool_arr, [False]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.where(edges == 1)[0]
    ends = np.where(edges == -1)[0]
    if len(starts) == 0:
        return 0
    return int((ends - starts).max())


def _h_rule_has_gap_both_sides(dark, h_rule, probe_depth_px,
                                  max_dark_frac, probe_offset_px: int = 0):
    """A horizontal rule is a frame edge only if a thin strip
    above the rule AND a thin strip below the rule are mostly light
    (paper). A photo top/bottom edge has dark content butting against
    the rule on at least one side.

    probe_offset_px shifts the strips AWAY from the rule by that many
    rows. Use this when the rule was found in a dilated mask so the
    actual line sits a few rows inside the rule's nominal y-range —
    set probe_offset_px = dilation_radius so the probe strip lands on
    paper, not on the line itself.
    """
    y_start, y_end, x_lo, x_hi = h_rule
    H, W = dark.shape
    above_hi = max(0, y_start - probe_offset_px)
    above_lo = max(0, above_hi - probe_depth_px)
    below_lo = min(H, y_end + 1 + probe_offset_px)
    below_hi = min(H, below_lo + probe_depth_px)
    x_lo_c = max(0, x_lo)
    x_hi_c = min(W, x_hi + 1)
    if above_hi - above_lo <= 0 or below_hi - below_lo <= 0:
        return False
    if x_hi_c - x_lo_c <= 0:
        return False
    above = dark[above_lo:above_hi, x_lo_c:x_hi_c]
    below = dark[below_lo:below_hi, x_lo_c:x_hi_c]
    above_frac = above.mean() if above.size else 1.0
    below_frac = below.mean() if below.size else 1.0
    return above_frac <= max_dark_frac and below_frac <= max_dark_frac


def _v_rule_has_gap_both_sides(dark, v_rule, probe_depth_px,
                                  max_dark_frac, probe_offset_px: int = 0):
    """Mirror of _h_rule_has_gap_both_sides for vertical rules."""
    x_start, x_end, y_lo, y_hi = v_rule
    H, W = dark.shape
    left_hi = max(0, x_start - probe_offset_px)
    left_lo = max(0, left_hi - probe_depth_px)
    right_lo = min(W, x_end + 1 + probe_offset_px)
    right_hi = min(W, right_lo + probe_depth_px)
    y_lo_c = max(0, y_lo)
    y_hi_c = min(H, y_hi + 1)
    if left_hi - left_lo <= 0 or right_hi - right_lo <= 0:
        return False
    if y_hi_c - y_lo_c <= 0:
        return False
    left = dark[y_lo_c:y_hi_c, left_lo:left_hi]
    right = dark[y_lo_c:y_hi_c, right_lo:right_hi]
    left_frac = left.mean() if left.size else 1.0
    right_frac = right.mean() if right.size else 1.0
    return left_frac <= max_dark_frac and right_frac <= max_dark_frac


def find_closed_rectangles(page, mast_y, page_h, page_w,
                             column_grid=None,
                             text_area_left_pt: float = None,
                             text_area_right_pt: float = None,
                             dpi: int = 300,
                             dark_thr: int = 180,
                             min_dim_pt: float = 50.0,
                             footer_margin_pt: float = 20.0,
                             ) -> List[ClosedRectangle]:
    """Find all closed rectangles formed by rules in the rendered
    raster. Pairs h_rules (top + bottom) with v_rules at matching
    corner x-positions. Uses wobble-tolerant pre-dilation so wobbly
    scans still produce contiguous detected rules.

    column_grid (if provided) makes the rule grouping aware of the
    column boundaries: collinear h_rule segments separated by a small
    x-gap are merged when the gap straddles a grid anchor (broken-up
    top rules of bordered articles look like this — e.g. text touching
    the rule at a column boundary cuts the rule into pieces).
    """
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    H, W = img.shape
    dark = img < dark_thr

    mast_slack_pt = 12.0
    y_lo = max(0, int(round(((mast_y or 0) - mast_slack_pt) * zoom)))
    y_hi = min(H, int(round((page_h - footer_margin_pt) * zoom)))
    min_run_px = max(20, int(min_dim_pt * zoom))

    # Wobble-tolerant pre-dilation: scans wobble by ~0.3° locally,
    # spreading a horizontal rule across ~5 px vertically across a
    # 1000-px-wide box. Dilate perpendicular to each axis before
    # finding long runs.
    from scipy.ndimage import binary_dilation
    skew_r = max(2, int(zoom * 1.0))
    v_struct = np.ones((2 * skew_r + 1, 1), dtype=bool)
    h_struct = np.ones((1, 2 * skew_r + 1), dtype=bool)
    dark_for_h = binary_dilation(dark[y_lo:y_hi], structure=v_struct)
    dark_for_v = binary_dilation(dark[y_lo:y_hi], structure=h_struct)

    h_runs = _find_long_h_runs(dark_for_h, min_run_px)
    h_runs = [(r[0] + y_lo, r[1], r[2], r[3]) for r in h_runs]
    v_runs = _find_long_v_runs(dark_for_v, min_run_px)
    v_runs = [(r[0], r[1] + y_lo, r[2] + y_lo, r[3]) for r in v_runs]

    max_thickness_px = max(6, int(zoom * 6)) + 2 * skew_r
    gap_tolerance = max(3, int(zoom * 2))
    h_rules = _group_lines(h_runs, max_thickness_px=max_thickness_px,
                            gap_tolerance=gap_tolerance)
    v_rules = _group_lines(v_runs, max_thickness_px=max_thickness_px,
                            gap_tolerance=gap_tolerance)

    # COMMENTED OUT — was causing regressions vs phase 16 baseline.
    # Bridge helpers are still defined below; revisit with the user
    # before re-enabling. Goal: merge collinear rule segments split
    # by content touching at column boundaries (e.g. Tourism box).
    # if column_grid or text_area_left_pt is not None \
    #         or text_area_right_pt is not None:
    #     grid_xs_px = []
    #     if column_grid:
    #         grid_xs_px.extend(int(round(g["x_pt"] * zoom))
    #                           for g in column_grid)
    #     if text_area_left_pt is not None:
    #         grid_xs_px.append(int(round(text_area_left_pt * zoom)))
    #     if text_area_right_pt is not None:
    #         grid_xs_px.append(int(round(text_area_right_pt * zoom)))
    #     h_rules = _bridge_collinear_h_rules(h_rules, grid_xs_px, zoom)
    #     v_rules = _bridge_collinear_v_rules(v_rules, grid_xs_px, zoom)

    # Gap-on-both-sides filter, applied to the ORIGINAL (un-dilated)
    # mask. Probe strip offset by skew_r so it lands on paper, not
    # on the actual wobbly line.
    probe_depth_px = max(4, int(zoom * 1.5))
    probe_offset_px = skew_r + 1
    gap_max_dark_frac = 0.30
    h_rules = [r for r in h_rules
                if _h_rule_has_gap_both_sides(dark, r, probe_depth_px,
                                                gap_max_dark_frac,
                                                probe_offset_px)]
    v_rules = [r for r in v_rules
                if _v_rule_has_gap_both_sides(dark, r, probe_depth_px,
                                                gap_max_dark_frac,
                                                probe_offset_px)]

    corner_tol_px = max(8, int(zoom * 8))
    span_slack_px = max(8, int(zoom * 10))

    rectangles: List[ClosedRectangle] = []
    for i, h_top in enumerate(h_rules):
        ty_start, ty_end, tx_lo, tx_hi = h_top
        ty_mid = (ty_start + ty_end) / 2.0
        for j in range(i + 1, len(h_rules)):
            h_bot = h_rules[j]
            by_start, by_end, bx_lo, bx_hi = h_bot
            by_mid = (by_start + by_end) / 2.0
            if by_mid - ty_mid < min_run_px:
                continue
            r_x_lo = max(tx_lo, bx_lo)
            r_x_hi = min(tx_hi, bx_hi)
            if r_x_hi - r_x_lo < min_run_px:
                continue
            left_match = None
            for vr in v_rules:
                vx_start, vx_end, vy_lo, vy_hi = vr
                vx_mid = (vx_start + vx_end) / 2.0
                if abs(vx_mid - r_x_lo) <= corner_tol_px:
                    if (vy_lo <= ty_mid + span_slack_px
                            and vy_hi >= by_mid - span_slack_px):
                        left_match = vr
                        break
            if left_match is None:
                continue
            right_match = None
            for vr in v_rules:
                vx_start, vx_end, vy_lo, vy_hi = vr
                vx_mid = (vx_start + vx_end) / 2.0
                if abs(vx_mid - r_x_hi) <= corner_tol_px:
                    if (vy_lo <= ty_mid + span_slack_px
                            and vy_hi >= by_mid - span_slack_px):
                        right_match = vr
                        break
            if right_match is None:
                continue
            t_thick = ty_end - ty_start + 1
            b_thick = by_end - by_start + 1
            avg_thick = (t_thick + b_thick) / 2.0
            rectangles.append(ClosedRectangle(
                x0_pt=r_x_lo / zoom,
                y0_pt=ty_mid / zoom,
                x1_pt=r_x_hi / zoom,
                y1_pt=by_mid / zoom,
                border_thickness_pt=avg_thick / zoom,
            ))

    rectangles.sort(key=lambda r: (r.y0_pt, r.x0_pt))
    deduped: List[ClosedRectangle] = []
    for r in rectangles:
        dup = False
        for d in deduped:
            if (abs(r.x0_pt - d.x0_pt) < 5 and abs(r.y0_pt - d.y0_pt) < 5
                    and abs(r.x1_pt - d.x1_pt) < 5
                    and abs(r.y1_pt - d.y1_pt) < 5):
                dup = True
                break
        if not dup:
            deduped.append(r)
    return deduped


def _bridge_collinear_h_rules(h_rules, grid_xs_px, zoom):
    """Merge h_rules at near-identical y-positions if their x_ranges
    are close and the gap straddles a column-grid anchor.

    A bordered article's top rule often gets split into pieces by
    text touching the rule at a column boundary. The two pieces sit
    at the same y and the gap is small (a few pixels to ~20pt). When
    the gap contains a grid anchor, treating them as one rule lets
    the rectangle pairing find the actual end-to-end extent.
    """
    if not h_rules or not grid_xs_px:
        return h_rules
    grid_xs_px = sorted(grid_xs_px)
    # Two rules are "collinear" if their perp_idx ranges overlap or
    # are within y_tol of each other.
    y_tol = max(2, int(zoom * 1.0))
    # Gap of up to 25pt between rule ends is bridgeable.
    max_gap_px = int(zoom * 25)
    # Sort by y-mid then by x_lo
    sorted_rules = sorted(h_rules,
                            key=lambda r: ((r[0] + r[1]) / 2.0, r[2]))
    merged = list(sorted_rules)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            if merged[i] is None:
                continue
            yi_s, yi_e, xi_lo, xi_hi = merged[i]
            yi_m = (yi_s + yi_e) / 2.0
            for j in range(i + 1, len(merged)):
                if merged[j] is None:
                    continue
                yj_s, yj_e, xj_lo, xj_hi = merged[j]
                yj_m = (yj_s + yj_e) / 2.0
                if abs(yj_m - yi_m) > y_tol:
                    continue
                # x-gap (positive = no overlap, with i to the left)
                if xi_hi < xj_lo:
                    gap_lo, gap_hi = xi_hi, xj_lo
                elif xj_hi < xi_lo:
                    gap_lo, gap_hi = xj_hi, xi_lo
                else:
                    # overlap — _group_lines should have merged these
                    # already; leave alone
                    continue
                gap = gap_hi - gap_lo
                if gap > max_gap_px:
                    continue
                # Require a grid anchor inside the gap (or at an edge,
                # within tolerance).
                straddle_tol = max(4, int(zoom * 3))
                hits = [g for g in grid_xs_px
                         if gap_lo - straddle_tol <= g <= gap_hi + straddle_tol]
                if not hits:
                    continue
                # Merge i and j
                merged[i] = (min(yi_s, yj_s), max(yi_e, yj_e),
                              min(xi_lo, xj_lo), max(xi_hi, xj_hi))
                merged[j] = None
                changed = True
    return [r for r in merged if r is not None]


def _bridge_collinear_v_rules(v_rules, grid_xs_px, zoom):
    """Same idea as _bridge_collinear_h_rules but for vertical rules:
    pieces of one box's vertical edge can be split by interior content
    touching the line. Bridge across small y-gaps when the rule's x
    is at a grid anchor."""
    if not v_rules or not grid_xs_px:
        return v_rules
    x_tol = max(2, int(zoom * 1.0))
    max_gap_px = int(zoom * 25)
    sorted_rules = sorted(v_rules,
                            key=lambda r: ((r[0] + r[1]) / 2.0, r[2]))
    merged = list(sorted_rules)
    grid_tol = max(4, int(zoom * 3))
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            if merged[i] is None:
                continue
            xi_s, xi_e, yi_lo, yi_hi = merged[i]
            xi_m = (xi_s + xi_e) / 2.0
            # Only consider bridging if rule lies near a grid anchor
            if not any(abs(g - xi_m) <= grid_tol for g in grid_xs_px):
                continue
            for j in range(i + 1, len(merged)):
                if merged[j] is None:
                    continue
                xj_s, xj_e, yj_lo, yj_hi = merged[j]
                xj_m = (xj_s + xj_e) / 2.0
                if abs(xj_m - xi_m) > x_tol:
                    continue
                if yi_hi < yj_lo:
                    gap = yj_lo - yi_hi
                elif yj_hi < yi_lo:
                    gap = yi_lo - yj_hi
                else:
                    continue
                if gap > max_gap_px:
                    continue
                merged[i] = (min(xi_s, xj_s), max(xi_e, xj_e),
                              min(yi_lo, yj_lo), max(yi_hi, yj_hi))
                merged[j] = None
                changed = True
    return [r for r in merged if r is not None]


@dataclass
class HorizontalRule:
    """A detected horizontal rule line in the rendered raster."""
    y_pt: float           # vertical position
    x0_pt: float
    x1_pt: float
    thickness_pt: float


def detect_horizontal_rules(page,
                             mast_y: float,
                             page_h: float,
                             page_w: float,
                             dpi: int = 100,
                             dark_thr: int = 130,
                             min_width_fraction: float = 0.30,
                             footer_margin_pt: float = 20.0,
                             ) -> List[HorizontalRule]:
    """Find horizontal rule lines — rows with a long contiguous dark
    span. These are visual barriers in modular layouts (between ads,
    under section headers, etc).

    Args:
        min_width_fraction: rule's longest dark run must be at least
            this fraction of page width to qualify. 0.30 catches both
            full-page rules and shorter section-divider rules.

    Returns list of HorizontalRule ordered by y.
    """
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    H, W = img.shape
    dark = img < dark_thr
    y_lo = max(0, int(round((mast_y or 0) * zoom)))
    y_hi = min(H, int(round((page_h - footer_margin_pt) * zoom)))
    min_run_px = int(min_width_fraction * W)
    if min_run_px < 10:
        min_run_px = 10

    rules: List[HorizontalRule] = []
    # For each row, compute the longest contiguous run of dark.
    in_rule = False
    rule_start_y = 0
    rule_x0 = 0; rule_x1 = 0
    for y in range(y_lo, y_hi):
        row = dark[y]
        # numpy-vectorised run lengths
        # pad with False to detect runs at boundaries
        padded = np.concatenate(([False], row, [False]))
        edges = np.diff(padded.astype(np.int8))
        starts = np.where(edges == 1)[0]
        ends = np.where(edges == -1)[0]
        if len(starts) == 0:
            max_run = 0
            best_x0 = 0; best_x1 = 0
        else:
            run_lens = ends - starts
            best = run_lens.argmax()
            max_run = int(run_lens[best])
            best_x0 = int(starts[best])
            best_x1 = int(ends[best])
        is_rule = max_run >= min_run_px
        if is_rule:
            if not in_rule:
                in_rule = True
                rule_start_y = y
                rule_x0 = best_x0
                rule_x1 = best_x1
            else:
                rule_x0 = min(rule_x0, best_x0)
                rule_x1 = max(rule_x1, best_x1)
        else:
            if in_rule:
                rule_end_y = y - 1
                thickness = max(1, rule_end_y - rule_start_y + 1)
                # Skip implausibly thick "rules" — those are bars
                # belonging to ads or solid blocks, not separators.
                if thickness <= max(8, int(zoom * 8)):
                    y_center = (rule_start_y + rule_end_y) / 2.0 / zoom
                    rules.append(HorizontalRule(
                        y_pt=y_center,
                        x0_pt=rule_x0 / zoom,
                        x1_pt=rule_x1 / zoom,
                        thickness_pt=thickness / zoom,
                    ))
                in_rule = False
    if in_rule:
        rule_end_y = y_hi - 1
        thickness = max(1, rule_end_y - rule_start_y + 1)
        if thickness <= max(8, int(zoom * 8)):
            y_center = (rule_start_y + rule_end_y) / 2.0 / zoom
            rules.append(HorizontalRule(
                y_pt=y_center,
                x0_pt=rule_x0 / zoom,
                x1_pt=rule_x1 / zoom,
                thickness_pt=thickness / zoom,
            ))
    return rules


def widen_article_headlines(articles_list: List[dict],
                             pixel_runs: List[PixelHeadlineRun],
                             min_iou: float = 0.15) -> int:
    """For each article in articles_list, find the best-matching pixel
    headline run and widen the article's headline.bbox to it where the
    pixel version is broader. Also grows the article's bbox x-range to
    include the wider headline.

    Modifies `articles_list` in place. Returns the number of articles
    whose headlines were widened.
    """
    if not pixel_runs:
        return 0
    n_widened = 0
    for art in articles_list:
        if not isinstance(art, dict):
            continue
        hl = art.get("headline") or {}
        hl_bb = hl.get("bbox")
        if not hl_bb or len(hl_bb) < 4:
            continue
        hx0, hy0, hx1, hy1 = hl_bb[:4]
        ocr_w = hx1 - hx0
        best_run = None
        best_score = 0.0
        for r in pixel_runs:
            # Spatial overlap: prefer runs whose y-range overlaps the
            # OCR headline AND whose x-range covers the OCR headline.
            y_ov = max(0, min(hy1, r.y1) - max(hy0, r.y0))
            y_len = max(1, hy1 - hy0)
            y_frac = y_ov / y_len
            if y_frac < 0.3:
                continue
            x_ov = max(0, min(hx1, r.x1) - max(hx0, r.x0))
            x_frac = x_ov / max(1, ocr_w)
            score = y_frac * x_frac
            if score > best_score:
                best_score = score
                best_run = r
        if best_run is None or best_score < min_iou:
            continue
        # Widen if the pixel run is meaningfully wider OR taller than
        # the OCR-derived headline. Height case captures the two-tier
        # headline pattern (Bassile big tier + Developer sub-deck on
        # 2007-02-13 p1) — OCR only caught the lower tier, but the
        # pixel detector found both as a single multi-line run.
        pixel_w = best_run.x1 - best_run.x0
        pixel_h = best_run.y1 - best_run.y0
        ocr_h = hy1 - hy0
        wider = pixel_w > ocr_w * 1.10
        taller = pixel_h > ocr_h * 1.10
        if wider or taller:
            new_bbox = (min(hx0, best_run.x0),
                        min(hy0, best_run.y0),
                        max(hx1, best_run.x1),
                        max(hy1, best_run.y1))
            art["headline"]["bbox"] = list(new_bbox)
            art["headline"]["widened_from_pixels"] = True
            # Grow the article's own bbox to include the widened
            # headline on ALL FOUR sides — the article must include
            # its full headline (any tier).
            ab = list(art.get("bbox") or new_bbox)
            ab[0] = min(ab[0], new_bbox[0])
            ab[1] = min(ab[1], new_bbox[1])
            ab[2] = max(ab[2], new_bbox[2])
            ab[3] = max(ab[3], new_bbox[3])
            art["bbox"] = ab
            n_widened += 1
    return n_widened
