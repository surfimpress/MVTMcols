"""
Display ad detection for the Almonte Gazette pipeline.

Detects bordered display advertisements using OpenCV contour analysis.
Ads are identified by their thick rectangular borders, which form
closed contours with high rectangularity scores.

The detected ad boxes tell the column detection pipeline where
column rules are likely obscured, so those zones can be excluded
from the consensus.

Usage:
    from detect_ads import detect_ads

    ads = detect_ads("page.pdf")
    for ad in ads:
        print(f"Ad at ({ad['x_pct']:.0f}%, {ad['y_pct']:.0f}%) "
              f"{ad['w_pct']:.0f}%x{ad['h_pct']:.0f}% ~{ad['cols']}col")
"""

import sqlite3
import uuid
from contextlib import closing

import fitz
# import numpy as np  # unused — kept commented for revival convenience
import cv2


from coordinates import pct_to_px, pct_to_px_float, px_to_pct
from pdf_utils import (
    open_clean_pdf as _open_clean,
    render_grey_uint8,
    get_clip_pixmap,
    get_page_size_pts,
)


# ── Adaptive-threshold parameter groups for the contour-finding pass ─
# STRICT_PARAMS is the working default — small block, modest C, light
# close. LOOSE_PARAMS catches faint borders on low-contrast pages by
# widening the threshold neighbourhood and bridging broken stretches
# with a bigger close kernel and more iterations.
#
# CONTRAST_TIER1_THRESHOLD is the empirical paper/ink gap below which
# the loose pass fires as a second sweep. Anchor: 1898-10-07 P5/P6 —
# text legible but the strict 21/10 missed real borders. Sibling
# trigger `dynamic_range < 100` lives in page_profile.py:475 and is
# OR'd with this threshold (covers pages where dynamic range is wide
# because of dark imagery but the running text is still light).
STRICT_PARAMS = dict(block_size=21, C=10, kernel_size=3, iterations=2)
LOOSE_PARAMS = dict(block_size=31, C=8, kernel_size=5, iterations=3)
CONTRAST_TIER1_THRESHOLD = 145


def _detect_ads_pass(grey, h, w, *, block_size, C, kernel_size, iterations,
                     min_width_pct, min_height_pct, gather_min_height_pct,
                     min_rect_ratio, pitch, page_profile):
    """
    One threshold-and-contour pass. Returns the per-contour-filtered ad
    candidate list (still carrying the internal _y1_px / _is_short fields
    that the dedup + sibling-merge stages consume).

    Pulled out of detect_ads so a second, looser pass can run on
    low-contrast pages (Tier 1 adaptive thresholds) without duplicating
    the per-contour filter logic.
    """
    binary = cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block_size, C,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=iterations)

    contours, hierarchy = cv2.findContours(
        closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE,
    )

    min_area = w * h * 0.005  # at least 0.5% of page area
    min_w = pct_to_px(min_width_pct, w)
    min_h = pct_to_px(min_height_pct, h)
    gather_min_h = pct_to_px(gather_min_height_pct, h)

    ads = []
    for i, cnt in enumerate(contours):
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        rect_area = bw * bh
        if rect_area == 0:
            continue

        # Size filters. Use gather_min_h here so short PROD-grade
        # candidates can survive into the sibling-merge pass below.
        if bw < min_w or bh < gather_min_h:
            continue
        is_short = bh < min_h
        # Not the whole page, and not a page border
        if bw > w * 0.85 and bh > h * 0.85:
            continue
        # A contour covering > 50% of the page is a page border or
        # photograph edge, not a display ad
        if (bw * bh) > (w * h * 0.50):
            continue

        rect_ratio = area / rect_area
        aspect = bw / bh if bh > 0 else 0

        # Rectangularity filter. Short candidates (below min_height_pct)
        # must be PROD-grade rect_ratio — they're only kept for the
        # sibling-merge pass and we don't want noise.
        if rect_ratio < 0.40:
            continue
        if is_short and rect_ratio < min_rect_ratio:
            continue
        # Aspect ratio filter: not a thin horizontal rule
        if aspect > 10.0 or aspect < 0.1:
            continue

        # ── Edge filter: reject if any edge aligns with page boundary ──
        # Real ads are interior to the page. If an edge is within 3%
        # of the page/image boundary, it's likely shadow, photo edge,
        # or scan artifact — not a boxed ad.
        EDGE_MARGIN = 3.0  # percent of page dimension
        x_pct = px_to_pct(x, w)
        y_pct = px_to_pct(y, h)
        x_end_pct = px_to_pct(x + bw, w)
        y_end_pct = px_to_pct(y + bh, h)

        at_left_edge = x_pct < EDGE_MARGIN
        at_right_edge = x_end_pct > (100 - EDGE_MARGIN)
        at_top_edge = y_pct < EDGE_MARGIN
        at_bottom_edge = y_end_pct > (100 - EDGE_MARGIN)

        # If touching two opposing edges (left+right or top+bottom),
        # it's a full-width/height element, not a boxed ad
        if (at_left_edge and at_right_edge) or (at_top_edge and at_bottom_edge):
            continue

        # If touching any edge AND low rectangularity, it's shadow/artifact
        if (at_left_edge or at_right_edge) and rect_ratio < 0.80:
            continue
        if (at_top_edge or at_bottom_edge) and rect_ratio < 0.80:
            continue

        # Confidence scoring
        if rect_ratio > min_rect_ratio and 0.3 < aspect < 5.0:
            confidence = "high"
        elif rect_ratio > 0.70 and 0.2 < aspect < 8.0:
            confidence = "medium"
        else:
            confidence = "low"

        # Reject contours that match the photograph boundary (R2).
        # The scanned image edge forms a large rectangular contour
        # that is NOT an ad — it's the edge of the photograph.
        if page_profile and "r2" in page_profile:
            r2 = page_profile["r2"]
            # Check if this contour closely matches R2 on 2+ sides
            matches_r2 = 0
            if abs(x_pct - r2["left"]) < 3: matches_r2 += 1
            if abs(x_end_pct - r2["right"]) < 3: matches_r2 += 1
            if abs(y_pct - r2["top"]) < 5: matches_r2 += 1
            if abs(y_end_pct - r2["bottom"]) < 5: matches_r2 += 1
            if matches_r2 >= 2:
                continue  # this is the photograph edge, not an ad

        # Downgrade confidence if touching any page edge
        if at_left_edge or at_right_edge or at_top_edge or at_bottom_edge:
            if confidence == "high":
                confidence = "medium"
            elif confidence == "medium":
                confidence = "low"

        # Check for children (content inside the box)
        has_children = (hierarchy[0][i][2] != -1) if hierarchy is not None else False

        # Estimate column span
        cols = max(1, round(bw / w * 100 / pitch))

        ads.append({
            "x_pct": round(x_pct, 2),
            "y_pct": round(y_pct, 2),
            "w_pct": px_to_pct(bw, w),
            "h_pct": px_to_pct(bh, h),
            "x_end_pct": round(x_end_pct, 2),
            "y_end_pct": round(y_end_pct, 2),
            "rect_ratio": round(rect_ratio, 3),
            "aspect": round(aspect, 2),
            "cols": cols,
            "has_children": has_children,
            "confidence": confidence,
            "_is_short": is_short,
            # Raw pixel-space bounds, used by sibling-merge to avoid
            # rounding-noise false positives at the boundary tolerance.
            "_y1_px": y,
            "_y2_px": y + bh,
            "_x1_px": x,
            "_x2_px": x + bw,
        })

    return ads


def detect_ads(pdf_path, page_number=0, render_dpi=150,
               min_width_pct=15, min_height_pct=5,
               gather_min_height_pct=3.0,
               min_rect_ratio=0.85, column_pitch=None,
               page_profile=None):
    """
    Detect bordered display advertisements on a PDF page.

    Uses adaptive thresholding and contour analysis to find
    rectangular bordered regions that span 2+ columns.

    Args:
        pdf_path:        Path to the PDF.
        page_number:     Zero-indexed page within the PDF.
        render_dpi:      DPI for rendering (150 is sufficient for ad detection).
        min_width_pct:   Minimum ad width as % of page width.
        min_height_pct:  Minimum ad height as % of page height for emission.
        gather_min_height_pct: Lower height floor for the sibling-merge pass.
                         Candidates between gather_min_height_pct and
                         min_height_pct are only emitted if they merge with
                         a full-height ad sharing a horizontal boundary
                         (e.g. an ad cut off at the top because a full-width
                         internal rule produced a shorter sub-blob, like
                         "Karl's Grocery" on 1947-11-06 P4).
        min_rect_ratio:  Minimum rectangularity (contour area / bounding rect area).
        column_pitch:    If known, used to estimate column span of each ad.
        page_profile:    Optional dict from page_profile.profile_page().
                         Used both for R2 photograph-edge rejection and to
                         decide whether to run a looser second contour pass
                         on low-contrast pages (Tier 1 adaptive thresholds).

    Returns:
        List of ad dicts, sorted by area (largest first).
        Each dict has: x_pct, y_pct, w_pct, h_pct, rect_ratio,
        aspect, cols (estimated column span), confidence.
    """
    grey = render_grey_uint8(pdf_path, page_number, render_dpi)
    h, w = grey.shape
    pitch = column_pitch or 12.0  # default guess if not provided

    pass_kwargs = dict(
        min_width_pct=min_width_pct,
        min_height_pct=min_height_pct,
        gather_min_height_pct=gather_min_height_pct,
        min_rect_ratio=min_rect_ratio,
        pitch=pitch,
        page_profile=page_profile,
    )

    # ── Pass 1 — standard params (the working default) ──────────────
    ads = _detect_ads_pass(grey, h, w, **STRICT_PARAMS, **pass_kwargs)

    # ── Pass 2 — looser params for low-contrast pages (Tier 1) ──────
    # Triggered when the page profile says contrast is low. The loose
    # params (see LOOSE_PARAMS at module top) catch faint borders that
    # the strict pass misses, and bridge the broken stretches that
    # result from light ink. Empirically validated on 1898-10-07: P4
    # 0→1 candidate, P5 0→3 candidates with a PROD, P6 2→4 candidates.
    # Dense pages (1947 P4/P6, 1920 P8) show no regression because the
    # trigger doesn't fire on them.
    #
    # Trigger: either the profile flagged 'low_contrast' (dynamic_range
    # < 100, set in page_profile.py), OR the empirical paper/ink gap is
    # below CONTRAST_TIER1_THRESHOLD. The latter catches pages where
    # dynamic_range happens to be wide because of dark imagery but the
    # running text is still printed lightly — observed on 1898-10-07
    # P5/P6.
    if page_profile:
        # ink_mean is computed on the *inverted* image in page_profile.py
        # (line 444: ink_mask = body > 128 where body = inv[...]), so a
        # large ink_mean means dark ink. paper_mean is the paper darkness
        # baseline (small for clean white paper). contrast = ink_mean -
        # paper_mean is therefore positive, with larger = more contrast.
        contrast = (page_profile.get("ink_mean", 255.0) -
                    page_profile.get("paper_mean", 0.0))
        flags = page_profile.get("quality_flags") or []
        if contrast < CONTRAST_TIER1_THRESHOLD or "low_contrast" in flags:
            ads.extend(_detect_ads_pass(
                grey, h, w, **LOOSE_PARAMS, **pass_kwargs,
            ))

    # Sort by area (largest first)
    ads.sort(key=lambda a: a["w_pct"] * a["h_pct"], reverse=True)

    # Deduplicate: remove ads that are almost entirely contained within a larger one
    deduped = []
    for ad in ads:
        is_contained = False
        for existing in deduped:
            # Check if this ad is mostly inside an existing one
            overlap_left = max(ad["x_pct"], existing["x_pct"])
            overlap_right = min(ad["x_end_pct"], existing["x_end_pct"])
            overlap_top = max(ad["y_pct"], existing["y_pct"])
            overlap_bottom = min(ad["y_end_pct"], existing["y_end_pct"])

            if overlap_right > overlap_left and overlap_bottom > overlap_top:
                overlap_area = (overlap_right - overlap_left) * (overlap_bottom - overlap_top)
                ad_area = ad["w_pct"] * ad["h_pct"]
                if ad_area > 0 and overlap_area / ad_area > 0.8:
                    is_contained = True
                    break

        if not is_contained:
            deduped.append(ad)

    # ── Sibling-merge pass for short PROD-grade extras ──────────────
    # Catches the "internal full-width rule cuts the contour short"
    # case (e.g. Karl's Grocery on 1947-11-06 P4): a short, crisp
    # candidate sits directly above or below a full-height ad, sharing
    # an exact horizontal boundary (i.e. the rule between them is the
    # SAME rule, not separate top/bottom borders with a gutter between).
    # Extend the full ad to absorb the short extra. Unmatched short
    # extras are dropped — they don't survive on their own.
    #
    # Tolerance of 6 px in pixel space (~0.34% at 150 DPI on a typical
    # 1754-row page) admits a single shared rule with a small bbox
    # jitter, but rejects a real gutter between two distinct ads.
    # Empirical anchors on 1947-11-06: Karl's Grocery + tallest 2col
    # on P4 have a 5-px contour overlap (shared rule); R. P. Egerton +
    # Comba's Furniture on P3 have a 10-px gutter (separate ads).
    SHARE_TOL_PX = 6
    XOVL_THRESH = 0.70
    full = [a for a in deduped if not a.get("_is_short")]
    short = [a for a in deduped if a.get("_is_short")]
    for s in short:
        s_w_px = s["_x2_px"] - s["_x1_px"]
        for f in full:
            f_w_px = f["_x2_px"] - f["_x1_px"]
            ox1 = max(s["_x1_px"], f["_x1_px"])
            ox2 = min(s["_x2_px"], f["_x2_px"])
            if ox2 <= ox1:
                continue
            if (ox2 - ox1) / min(s_w_px, f_w_px) < XOVL_THRESH:
                continue
            # Short extra ABOVE full (extra's bottom == full's top)
            if abs(s["_y2_px"] - f["_y1_px"]) <= SHARE_TOL_PX:
                f["_y1_px"] = s["_y1_px"]
                f["y_pct"] = px_to_pct(s["_y1_px"], h)
                f["h_pct"] = round(f["y_end_pct"] - f["y_pct"], 2)
                f["extended"] = True
                break
            # Short extra BELOW full (extra's top == full's bottom)
            if abs(s["_y1_px"] - f["_y2_px"]) <= SHARE_TOL_PX:
                f["_y2_px"] = s["_y2_px"]
                f["y_end_pct"] = px_to_pct(s["_y2_px"], h)
                f["h_pct"] = round(f["y_end_pct"] - f["y_pct"], 2)
                f["extended"] = True
                break

    # Strip internal fields and emit only full-height ads.
    for a in full:
        for k in ("_is_short", "_y1_px", "_y2_px", "_x1_px", "_x2_px"):
            a.pop(k, None)
    return full


def detect_single_col_ads(pdf_path, multi_col_ads=None, page_number=0,
                          render_dpi=150,
                          min_width_pct=5, max_width_pct=15,
                          min_height_pct=4,
                          min_rect_ratio=0.70,
                          edge_margin_pct=3.0,
                          page_profile=None):
    """
    Detect bordered single-column display advertisements on a PDF page.

    A complement to detect_ads(): same image pipeline (adaptive threshold +
    morph close + contour analysis), but tuned for the single-column case
    where contour borders often have small printer-rule junctions that the
    morph close cannot bridge. Two refinement passes recover ads whose
    initial contour is fragmented or truncated:

    - Sibling-merge: stacked or side-by-side sub-blobs sharing an x or y
      range and tight gap merge into one ad.
    - Boundary-search: a candidate's top/bottom y is extended outward to
      the outermost horizontal rule found in the binary mask within 6%.

    Args:
        pdf_path:        Path to the PDF.
        multi_col_ads:   List of multi-column ad dicts from detect_ads();
                         single-col candidates >= 50% inside any of these
                         are dropped (multi takes precedence).
        page_number:     Zero-indexed page within the PDF.
        render_dpi:      DPI for rendering (matches detect_ads default).
        min_width_pct:   Minimum ad width as % of page width (5).
        max_width_pct:   Maximum ad width as % of page width (15).
        min_height_pct:  Minimum ad height as % of page height (4).
        min_rect_ratio:  Minimum rectangularity for initial admission
                         (0.70 — admits sub-blobs from fragmented borders).
        edge_margin_pct: Reject contours within this % of any page edge.

    Returns:
        List of ad dicts with the same shape as detect_ads(), plus
        'cols' always = 1.
    """
    multi_col_ads = multi_col_ads or []

    # ── A) Common preprocessing — identical to detect_ads ──────────────
    grey = render_grey_uint8(pdf_path, page_number, render_dpi)
    h, w = grey.shape

    binary = cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 21, 10,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, hierarchy = cv2.findContours(
        closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE,
    )

    # ── B) Initial contour scan with single-col filters ────────────────
    min_area = w * h * 0.005
    min_w_px = pct_to_px(min_width_pct, w)
    max_w_px = pct_to_px(max_width_pct, w)
    min_h_px = pct_to_px(min_height_pct, h)

    def _overlap_pct(a_x1, a_y1, a_x2, a_y2, b_x1, b_y1, b_x2, b_y2):
        ox = max(0.0, min(a_x2, b_x2) - max(a_x1, b_x1))
        oy = max(0.0, min(a_y2, b_y2) - max(a_y1, b_y1))
        return ox * oy

    def _inside_multi(x_pct, y_pct, x_end_pct, y_end_pct):
        if not multi_col_ads:
            return False
        area = (x_end_pct - x_pct) * (y_end_pct - y_pct)
        if area <= 0:
            return False
        for m in multi_col_ads:
            mx1 = m["x_pct"]; my1 = m["y_pct"]
            mx2 = m.get("x_end_pct", mx1 + m["w_pct"])
            my2 = m.get("y_end_pct", my1 + m["h_pct"])
            ov = _overlap_pct(x_pct, y_pct, x_end_pct, y_end_pct,
                              mx1, my1, mx2, my2)
            if ov / area > 0.5:
                return True
        return False

    raw_candidates = []  # list of dicts
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw == 0 or bh == 0:
            continue
        if bw < min_w_px or bw > max_w_px:
            continue
        if bh < min_h_px:
            continue
        rect_area = bw * bh
        rect_ratio = area / rect_area
        if rect_ratio < min_rect_ratio:
            continue
        x_pct = px_to_pct(x, w)
        y_pct = px_to_pct(y, h)
        x_end_pct = px_to_pct(x + bw, w)
        y_end_pct = px_to_pct(y + bh, h)
        # Edge margin
        if (x_pct < edge_margin_pct or
            x_end_pct > 100 - edge_margin_pct or
            y_pct < edge_margin_pct or
            y_end_pct > 100 - edge_margin_pct):
            continue
        # R2 photograph-edge match (mirrors detect_ads:167-176)
        if page_profile and "r2" in page_profile:
            r2 = page_profile["r2"]
            matches = 0
            if abs(x_pct - r2["left"]) < 3: matches += 1
            if abs(x_end_pct - r2["right"]) < 3: matches += 1
            if abs(y_pct - r2["top"]) < 5: matches += 1
            if abs(y_end_pct - r2["bottom"]) < 5: matches += 1
            if matches >= 2:
                continue
        # Drop if mostly inside an existing multi-col ad
        if _inside_multi(x_pct, y_pct, x_end_pct, y_end_pct):
            continue

        raw_candidates.append({
            "x_pct": x_pct, "y_pct": y_pct,
            "x_end_pct": x_end_pct, "y_end_pct": y_end_pct,
            "rect_ratio": rect_ratio,
            "fill_area_px": area,        # for sibling-merge math
            "rect_area_px": rect_area,
            "merged": False,
            "extended": False,
        })

    # ── C) Sibling-merge: fragmented perimeters into one rectangle ─────
    # Iterate until a pass produces no merge. Cap at 4 iterations.
    def _try_merge(a, b):
        ax1, ay1, ax2, ay2 = a["x_pct"], a["y_pct"], a["x_end_pct"], a["y_end_pct"]
        bx1, by1, bx2, by2 = b["x_pct"], b["y_pct"], b["x_end_pct"], b["y_end_pct"]
        aw_p = ax2 - ax1; ah_p = ay2 - ay1
        bw_p = bx2 - bx1; bh_p = by2 - by1
        # union extent
        ux1, uy1 = min(ax1, bx1), min(ay1, by1)
        ux2, uy2 = max(ax2, bx2), max(ay2, by2)
        uw_p = ux2 - ux1; uh_p = uy2 - uy1
        # union must remain single-col-width
        if uw_p < min_width_pct or uw_p > max_width_pct:
            return None
        # x-overlap fraction over narrower
        ovx = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        narrower_w = min(aw_p, bw_p)
        # y-overlap fraction over shorter
        ovy = max(0.0, min(ay2, by2) - max(ay1, by1))
        shorter_h = min(ah_p, bh_p)
        # vertical alignment: stacked, x-cols overlap
        v_ok = (narrower_w > 0 and ovx / narrower_w >= 0.70)
        v_gap = max(0.0, max(ay1, by1) - min(ay2, by2))
        # horizontal alignment: side-by-side, y-rows overlap
        h_ok = (shorter_h > 0 and ovy / shorter_h >= 0.70)
        h_gap = max(0.0, max(ax1, bx1) - min(ax2, bx2))

        is_vert = v_ok and v_gap <= 1.5
        is_horz = h_ok and h_gap <= 1.5
        if not (is_vert or is_horz):
            return None

        # Only merge if BOTH are fragments. A fully-formed contour
        # (rect_ratio >= 0.85) is an intact ad on its own and must not
        # absorb stacked neighbours.
        if a["rect_ratio"] >= 0.85 or b["rect_ratio"] >= 0.85:
            return None

        # bridge area estimate (conservative — assume slit is half-filled)
        if is_vert:
            gap_h_pct = v_gap
            gap_h_px = pct_to_px_float(gap_h_pct, h)
            bridge = pct_to_px_float(uw_p, w) * gap_h_px * 0.5
        else:
            gap_w_pct = h_gap
            gap_w_px = pct_to_px_float(gap_w_pct, w)
            bridge = pct_to_px_float(uh_p, h) * gap_w_px * 0.5
        union_rect_px = pct_to_px_float(uw_p, w) * pct_to_px_float(uh_p, h)
        if union_rect_px <= 0:
            return None
        combined = (a["fill_area_px"] + b["fill_area_px"] + bridge) / union_rect_px
        if combined < 0.70:
            return None

        return {
            "x_pct": ux1, "y_pct": uy1,
            "x_end_pct": ux2, "y_end_pct": uy2,
            "rect_ratio": min(combined, 1.0),
            "fill_area_px": a["fill_area_px"] + b["fill_area_px"] + bridge,
            "rect_area_px": union_rect_px,
            "merged": True,
            "extended": a.get("extended", False) or b.get("extended", False),
        }

    candidates = list(raw_candidates)
    for _ in range(4):
        merged_any = False
        i = 0
        while i < len(candidates):
            j = i + 1
            merged_here = False
            while j < len(candidates):
                m = _try_merge(candidates[i], candidates[j])
                if m is not None:
                    candidates[i] = m
                    candidates.pop(j)
                    merged_any = True
                    merged_here = True
                    continue
                j += 1
            i += 1
            if merged_here:
                # restart inner sweep is unnecessary; outer loop handles chains
                pass
        if not merged_any:
            break

    # ── D) Boundary-search refinement: extend y to outermost rules ─────
    # Use the ORIGINAL binary (not closed) so we find real rules, not
    # close-bridged content.
    SEARCH_PCT = 6.0
    RULE_FILL_FRAC = 0.80
    MAX_HEIGHT_PCT = 25.0

    def _row_is_rule(row_idx, x1_px, x2_px):
        if row_idx < 0 or row_idx >= h or x2_px <= x1_px:
            return False
        row = binary[row_idx, x1_px:x2_px]
        if row.size == 0:
            return False
        # binary is 0/255; ink is 255 (THRESH_BINARY_INV)
        return (row > 0).mean() >= RULE_FILL_FRAC

    def _multi_blocks_extension(x1, x2, y_old, y_new, going_up):
        """True if extending in [y_old <-> y_new] would cross a multi-col ad
        boundary that overlaps our x-range."""
        for m in multi_col_ads:
            mx1 = m["x_pct"]; my1 = m["y_pct"]
            mx2 = m.get("x_end_pct", mx1 + m["w_pct"])
            my2 = m.get("y_end_pct", my1 + m["h_pct"])
            # x must overlap to be relevant
            ovx = max(0.0, min(x2, mx2) - max(x1, mx1))
            if ovx <= 0:
                continue
            if going_up:
                # extending y_old -> y_new (y_new < y_old)
                # if any horizontal edge of the multi ad sits in [y_new, y_old]
                if y_new < my1 < y_old or y_new < my2 < y_old:
                    return True
            else:
                if y_old < my1 < y_new or y_old < my2 < y_new:
                    return True
        return False

    for c in candidates:
        x1_px = pct_to_px(c["x_pct"], w)
        x2_px = pct_to_px(c["x_end_pct"], w)
        y1_px = pct_to_px(c["y_pct"], h)
        y2_px = pct_to_px(c["y_end_pct"], h)

        new_y1_px = y1_px
        new_y2_px = y2_px

        # search top: scan [y1 - 6%, y1] from y1 upward — the rule
        # closest to the candidate is the real top border. Going farther
        # risks hitting unrelated content above.
        top_lo = max(pct_to_px(edge_margin_pct, h),
                     y1_px - pct_to_px(SEARCH_PCT, h))
        for r in range(y1_px - 1, top_lo - 1, -1):
            if _row_is_rule(r, x1_px, x2_px):
                new_y1_px = r
                break

        # search bottom: scan [y2, y2 + 6%] from y2 downward — closest
        # rule is the real bottom border.
        bot_hi = min(pct_to_px(100 - edge_margin_pct, h),
                     y2_px + pct_to_px(SEARCH_PCT, h))
        for r in range(y2_px, bot_hi):
            if _row_is_rule(r, x1_px, x2_px):
                new_y2_px = r + 1  # exclusive end
                break

        # apply guards
        new_y1_pct = px_to_pct(new_y1_px, h)
        new_y2_pct = px_to_pct(new_y2_px, h)
        new_h_pct = new_y2_pct - new_y1_pct

        # height cap
        if new_h_pct > MAX_HEIGHT_PCT:
            continue
        # multi-col boundary crossing
        if (new_y1_pct < c["y_pct"] and
            _multi_blocks_extension(c["x_pct"], c["x_end_pct"],
                                    c["y_pct"], new_y1_pct, going_up=True)):
            new_y1_px = y1_px
            new_y1_pct = c["y_pct"]
        if (new_y2_pct > c["y_end_pct"] and
            _multi_blocks_extension(c["x_pct"], c["x_end_pct"],
                                    c["y_end_pct"], new_y2_pct, going_up=False)):
            new_y2_px = y2_px
            new_y2_pct = c["y_end_pct"]

        # was anything actually extended?
        if new_y1_px == y1_px and new_y2_px == y2_px:
            continue

        # Don't extend across another single-col candidate's bbox in the
        # same x-range — that would merge two distinct ads.
        for other in candidates:
            if other is c:
                continue
            ox1 = max(c["x_pct"], other["x_pct"])
            ox2 = min(c["x_end_pct"], other["x_end_pct"])
            if ox2 <= ox1:
                continue
            ovx_frac = (ox2 - ox1) / (c["x_end_pct"] - c["x_pct"])
            if ovx_frac < 0.5:
                continue
            # extending up across other's bottom?
            if (new_y1_pct < other["y_end_pct"] <= c["y_pct"] and
                new_y1_pct < other["y_end_pct"]):
                new_y1_px = y1_px
                new_y1_pct = c["y_pct"]
            # extending down across other's top?
            if (c["y_end_pct"] <= other["y_pct"] < new_y2_pct):
                new_y2_px = y2_px
                new_y2_pct = c["y_end_pct"]

        # was anything still extended after the cross-candidate guard?
        if new_y1_px == y1_px and new_y2_px == y2_px:
            continue

        new_w_px = x2_px - x1_px
        new_h_px = new_y2_px - new_y1_px
        if new_w_px <= 0 or new_h_px <= 0:
            continue

        c["y_pct"] = new_y1_pct
        c["y_end_pct"] = new_y2_pct
        c["rect_area_px"] = new_w_px * new_h_px
        # The strong-rule discovery validates the extension; rect_ratio
        # stays at its pre-extension value (it was computed on the contour
        # that opened the extension, and the new region doesn't have its
        # own contour metric).
        c["extended"] = True

    # ── E) Final dedup: drop any ad mostly inside another ──────────────
    candidates.sort(
        key=lambda c: (c["x_end_pct"] - c["x_pct"]) * (c["y_end_pct"] - c["y_pct"]),
        reverse=True,
    )
    deduped = []
    for c in candidates:
        contained = False
        c_area = (c["x_end_pct"] - c["x_pct"]) * (c["y_end_pct"] - c["y_pct"])
        if c_area <= 0:
            continue
        for d in deduped:
            ov = _overlap_pct(c["x_pct"], c["y_pct"],
                              c["x_end_pct"], c["y_end_pct"],
                              d["x_pct"], d["y_pct"],
                              d["x_end_pct"], d["y_end_pct"])
            if ov / c_area > 0.8:
                contained = True
                break
        if not contained:
            deduped.append(c)

    # ── F) Output shape — match detect_ads() ───────────────────────────
    out = []
    for c in deduped:
        bw_p = c["x_end_pct"] - c["x_pct"]
        bh_p = c["y_end_pct"] - c["y_pct"]
        if bh_p <= 0:
            continue
        aspect = bw_p / bh_p
        rr = c["rect_ratio"]
        merged = c.get("merged", False)
        extended = c.get("extended", False)
        if rr >= 0.85 and not merged and not extended:
            confidence = "high"
        elif rr >= 0.78 or merged:
            confidence = "medium"
        else:
            confidence = "low"
        out.append({
            "x_pct": round(c["x_pct"], 2),
            "y_pct": round(c["y_pct"], 2),
            "w_pct": round(bw_p, 2),
            "h_pct": round(bh_p, 2),
            "x_end_pct": round(c["x_end_pct"], 2),
            "y_end_pct": round(c["y_end_pct"], 2),
            "rect_ratio": round(rr, 3),
            "aspect": round(aspect, 2),
            "cols": 1,
            "has_children": False,
            "confidence": confidence,
            "merged": merged,
            "extended": extended,
        })
    return out


def get_ad_exclusion_zones(ads, min_confidence="medium"):
    """
    Convert detected ads into x-range exclusion zones for column detection.

    Returns list of (x_start_pct, x_end_pct, y_start_pct, y_end_pct) tuples
    representing page areas where column rules may be obscured by ads.
    """
    conf_order = {"high": 0, "medium": 1, "low": 2}
    min_level = conf_order.get(min_confidence, 1)

    zones = []
    for ad in ads:
        level = conf_order.get(ad["confidence"], 2)
        if level <= min_level and ad["cols"] >= 2:
            zones.append((
                ad["x_pct"],
                ad["x_end_pct"],
                ad["y_pct"],
                ad["y_end_pct"],
            ))

    return zones


def print_ads(ads):
    """Pretty-print detected ads."""
    if not ads:
        print("  No display ads detected.")
        return
    for ad in ads:
        print(f"  ({ad['x_pct']:5.1f}%, {ad['y_pct']:5.1f}%) "
              f"{ad['w_pct']:4.1f}%x{ad['h_pct']:4.1f}%  "
              f"~{ad['cols']}col  rect={ad['rect_ratio']:.2f}  "
              f"aspect={ad['aspect']:.1f}  {ad['confidence']}")


def extract_ad_images(pdf_path, ads, output_dir, page_number=0, dpi=450,
                      margin_pct=2.0, name_prefix="ad"):
    """
    Extract each detected ad as a separate PNG image with margin.

    Adds a margin around each ad to capture the full border and
    handle page skew. The margin is a percentage of the ad's own
    dimensions, not the page.

    Args:
        pdf_path:    Path to the PDF.
        ads:         List of ad dicts from detect_ads().
        output_dir:  Where to save ad images.
        page_number: Zero-indexed page within the PDF.
        dpi:         Render resolution for extraction.
        margin_pct:  Margin as % of ad dimensions (default 2%).
        name_prefix: Filename prefix between page stem and index
                     (default "ad" → "{stem}_ad1.png"). Use a
                     different prefix for single-col extraction so
                     filenames don't collide.

    Returns:
        List of dicts with ad metadata + image_path.
    """
    import os

    # P-shared: page dims come from the cached render entry; per-ad
    # clips reuse the cached full-page pixmap instead of opening the
    # PDF and re-rasterising once per ad.
    pw, ph = get_page_size_pts(pdf_path, page_number, dpi)

    os.makedirs(output_dir, exist_ok=True)
    stem = pdf_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]

    results = []
    for i, ad in enumerate(ads):
        # Compute margin in page percentage based on ad size
        margin_x = ad["w_pct"] * margin_pct / 100
        margin_y = ad["h_pct"] * margin_pct / 100

        # Apply margin, clamped to page bounds
        x0_pct = max(0, ad["x_pct"] - margin_x)
        y0_pct = max(0, ad["y_pct"] - margin_y)
        x1_pct = min(100, ad["x_end_pct"] + margin_x)
        y1_pct = min(100, ad["y_end_pct"] + margin_y)

        # Convert percentages to PDF points (pct_to_px_float is
        # dimension-agnostic — works for points just as for pixels).
        x0 = pct_to_px_float(x0_pct, pw)
        y0 = pct_to_px_float(y0_pct, ph)
        x1 = pct_to_px_float(x1_pct, pw)
        y1 = pct_to_px_float(y1_pct, ph)

        clip = fitz.Rect(x0, y0, x1, y1)
        pix = get_clip_pixmap(pdf_path, page_number, dpi, clip)

        filename = f"{stem}_{name_prefix}{i + 1}.png"
        filepath = os.path.join(output_dir, filename)
        pix.save(filepath)

        results.append({
            **ad,
            "image_path": filepath,
            "image_filename": filename,
            "uuid": str(uuid.uuid4()),
        })
    return results


def init_ads_table(db_path):
    """Create the ads table in SQLite if it doesn't exist."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS detected_ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                page INTEGER NOT NULL,
                x_pct REAL NOT NULL,
                y_pct REAL NOT NULL,
                w_pct REAL NOT NULL,
                h_pct REAL NOT NULL,
                x_end_pct REAL NOT NULL,
                y_end_pct REAL NOT NULL,
                rect_ratio REAL,
                aspect REAL,
                cols INTEGER,
                confidence TEXT,
                image_filename TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_detected_ads_issue
                ON detected_ads(year, month, day)
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_detected_ads_uuid
                ON detected_ads(uuid)
        """)


def store_ads(db_path, year, month, day, page, ads_with_images):
    """
    Store detected ads in SQLite.

    Each ad dict must already carry a `uuid` (assigned in
    `extract_ad_images`). The integer auto-increment `id` is also
    populated by SQLite but no longer surfaced — workers identify
    ads by `uuid`, the parallel-pipeline-safe handle that doesn't
    require a DB round-trip to learn.

    Args:
        db_path:          Path to the SQLite database.
        year, month, day: Issue date.
        page:             Page number.
        ads_with_images:  List of ad dicts (from extract_ad_images).
    """
    init_ads_table(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cur = conn.cursor()
        for ad in ads_with_images:
            cur.execute("""
                INSERT INTO detected_ads
                (uuid, year, month, day, page, x_pct, y_pct, w_pct, h_pct,
                 x_end_pct, y_end_pct, rect_ratio, aspect, cols,
                 confidence, image_filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ad["uuid"],
                year, month, day, page,
                ad["x_pct"], ad["y_pct"], ad["w_pct"], ad["h_pct"],
                ad["x_end_pct"], ad["y_end_pct"],
                ad.get("rect_ratio"), ad.get("aspect"), ad.get("cols"),
                ad.get("confidence"), ad.get("image_filename"),
            ))


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python detect_ads.py <page.pdf> [--pitch N]")
        sys.exit(1)

    pdf = sys.argv[1]
    pitch = None
    if "--pitch" in sys.argv:
        idx = sys.argv.index("--pitch")
        if idx + 1 < len(sys.argv):
            pitch = float(sys.argv[idx + 1])

    ads = detect_ads(pdf, column_pitch=pitch)
    print(f"Detected {len(ads)} display ads:")
    print_ads(ads)

    zones = get_ad_exclusion_zones(ads)
    if zones:
        print(f"\nExclusion zones ({len(zones)}):")
        for x1, x2, y1, y2 in zones:
            print(f"  x={x1:.0f}%-{x2:.0f}%  y={y1:.0f}%-{y2:.0f}%")
