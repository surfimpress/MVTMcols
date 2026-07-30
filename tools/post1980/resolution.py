"""Post-snap resolution: orange regions + false ads → accurate boundaries.

Runs after `column_grid.snap_obstacles_to_grid` and produces a final,
cleanly-typed set of items where each bbox accurately matches the
underlying content.

The two problems addressed:

1. Orange (uncovered) regions still on the page after snap+dedup —
   some are orphan body columns that should extend a nearby article;
   some are missed articles entirely (headline detector failure); some
   are noise.

2. False display ads — the classical ad detector boxes some regions
   that aren't really ads. Common cases: bordered photos, boxed
   articles.

See /Users/peter/.claude/plans/rustling-hopping-reef.md for the
approved design.
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

import numpy as np

# Reuse the helpers in column_grid rather than copying them.
from post1980.column_grid import _bbox_contains, _drop_nested


# Standard IoU thresholds — the only remaining fixed numbers in this
# module. 0.5 is the standard "significant overlap"; 0.3 is the
# standard "modest overlap" (e.g. PASCAL VOC convention).
IOU_PHOTO_OVERLAP = 0.5
IOU_ARTICLE_OVERLAP = 0.3


# ----- Geometric helpers ------------------------------------------------

def _iou(a, b):
    """Intersection-over-union of two bboxes (x0,y0,x1,y1)."""
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    iw = max(0.0, ix1 - ix0); ih = max(0.0, iy1 - iy0)
    inter = iw * ih
    a_area = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    b_area = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = a_area + b_area - inter
    return inter / union if union > 0 else 0.0


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]))


def _column_range(bbox, gutters_xs, x_lo, x_hi):
    """Return (i, j) — the inclusive column-strip indices that this
    bbox occupies. Column strips are the regions between consecutive
    sorted x-edges where the edges are x_lo + sorted(gutters_xs) + x_hi.
    """
    edges = sorted([float(x_lo)] + [float(x) for x in gutters_xs]
                    + [float(x_hi)])
    # i: leftmost strip whose right edge > bbox[0]
    # j: rightmost strip whose left edge < bbox[2]
    n_strips = len(edges) - 1
    i = 0
    while i < n_strips - 1 and edges[i + 1] <= bbox[0]:
        i += 1
    j = n_strips - 1
    while j > 0 and edges[j] >= bbox[2]:
        j -= 1
    return (i, j)


def _column_overlap(a_cols, b_cols):
    return max(0, min(a_cols[1], b_cols[1]) - max(a_cols[0], b_cols[0]) + 1)


def _spans_inside(bbox, spans, body):
    """Return (body_span_count, large_span_count) for spans whose
    centre falls inside the bbox. Body = within ±25% of body font size.
    Large = ≥ 1.3 × body font size. Useful as a confidence-booster
    where OCR is reliable, but gating decisions are pixel-based."""
    body_count = 0
    large_count = 0
    body_lo = body * 0.75
    body_hi = body * 1.25
    large_thr = body * 1.3
    for s in spans:
        cx = (s.x0 + s.x1) / 2.0
        cy = (s.y0 + s.y1) / 2.0
        if not (bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]):
            continue
        if body_lo <= s.size <= body_hi:
            body_count += 1
        elif s.size >= large_thr:
            large_count += 1
    return body_count, large_count


def _pixel_pattern_stats(image, bbox, zoom, dark_thr=130):
    """Pixel-based ink-pattern statistics for a bbox.

    Returns a dict:
      density       : fraction of dark pixels in the bbox (0..1)
      row_std       : std of per-row dark-fraction (high = striated text,
                       low = uniform fill or empty)
      is_text_like  : moderate density AND high row_std — body-text rows
                       alternating with line-gap whitespace
      is_photo_like : high density AND low row_std — uniform dark fill
                       (a photo, solid box, or large display block)
      is_empty      : density below noise floor — no real content

    These three flags are mutually exclusive intentionally; an item
    that doesn't match any of them is ambiguous (e.g. mixed photo +
    caption) and falls through to OCR-based tiebreakers.

    Decision thresholds are absolute physical constants (intrinsic to
    newspaper print), not page-derived:
      - density floor of 0.04: scanner noise level.
      - text_like uses density in [0.05, 0.55] and row_std > 0.10.
      - photo_like uses density > 0.30 and row_std < 0.10.
    """
    if image is None or zoom <= 0:
        return {"density": None, "row_std": None,
                "is_text_like": None, "is_photo_like": None,
                "is_empty": None}
    H, W = image.shape[:2]
    x0_px = max(0, int(round(bbox[0] * zoom)))
    y0_px = max(0, int(round(bbox[1] * zoom)))
    x1_px = min(W, int(round(bbox[2] * zoom)))
    y1_px = min(H, int(round(bbox[3] * zoom)))
    if x1_px <= x0_px or y1_px <= y0_px:
        return {"density": 0.0, "row_std": 0.0,
                "is_text_like": False, "is_photo_like": False,
                "is_empty": True}
    region = image[y0_px:y1_px, x0_px:x1_px]
    dark = region < dark_thr
    density = float(dark.mean())
    row_density = dark.mean(axis=1)
    row_std = float(row_density.std())
    is_empty = density < 0.04
    is_photo_like = (density > 0.30) and (row_std < 0.10) and not is_empty
    is_text_like = (0.05 <= density <= 0.55) and (row_std > 0.10) and not is_photo_like
    return {"density": density, "row_std": row_std,
            "is_text_like": is_text_like, "is_photo_like": is_photo_like,
            "is_empty": is_empty}


# ----- Main resolution --------------------------------------------------

def resolve_obstacles(layers, spans, body,
                      page_image=None, image_zoom=None):
    """Resolve orange regions and false ads into accurate boundaries.

    Pure function — no PDF I/O. Operates on the snapped_* layers plus
    the structural grid.

    Args:
        layers: the cut_page layers dict (must contain snapped_* keys,
                scored_grid, whitespace_bands, text_area edges, etc.)
        spans: list of Span objects (extract_spans output)
        body: body font size in pt
        page_image: optional 2D uint8 grayscale numpy array of the whole
                    page. When provided, B1/B2/C4 gating decisions are
                    pixel-based and OCR-independent.
        image_zoom: pixels per pt for page_image (e.g. 100/72 ≈ 1.389
                    for a 100 DPI render). Required when page_image is
                    supplied.

    Returns:
        dict with keys: items, resolved_articles, resolved_display_ads,
        resolved_photos, resolved_pull_quotes, resolved_dropped,
        resolution_log.
    """
    log: List[str] = []

    # ----- Phase A — input gathering & tolerances ----------------------
    sg = layers.get("scored_grid") or {}
    pitch = float(sg.get("pitch") or 200.0)
    gutters_xs = [float(g["x_pt"])
                   for g in (sg.get("estimated_gutters") or [])]
    x_lo = float(layers.get("text_area_left_pt") or 0.0)
    x_hi_v = layers.get("text_area_right_pt")
    page_size = layers.get("page_size") or [0.0, 0.0]
    page_w = float(page_size[0]) if len(page_size) > 0 else 0.0
    page_h = float(page_size[1]) if len(page_size) > 1 else 0.0
    x_hi = float(x_hi_v) if x_hi_v is not None else page_w
    mast_y = float(layers.get("masthead_bottom") or 0.0)

    bands = layers.get("whitespace_bands") or []
    band_heights = [float(b[1]) - float(b[0]) for b in bands
                     if float(b[1]) > float(b[0])]
    median_band_h = float(np.median(band_heights)) if band_heights else (body * 2.0)

    # Data-derived tolerances
    tol_y_adj = median_band_h                       # "across a band"
    caption_max_h = body * 4.0                      # captions: 1–3 body rows + leading
    noise_max_w = pitch * 0.5
    noise_max_h = body * 2.0
    rows_min_for_article = max(3, int(median_band_h * 1.5 / max(1.0, body)))

    # Build the working item list
    items: List[Dict] = []

    def _push(type_, bbox, src_type, src_idx):
        if bbox is None: return
        b = tuple(float(v) for v in bbox)
        if b[2] <= b[0] or b[3] <= b[1]:
            return
        cols = _column_range(b, gutters_xs, x_lo, x_hi)
        body_n, large_n = _spans_inside(b, spans, body)
        # Pixel-based gating stats — computed once per item, used by
        # B1, B2, C4 in preference to span counts (OCR-independent).
        pix = _pixel_pattern_stats(page_image, b, image_zoom) \
            if (page_image is not None and image_zoom is not None) \
            else {"density": None, "row_std": None,
                  "is_text_like": None, "is_photo_like": None,
                  "is_empty": None}
        items.append({
            "type": type_,
            "bbox": b,
            "src_type": src_type,
            "src_idx": src_idx,
            "cols": cols,
            "body_span_count": body_n,
            "large_span_count": large_n,
            "pixel": pix,
            "type_history": [],
            "evidence": {"rule": "passthrough"},
            "snap_delta": (0.0, 0.0, 0.0, 0.0),
            "orig_bbox": b,
            "dropped": False,
            "untitled": False,
        })

    # Barrier check (used by C3 and G). A merge between two y-adjacent
    # bboxes is refused if any whitespace band, horizontal rule, or
    # pixel-detected headline sits in the gap or near it.
    pixel_runs_data_top = layers.get("pixel_headline_runs") or []
    h_rules_data_top = layers.get("horizontal_rules") or []
    bands_data_top = layers.get("whitespace_bands") or []

    def _has_barrier_top(x0, x1, y_above, y_below, near_tol,
                          ignore_in_bbox=None):
        """ignore_in_bbox: optional (x0,y0,x1,y1) — a headline that
        sits inside this bbox is the merge candidate's OWN headline,
        not a barrier. Used when an article is being grown to include
        adjacent content: the article's own headline shouldn't block
        the merge."""
        for (b_y0, b_y1) in bands_data_top:
            band_centre = (float(b_y0) + float(b_y1)) / 2.0
            if y_above < band_centre < y_below:
                return ("whitespace_band", band_centre)
        merge_w = max(1.0, x1 - x0)
        for r in h_rules_data_top:
            ry = r["y_pt"]
            if y_above < ry < y_below:
                rx0 = r["x0_pt"]; rx1 = r["x1_pt"]
                ov = max(0, min(x1, rx1) - max(x0, rx0))
                if ov / merge_w >= 0.5:
                    return ("horizontal_rule", ry)
        zone_lo = y_above - near_tol
        zone_hi = y_below + near_tol
        for hr in pixel_runs_data_top:
            if hr.get("n_chars", 0) < 3:
                continue
            # Skip headlines that belong to the above item (they're
            # that item's own headline, not a separator)
            if ignore_in_bbox is not None:
                hc_x = (hr["x0"] + hr["x1"]) / 2.0
                hc_y = (hr["y0"] + hr["y1"]) / 2.0
                ix0, iy0, ix1, iy1 = ignore_in_bbox
                if ix0 - 5 <= hc_x <= ix1 + 5 and iy0 - 5 <= hc_y <= iy1 + 5:
                    continue
            if zone_lo <= hr["y0"] <= zone_hi:
                x_ov = max(0, min(x1, hr["x1"]) - max(x0, hr["x0"]))
                if x_ov >= 20:
                    return ("headline", hr["y0"])
        return None

    # For articles, match each snapped bbox back to its source dict so
    # we can use the headline bbox for growth bounds.
    source_articles = list(layers.get("articles") or [])

    def _find_source_article(snapped_bbox):
        best = None; best_iou = 0.0
        for art in source_articles:
            ab = art.get("bbox") if isinstance(art, dict) else None
            if ab is None: continue
            iou = _iou(snapped_bbox, ab)
            if iou > best_iou:
                best_iou = iou; best = art
        return best if best_iou > 0.3 else None

    for i, b in enumerate(layers.get("snapped_articles") or []):
        _push("article", b, "article", i)
        src = _find_source_article(tuple(float(v) for v in b))
        if src and isinstance(src, dict):
            hl = src.get("headline") or {}
            hl_bb = hl.get("bbox")
            if hl_bb and len(hl_bb) >= 4:
                items[-1]["headline_bbox"] = tuple(float(v) for v in hl_bb[:4])
    for i, b in enumerate(layers.get("snapped_display_ads") or []):
        _push("ad", b, "ad", i)
    for i, b in enumerate(layers.get("snapped_photos") or []):
        _push("photo", b, "photo", i)
    for i, b in enumerate(layers.get("snapped_uncovered") or []):
        _push("uncovered", b, "uncovered", i)
    # pull-quotes passthrough — not reclassified per user direction.
    pull_quotes_raw = layers.get("pull_quotes") or []
    pull_quotes_bboxes = [tuple(float(v) for v in pq[:4])
                          for pq in pull_quotes_raw if len(pq) >= 4]

    # ----- Phase B — reclassify false ads ------------------------------
    photos_now = [it for it in items if it["type"] == "photo" and not it["dropped"]]
    articles_now = [it for it in items if it["type"] == "article" and not it["dropped"]]

    # Helpers for B3 (thin-border sidebar) check
    def _measure_top_border_px(bbox):
        """Number of consecutive dark rows from the top edge of bbox
        (how thick is the top horizontal rule of this box)."""
        if page_image is None or image_zoom is None:
            return None
        H, W = page_image.shape[:2]
        z = image_zoom
        x0 = max(0, int(round(bbox[0] * z)))
        y0 = max(0, int(round(bbox[1] * z)))
        x1 = min(W, int(round(bbox[2] * z)))
        if x1 <= x0 + 2 or y0 >= H:
            return None
        thickness = 0
        for off in range(min(15, H - y0)):
            row = page_image[y0 + off, x0:x1]
            dark_frac = float((row < 130).mean())
            if dark_frac >= 0.5:
                thickness += 1
            else:
                break
        return thickness

    def _ad_contains_pixel_headline(ad_bbox):
        for hr in pixel_runs_data_top:
            if hr.get("n_chars", 0) < 4:
                continue
            hcx = (hr["x0"] + hr["x1"]) / 2.0
            hcy = (hr["y0"] + hr["y1"]) / 2.0
            if (ad_bbox[0] <= hcx <= ad_bbox[2]
                    and ad_bbox[1] <= hcy <= ad_bbox[3]):
                return True
        return False

    for ad in [it for it in items if it["type"] == "ad" and not it["dropped"]]:
        pix = ad["pixel"]
        # B1 → photo. Two paths:
        #   (a) ad overlaps an existing photo with high IoU → drop;
        #       photo wins regardless of ad interior.
        #   (b) ad has no text-row striations AND is either dense
        #       (photo-like fill) or near-empty → promote to photo or
        #       just drop. Pixel-based; falls back to span counts when
        #       pixel data isn't available.
        overlapping_photos = [p for p in photos_now
                              if _iou(ad["bbox"], p["bbox"]) >= IOU_PHOTO_OVERLAP]
        if overlapping_photos:
            ad["dropped"] = True
            ad["evidence"] = {"rule": "B1_ad_dropped_photo_wins",
                              "iou": _iou(ad["bbox"], overlapping_photos[0]["bbox"])}
            log.append(f"B1: ad #{ad['src_idx']} dropped (photo overlap)")
            continue
        if pix["is_text_like"] is None:
            no_text = (ad["body_span_count"] == 0
                       and ad["large_span_count"] == 0)
        else:
            no_text = (pix["is_photo_like"] is True
                       or pix["is_empty"] is True)
        if no_text:
            ad["type"] = "photo"
            ad["type_history"].append("ad")
            ad["evidence"] = {"rule": "B1_ad_to_photo_empty_interior",
                              "pixel": {k: pix[k] for k in
                                ("density", "row_std", "is_photo_like",
                                 "is_empty")}}
            log.append(f"B1: ad #{ad['src_idx']} → photo "
                        f"(density={pix['density']}, "
                        f"row_std={pix['row_std']})")
            continue

        # B2 → article: ad has text-row striations AND overlaps an article.
        if pix["is_text_like"] is None:
            looks_texty = ad["body_span_count"] >= rows_min_for_article
        else:
            looks_texty = pix["is_text_like"] is True
        if looks_texty:
            overlapping_articles = [(a, _iou(ad["bbox"], a["bbox"]))
                                    for a in articles_now
                                    if _iou(ad["bbox"], a["bbox"]) >= IOU_ARTICLE_OVERLAP]
            if overlapping_articles:
                best = max(overlapping_articles, key=lambda t: t[1])[0]
                best["bbox"] = _union(best["bbox"], ad["bbox"])
                best["evidence"] = {"rule": "B2_article_grew_by_ad_merge",
                                     "merged_ad_src_idx": ad["src_idx"]}
                ad["dropped"] = True
                ad["evidence"] = {"rule": "B2_ad_dropped_merged_into_article",
                                   "into_article_src_idx": best["src_idx"]}
                log.append(f"B2: ad #{ad['src_idx']} → merged into "
                            f"article #{best['src_idx']}")
                continue

        # B3 → sidebar article: ad has a THIN top border AND contains
        # a pixel-detected headline. Sidebar articles (boxed text
        # columns) typically use a thin rule frame; display ads use
        # a heavier border. Per user direction 2026-05-19.
        top_border_px = _measure_top_border_px(ad["bbox"])
        if top_border_px is not None and top_border_px <= 2:
            if _ad_contains_pixel_headline(ad["bbox"]):
                ad["type"] = "article"
                ad["type_history"].append("ad")
                ad["evidence"] = {
                    "rule": "B3_thin_border_sidebar_article",
                    "top_border_px": top_border_px,
                }
                log.append(f"B3: ad #{ad['src_idx']} → sidebar article "
                            f"(top border {top_border_px}px)")
                continue

        # Else: leave as ad

    # ----- Phase C — resolve uncovered regions -------------------------
    # Refresh living lists after Phase B
    photos_now = [it for it in items if it["type"] == "photo" and not it["dropped"]]
    articles_now = [it for it in items if it["type"] == "article" and not it["dropped"]]

    for u in [it for it in items if it["type"] == "uncovered" and not it["dropped"]]:
        w = u["bbox"][2] - u["bbox"][0]
        h = u["bbox"][3] - u["bbox"][1]

        # C1 noise drop
        if w < noise_max_w and h < noise_max_h:
            u["dropped"] = True
            u["evidence"] = {"rule": "C1_noise_too_small"}
            log.append(f"C1: orange #{u['src_idx']} dropped (too small)")
            continue
        if u["bbox"][3] <= mast_y or u["bbox"][1] >= page_h - 30:
            u["dropped"] = True
            u["evidence"] = {"rule": "C1_out_of_content"}
            log.append(f"C1: orange #{u['src_idx']} dropped (outside content)")
            continue

        # C2 caption attach: a short orange (≤ 4 body-rows tall) sitting
        # just above or below an existing item, in the same column
        # range, is its caption. Try photos first (the classic case),
        # then articles (caption between photo and body text, or
        # under the article when the body extends further than detected).
        if h <= caption_max_h:
            attached = False
            for p in photos_now:
                if _column_overlap(u["cols"], p["cols"]) >= 1:
                    gap_below = u["bbox"][1] - p["bbox"][3]
                    gap_above = p["bbox"][1] - u["bbox"][3]
                    if (0 <= gap_below <= tol_y_adj
                            or 0 <= gap_above <= tol_y_adj):
                        p["bbox"] = _union(p["bbox"], u["bbox"])
                        p["evidence"] = {"rule": "C2_photo_grew_by_caption",
                                          "captured_uncovered_src_idx": u["src_idx"]}
                        u["dropped"] = True
                        u["evidence"] = {"rule": "C2_caption_attached_to_photo",
                                          "into_photo_src_idx": p["src_idx"]}
                        log.append(f"C2: orange #{u['src_idx']} → photo "
                                    f"caption #{p['src_idx']}")
                        attached = True
                        break
            # Per user direction 2026-05-18: captions attach to images
            # only, NOT to articles. If no photo is detected nearby,
            # leave the orange alone — a future pixel-photo detector
            # will resolve these cases.
            if attached:
                continue

        # C3 orange → article merge (headline-aware). Per user
        # direction 2026-05-18:
        #  - An article's RIGHTFUL x-extent is its HEADLINE'S x-extent
        #    (the headline tells you how wide the article should be).
        #  - The orange can sit anywhere in the union(body, headline)
        #    column-range to be considered for merge.
        #  - After merge, the article's x is CLAMPED to its headline
        #    x-extent — preventing greedy expansion that swallows
        #    unrelated content (the Hallas case).
        # Downward-only merge (per user direction 2026-05-18): an
        # article's top is its headline; anything above the headline
        # isn't part of the article. So orange may only merge into an
        # article from BELOW (extending the body), never from above.
        best_article = None
        best_cols_ov = 0
        best_gap = float("inf")
        for a in articles_now:
            hl_cols = a["cols"]
            hl_bbox = a.get("headline_bbox")
            if hl_bbox:
                hl_cols_only = _column_range(hl_bbox, gutters_xs, x_lo, x_hi)
                hl_cols = (min(hl_cols[0], hl_cols_only[0]),
                            max(hl_cols[1], hl_cols_only[1]))
            cols_ov = _column_overlap(u["cols"], hl_cols)
            if cols_ov < 1:
                continue
            # Only orange whose top is at or below the article's bottom
            gap_below = u["bbox"][1] - a["bbox"][3]
            if not (0 <= gap_below <= tol_y_adj):
                continue
            if cols_ov > best_cols_ov or (cols_ov == best_cols_ov and gap_below < best_gap):
                best_article = a
                best_cols_ov = cols_ov
                best_gap = gap_below
        if best_article:
            # Barrier check (per user direction 2026-05-19): refuse
            # the merge if a whitespace band, horizontal rule, or
            # headline sits between the article and the orange.
            x0 = max(best_article["bbox"][0], u["bbox"][0])
            x1 = min(best_article["bbox"][2], u["bbox"][2])
            bar = _has_barrier_top(x0, x1,
                                    best_article["bbox"][3],
                                    u["bbox"][1],
                                    near_tol=tol_y_adj,
                                    ignore_in_bbox=best_article["bbox"])
            if bar is not None:
                continue
            new_bbox = _union(best_article["bbox"], u["bbox"])
            # Clamp x-range to the headline's x-extent if known
            hl_bbox = best_article.get("headline_bbox")
            if hl_bbox:
                hl_x_lo = min(hl_bbox[0], best_article["orig_bbox"][0])
                hl_x_hi = max(hl_bbox[2], best_article["orig_bbox"][2])
                new_bbox = (max(new_bbox[0], hl_x_lo),
                             new_bbox[1],
                             min(new_bbox[2], hl_x_hi),
                             new_bbox[3])
            # Anti-greedy: don't let the article grow over another
            # existing item. Two checks:
            #  - IoU > 0.3 (significant partial overlap)
            #  - new_bbox would wholly contain another item (catches the
            #    "swallow a small box" case where IoU is low because
            #    the contained item is much smaller than the merged
            #    bbox).
            crosses_other = False
            for other in items:
                if other is best_article or other["dropped"]:
                    continue
                if other["type"] not in ("article", "ad", "photo"):
                    continue
                if _iou(new_bbox, other["bbox"]) > 0.3 \
                        or _bbox_contains(new_bbox, other["bbox"], slack=5):
                    crosses_other = True
                    break
            if crosses_other:
                continue
            best_article["bbox"] = new_bbox
            best_article["evidence"] = {"rule": "C3_article_grew_by_orange_merge",
                                         "merged_orange_src_idx": u["src_idx"],
                                         "cols_overlap": best_cols_ov,
                                         "y_gap_pt": round(best_gap, 2),
                                         "clamped_to_headline": hl_bbox is not None}
            u["dropped"] = True
            u["evidence"] = {"rule": "C3_orange_merged_into_article",
                              "into_article_src_idx": best_article["src_idx"]}
            log.append(f"C3: orange #{u['src_idx']} → article "
                        f"#{best_article['src_idx']} (cols={best_cols_ov}, "
                        f"gap={best_gap:.1f})")
            continue

        # C4 orange → untitled article (conservative): orange spans
        # ≥ 1 column wide AND looks text-like in pixel pattern
        # (alternating dark/light rows = body text). Falls back to
        # body-span-count when no pixel data is available.
        n_cols = u["cols"][1] - u["cols"][0] + 1
        u_pix = u["pixel"]
        if u_pix["is_text_like"] is None:
            text_present = u["body_span_count"] >= rows_min_for_article
        else:
            text_present = u_pix["is_text_like"] is True
        if n_cols >= 1 and text_present:
            u["type"] = "article"
            u["type_history"].append("uncovered")
            u["untitled"] = True
            u["evidence"] = {"rule": "C4_uncovered_promoted_to_untitled_article",
                              "n_cols": n_cols,
                              "body_spans": u["body_span_count"],
                              "pixel": {k: u_pix[k] for k in
                                ("density", "row_std", "is_text_like")}}
            log.append(f"C4: orange #{u['src_idx']} → untitled article "
                        f"({n_cols} cols, density={u_pix['density']}, "
                        f"row_std={u_pix['row_std']})")
            continue

        # C5 else: drop
        u["dropped"] = True
        u["evidence"] = {"rule": "C5_no_match"}
        log.append(f"C5: orange #{u['src_idx']} dropped (no rule matched)")

    # ----- Phase G — vertical merge of adjacent articles --------------
    # Per user direction 2026-05-18: sometimes vertically-stacked
    # boxes are really one item. Merge two adjacent articles in the
    # same column range IF no barrier sits between them. Barriers are
    # any of:
    #   (1) a whitespace band intersecting the y-gap
    #   (2) a horizontal rule in the y-gap with x-coverage of the
    #       merge column range
    #   (3) a pixel-detected headline inside the y-gap
    # Use the top-level barrier helper (defined earlier).
    def _has_barrier(x0, x1, y_above, y_below, near_tol=None,
                     ignore_in_bbox=None):
        if near_tol is None:
            near_tol = tol_y_adj
        return _has_barrier_top(x0, x1, y_above, y_below,
                                 near_tol=near_tol,
                                 ignore_in_bbox=ignore_in_bbox)

    # Iterate until no more merges happen
    while True:
        live_arts = [it for it in items
                     if it["type"] == "article" and not it["dropped"]]
        live_arts.sort(key=lambda a: a["bbox"][1])
        merged = False
        for i, a_above in enumerate(live_arts):
            if a_above["dropped"]: continue
            for j in range(i + 1, len(live_arts)):
                a_below = live_arts[j]
                if a_below["dropped"]: continue
                # a_above must end before a_below starts vertically
                if a_above["bbox"][3] >= a_below["bbox"][1]:
                    continue
                # Reasonable y-gap (don't merge across half the page)
                gap = a_below["bbox"][1] - a_above["bbox"][3]
                if gap > tol_y_adj * 3:
                    continue
                # Significant column overlap required
                cols_ov = _column_overlap(a_above["cols"], a_below["cols"])
                if cols_ov < 1:
                    continue
                # Check barriers in the merge x-range
                x0 = max(a_above["bbox"][0], a_below["bbox"][0])
                x1 = min(a_above["bbox"][2], a_below["bbox"][2])
                bar = _has_barrier(x0, x1,
                                    a_above["bbox"][3], a_below["bbox"][1],
                                    ignore_in_bbox=a_above["bbox"])
                if bar is not None:
                    continue
                # Anti-greedy: refuse the merge if the resulting union
                # bbox would significantly overlap an unrelated item.
                # The two-bbox union over-claims when the items don't
                # have the same x-range — phantom corners can swallow
                # neighbouring ads/articles.
                proposed = _union(a_above["bbox"], a_below["bbox"])
                crosses = False
                for other in items:
                    if other is a_above or other is a_below:
                        continue
                    if other["dropped"]:
                        continue
                    if other["type"] not in ("article", "ad", "photo"):
                        continue
                    if _iou(proposed, other["bbox"]) > 0.3 \
                            or _bbox_contains(proposed, other["bbox"], slack=5):
                        crosses = True
                        break
                if crosses:
                    continue
                # Merge a_below into a_above
                a_above["bbox"] = proposed
                a_above["evidence"] = {
                    "rule": "G_article_merged_with_adjacent",
                    "merged_with_src_idx": a_below["src_idx"],
                    "y_gap_pt": round(gap, 2),
                }
                a_below["dropped"] = True
                a_below["evidence"] = {
                    "rule": "G_article_dropped_merged_into_above",
                    "into_article_src_idx": a_above["src_idx"],
                }
                log.append(f"G: article #{a_below['src_idx']} merged "
                            f"with article #{a_above['src_idx']} above "
                            f"(gap={gap:.1f})")
                merged = True
                break
            if merged:
                break
        if not merged:
            break

    # ----- Phase H — final classification of any remaining orange ---
    # By design (per user direction 2026-05-18), the final output must
    # categorise every region of the page as article / ad / photo.
    # Anything still uncovered after C1–C5 and G gets one last
    # classification pass here. No orange in the final.
    #
    #   is_empty       → drop (no content to classify)
    #   is_photo_like  → photo (dense uniform fill)
    #   otherwise      → untitled article (any other content)
    for u in [it for it in items
              if it["type"] == "uncovered" and not it["dropped"]]:
        pix = u["pixel"]
        if pix["is_empty"] is True:
            u["dropped"] = True
            u["evidence"] = {"rule": "H_orange_dropped_empty",
                              "pixel": {k: pix[k]
                                for k in ("density", "row_std")}}
            log.append(f"H: orange #{u['src_idx']} dropped (empty)")
            continue
        if pix["is_photo_like"] is True:
            u["type"] = "photo"
            u["type_history"].append("uncovered")
            u["evidence"] = {"rule": "H_orange_to_photo",
                              "pixel": {k: pix[k]
                                for k in ("density", "row_std")}}
            log.append(f"H: orange #{u['src_idx']} → photo "
                        f"(density={pix['density']:.2f})")
            continue
        # Anything else with content → untitled article
        u["type"] = "article"
        u["untitled"] = True
        u["type_history"].append("uncovered")
        u["evidence"] = {"rule": "H_orange_to_untitled_article",
                          "pixel": {k: pix[k]
                            for k in ("density", "row_std",
                                       "is_text_like", "is_photo_like")}}
        log.append(f"H: orange #{u['src_idx']} → untitled article "
                    f"(density={pix['density']}, row_std={pix['row_std']})")

    # ----- Phase F — horizontal merge of adjacent untitled articles -
    # Column-by-column orange detection splits a single row of body
    # text into per-column slivers, each promoted to untitled article
    # via C4 or H. Merge horizontally-adjacent untitled articles in
    # the same y-band so a row of fragments becomes one article.
    def _h_merge_round():
        live = [it for it in items
                if it["type"] == "article" and it.get("untitled")
                and not it["dropped"]]
        live.sort(key=lambda a: (a["bbox"][1], a["bbox"][0]))
        for i, a in enumerate(live):
            if a["dropped"]:
                continue
            for j in range(i + 1, len(live)):
                b = live[j]
                if b["dropped"]:
                    continue
                a_yc = (a["bbox"][1] + a["bbox"][3]) / 2.0
                b_yc = (b["bbox"][1] + b["bbox"][3]) / 2.0
                if abs(a_yc - b_yc) > median_band_h * 0.5:
                    continue
                # Horizontally adjacent (small x-gap)
                if b["bbox"][0] >= a["bbox"][2]:
                    x_gap = b["bbox"][0] - a["bbox"][2]
                elif a["bbox"][0] >= b["bbox"][2]:
                    x_gap = a["bbox"][0] - b["bbox"][2]
                else:
                    x_gap = 0   # they overlap in x
                if x_gap > pitch * 0.5:
                    continue
                proposed = _union(a["bbox"], b["bbox"])
                crosses = False
                for other in items:
                    if other is a or other is b or other["dropped"]:
                        continue
                    if other["type"] not in ("article", "ad", "photo"):
                        continue
                    if _iou(proposed, other["bbox"]) > 0.3 \
                            or _bbox_contains(proposed, other["bbox"], slack=5):
                        crosses = True
                        break
                if crosses:
                    continue
                a["bbox"] = proposed
                b["dropped"] = True
                b["evidence"] = {"rule": "F_untitled_merged_horizontally",
                                  "into_article_src_idx": a["src_idx"]}
                log.append(f"F: untitled article #{b['src_idx']} merged "
                            f"horizontally with #{a['src_idx']}")
                return True
        return False

    while _h_merge_round():
        pass

    # ----- Phase D — re-dedup nested items -----------------------------
    living = [it for it in items if not it["dropped"]]
    # Build dict-of-lists matching _drop_nested's expected shape
    type_to_bboxes: Dict[str, List[Tuple]] = {}
    type_to_items: Dict[str, List[Dict]] = {}
    for it in living:
        type_to_bboxes.setdefault(it["type"], []).append(it["bbox"])
        type_to_items.setdefault(it["type"], []).append(it)
    deduped = _drop_nested(type_to_bboxes)
    # Drop items whose bbox isn't in the deduped result
    for type_, items_of_type in type_to_items.items():
        kept_bboxes = set(tuple(b) for b in deduped.get(type_, []))
        for it in items_of_type:
            if tuple(it["bbox"]) not in kept_bboxes:
                it["dropped"] = True
                # Keep its evidence; tag the drop reason
                it["evidence"] = {"rule": "D_dropped_nested_after_resolution",
                                   **(it.get("evidence") or {})}
                log.append(f"D: {type_} #{it['src_idx']} dropped "
                            f"(nested after resolution)")

    # ----- Phase E — trim, record snap_delta, emit --------------------
    # Trim ads and articles back to the content area: x in
    # [text_area_left, text_area_right], y in [masthead_bottom,
    # page_h]. text_area_top/bottom_pt is the bbox of central body
    # text and would be too restrictive.
    mast_y_v = layers.get("masthead_bottom") or 0.0
    y_top = float(mast_y_v)
    y_bot = float(page_h) - 20.0   # footer margin (matches snap)
    for it in items:
        if it["dropped"]:
            continue
        if it["type"] in ("ad", "article"):
            b = it["bbox"]
            tx0 = max(b[0], x_lo); ty0 = max(b[1], y_top)
            tx1 = min(b[2], x_hi); ty1 = min(b[3], y_bot)
            if tx1 <= tx0 or ty1 <= ty0:
                # Trim collapsed it — entire bbox was in the margin
                it["dropped"] = True
                it["evidence"] = {"rule": "E_trim_collapsed_into_margin",
                                   **(it.get("evidence") or {})}
                log.append(f"E: {it['type']} #{it['src_idx']} dropped "
                            f"(trim collapsed into margin)")
                continue
            it["bbox"] = (tx0, ty0, tx1, ty1)
        ob = it["orig_bbox"]
        nb = it["bbox"]
        it["snap_delta"] = (round(nb[0] - ob[0], 2),
                             round(nb[1] - ob[1], 2),
                             round(nb[2] - ob[2], 2),
                             round(nb[3] - ob[3], 2))

    def _serialize(it):
        pix = it.get("pixel") or {}
        return {
            "type": it["type"],
            "bbox": list(it["bbox"]),
            "src_type": it["src_type"],
            "src_idx": it["src_idx"],
            "type_history": list(it["type_history"]),
            "snap_delta": list(it["snap_delta"]),
            "evidence": it.get("evidence") or {},
            "untitled": it.get("untitled", False),
            "pixel_density": pix.get("density"),
            "pixel_row_std": pix.get("row_std"),
        }

    serialized_items = [_serialize(it) for it in items if not it["dropped"]]

    by_type = {"article": [], "ad": [], "photo": [], "uncovered": []}
    for it in items:
        if it["dropped"]:
            continue
        if it["type"] in by_type:
            by_type[it["type"]].append(list(it["bbox"]))

    return {
        "items": serialized_items,
        "resolved_articles":    by_type["article"],
        "resolved_display_ads": by_type["ad"],
        "resolved_photos":      by_type["photo"],
        "resolved_uncovered":   by_type["uncovered"],
        "resolved_pull_quotes": [list(b) for b in pull_quotes_bboxes],
        "resolved_dropped":     [list(it["bbox"]) for it in items if it["dropped"]],
        "resolution_log":       log,
    }
