"""
Decomposed column detection pipeline.

Each function takes (data, context) and returns data.
No side effects, no database queries, no mutation of upstream results.
The PageContext carries all knowledge needed for decisions.

Pipeline stages:
    detect_strips()       → raw boundaries per strip
    cluster_boundaries()  → merge nearby detections
    place_columns()       → final positions by page type

Page type strategies:
    place_standard()          → project from interior + clean edge
    place_page2_editorial()   → arithmetic: start + 2×(1.5p) + 4×p
"""

import numpy as np
from find_columns import find_column_boundaries, find_column_boundaries_morph


# ── Stage 1: Detection ───────────────────────────────────────────────────────

# Grid rows for multi-strip consensus (1-indexed, 10% blocks).
# Skip row 1 (masthead) and row 10 (bottom margin).
CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]

# Strip weighting — middle strips are most reliable for grid
# detection (body text, fewer ads). Edge strips help measure skew
# but are noisy.
STRIP_WEIGHTS = {
    3: 0.5,   # upper — ads, mastheads
    4: 0.8,   # upper-mid
    5: 1.0,   # mid — best body text
    6: 1.0,   # mid — best body text
    7: 0.8,   # lower-mid
    8: 0.5,   # lower — ads, footers
    9: 0.3,   # bottom — margin noise
}


def detect_strips(pdf_path, ctx, dpi=450):
    """
    Run find_column_boundaries on each strip within R3 (the newspaper
    page boundary). We use R3 rather than text_area to avoid missing
    columns near the edges — text_area can be too aggressive.

    Returns (detections, strip_profiles).
    """
    clip_x = (ctx.r3_left / 100, ctx.r3_right / 100)
    dark_thresh = max(60, int(ctx.column_darkness_threshold))
    std_thresh = int(ctx.row_std_threshold)

    all_detections = []

    # ── Morphological vertical rule detection ───────────────────────
    # Uses a tall vertical kernel to isolate column rules directly.
    # More effective than Hough on heritage scans with thin, faint rules.
    # Catches rules the darkness-peak method misses near page edges.
    for grid_y in [5, 6]:  # middle strips — morphological is most reliable here
        try:
            morph_results = find_column_boundaries_morph(
                pdf_path, x=1, y=grid_y, w=10, h=1,
                page_number=0, dpi=dpi,
                clip_x_frac=clip_x,
            )
            strip_y_start = (grid_y - 1) * 10
            strip_y_end = grid_y * 10
            for r in morph_results:
                if r.confidence not in ("high", "medium"):
                    continue
                # Apply same ad exclusion as darkness-peak detections
                in_ad = False
                for ax1, ax2, ay1, ay2 in ctx.ad_zones:
                    if ax1 < r.page_pct < ax2 and ay1 < strip_y_end and ay2 > strip_y_start:
                        in_ad = True
                        break
                if in_ad:
                    continue
                all_detections.append({
                    "pct": r.page_pct,
                    "confidence": r.confidence,
                    "row_std": 0,
                    "valley_depth": r.valley_depth,
                    "darkness": r.peak_darkness,
                    "strip": grid_y,
                    "weight": STRIP_WEIGHTS.get(grid_y, 1.0),
                })
        except Exception:
            pass

    # ── Darkness-peak detection (original method) ────────────────────
    strip_profiles = []
    for grid_y in CONSENSUS_ROWS:
        strip_weight = STRIP_WEIGHTS.get(grid_y, 0.5)
        strip_y_start = (grid_y - 1) * 10
        strip_y_end = grid_y * 10

        try:
            results, profile = find_column_boundaries(
                pdf_path, x=1, y=grid_y, w=10, h=1,
                page_number=0, dpi=dpi,
                darkness_threshold=dark_thresh,
                clip_x_frac=clip_x,
                return_profile=True,
            )
        except Exception:
            continue

        strip_profiles.append({
            "strip": grid_y,
            "y_start_pct": strip_y_start,
            "y_end_pct": strip_y_end,
            "profile": profile,
        })

        for r in results:
            # Skip boundaries inside ad exclusion zones
            in_ad = False
            for ax1, ax2, ay1, ay2 in ctx.ad_zones:
                if ax1 < r.page_pct < ax2 and ay1 < strip_y_end and ay2 > strip_y_start:
                    in_ad = True
                    break
            if in_ad:
                continue

            # Accept high/medium confidence, valleys, or low with good structure
            if r.confidence in ("high", "medium"):
                weight = strip_weight
            elif r.confidence == "valley":
                weight = strip_weight * 0.5
            elif r.row_std < std_thresh:
                weight = strip_weight * 0.5
            else:
                continue

            all_detections.append({
                "pct": r.page_pct,
                "confidence": r.confidence,
                "row_std": r.row_std,
                "valley_depth": r.valley_depth,
                "darkness": r.peak_darkness,
                "strip": grid_y,
                "weight": weight,
            })

    return all_detections, strip_profiles, dark_thresh


# ── Stage 2: Clustering ──────────────────────────────────────────────────────

def cluster_boundaries(detections, merge_distance=2.0,
                       strip_profiles=None, ad_zones=None):
    """
    Cluster nearby detections and compute weighted positions.
    Optionally reinforces/penalises using the composite rate-of-change
    signal from strip profiles.

    Returns list of boundary dicts with position, confidence,
    strip count, weighted score, and drift.
    """
    if not detections:
        return []

    detections.sort(key=lambda d: d["pct"])
    clusters = [[detections[0]]]
    for d in detections[1:]:
        if d["pct"] - clusters[-1][-1]["pct"] < merge_distance:
            clusters[-1].append(d)
        else:
            clusters.append([d])

    # ── Build composite rate-of-change from strip profiles ────────
    # Sum strip values (zeroing ad zones), then compute abs change.
    change_at_pct = None
    if strip_profiles and len(strip_profiles) > 0:
        ref = strip_profiles[0]["profile"]
        composite = [0.0] * len(ref)
        pcts = [p["pct"] for p in ref]
        ad_z = ad_zones or []

        for strip in strip_profiles:
            sp = strip["profile"]
            y1, y2 = strip["y_start_pct"], strip["y_end_pct"]
            for i, pt in enumerate(sp):
                if i >= len(composite):
                    break
                in_ad = any(
                    az[0] < pt["pct"] < az[1] and az[2] < y2 and az[3] > y1
                    for az in ad_z
                )
                if not in_ad:
                    composite[i] += pt["val"]

        # Absolute change
        changes = [0.0]
        for i in range(1, len(composite)):
            changes.append(abs(composite[i] - composite[i - 1]))
        max_change = max(changes) if changes else 1

        # Build a lookup: for a given page-%, what's the normalised
        # change score (0-1) in the nearest 2% window?
        def _change_near(target_pct, window=1.0):
            best = 0.0
            for i, p in enumerate(pcts):
                if abs(p - target_pct) <= window:
                    if changes[i] > best:
                        best = changes[i]
            return best / max_change if max_change > 0 else 0

        change_at_pct = _change_near

    boundaries = []
    for cluster in clusters:
        strips_hit = len(set(d["strip"] for d in cluster))
        weighted_score = sum(d["weight"] for d in cluster)
        total_w = sum(d["weight"] for d in cluster)

        if total_w > 0:
            wmean = sum(d["pct"] * d["weight"] for d in cluster) / total_w
        else:
            wmean = np.mean([d["pct"] for d in cluster])

        best = min(cluster, key=lambda d: d["row_std"])

        if strips_hit >= 2:
            drift = max(d["pct"] for d in cluster) - min(d["pct"] for d in cluster)
        else:
            drift = 0.0

        # Reinforce/penalise using composite rate-of-change.
        # A change spike within 2% of the boundary reinforces it.
        # No change spike nearby marks it as uncertain.
        change_score = 0.0
        if change_at_pct:
            change_score = change_at_pct(wmean)
            # Boost: up to 50% extra score for strong change agreement
            weighted_score *= (1.0 + change_score * 0.5)
            # Penalise: if change is very weak, reduce score
            if change_score < 0.1:
                weighted_score *= 0.7

        # How many strips were available (not blocked by ads) at this x?
        available_strips = len(CONSENSUS_ROWS)
        if ad_zones:
            for row in CONSENSUS_ROWS:
                y1 = (row - 1) * 10
                y2 = row * 10
                for az in ad_zones:
                    if az[0] < wmean < az[1] and az[2] < y2 and az[3] > y1:
                        available_strips -= 1
                        break

        # Scale threshold by coverage: if only 2 of 7 strips are
        # available, threshold drops proportionally
        coverage = max(available_strips, 1) / len(CONSENSUS_ROWS)
        accept_thresh = 1.5 * coverage

        # Accept if reasonable support relative to what was available
        if weighted_score >= accept_thresh or strips_hit >= min(3, available_strips):
            boundaries.append({
                "x_pct": round(float(wmean), 2),
                "confidence": best["confidence"],
                "row_std": best["row_std"],
                "valley_depth": best["valley_depth"],
                "peak_darkness": best["darkness"],
                "strips_hit": strips_hit,
                "weighted_score": round(weighted_score, 2),
                "drift": round(drift, 2),
            })

    return sorted(boundaries, key=lambda b: b["x_pct"])


# ── Stage 3: Place columns by page type ──────────────────────────────────────

def place_columns(boundaries, ctx):
    """
    Place final column positions based on page type and context.

    Dispatches to the appropriate strategy.
    """
    if ctx.is_page_2 and ctx.page_2_template:
        return place_page2_editorial(boundaries, ctx)
    else:
        return place_standard(boundaries, ctx)


def place_page2_editorial(boundaries, ctx):
    """
    Place columns for page 2 editorial layout.

    Pattern: 2 wide columns (1.5× pitch) + 4 regular columns.
    Build the pattern as intervals, then slide it to best align
    with detected boundaries, same as place_standard but with
    non-uniform spacing.
    """
    pitch = ctx.pitch
    wide = ctx.wide_pitch

    # Build the interval pattern: [wide, wide, pitch, pitch, pitch, pitch]
    # This gives 7 boundaries for 6 columns.
    intervals = [wide, wide] + [pitch] * 4

    if not boundaries:
        return _boundaries_from_positions(ctx.expected_boundaries)

    # Estimate per-page pitch from detections (same as place_standard)
    ref_pitch = ctx.pitch
    page_gaps = []
    if len(boundaries) >= 2:
        positions = sorted(b["x_pct"] for b in boundaries)
        for i in range(len(positions) - 1):
            g = positions[i + 1] - positions[i]
            if ref_pitch * 0.7 < g < ref_pitch * 1.4:
                page_gaps.append(g)
            elif ref_pitch * 1.4 <= g < ref_pitch * 2.5:
                page_gaps.append(g / 2)
    if len(page_gaps) >= 3:
        q1 = float(np.percentile(page_gaps, 25))
        q3 = float(np.percentile(page_gaps, 75))
        iqr = q3 - q1
        filtered = [g for g in page_gaps if q1 - 1.5 * iqr <= g <= q3 + 1.5 * iqr]
        pitch = round(float(np.median(filtered if filtered else page_gaps)), 2)
    elif page_gaps:
        pitch = round(float(np.median(page_gaps)), 2)

    wide = round(pitch * 1.5, 2)
    intervals = [wide, wide] + [pitch] * 4

    # Build targets from detected boundaries (log-weighted)
    targets = []
    for b in boundaries:
        raw_score = max(b.get("weighted_score", 1.0), 0.1)
        weight = 1.0 + np.log2(raw_score)
        targets.append((b["x_pct"], weight))

    # Slide the pattern across R2 centre, scoring against targets
    r2_center = (ctx.r3_left + ctx.r3_right) / 2
    total_span = sum(intervals)

    best_grid = None
    best_score = -1

    for step in range(100):
        start = r2_center - total_span / 2 - pitch / 2 + pitch * step / 100

        grid = [round(start, 2)]
        for iv in intervals:
            grid.append(round(grid[-1] + iv, 2))

        # Allow binding slack
        bind_slack = pitch * 0.5
        if ctx.binding_side == "left":
            left_limit = ctx.r3_left - bind_slack
            right_limit = ctx.r3_right
        else:
            left_limit = ctx.r3_left
            right_limit = ctx.r3_right + bind_slack
        grid = [g for g in grid if left_limit <= g <= right_limit]
        if len(grid) < 3:
            continue

        score = 0
        for g in grid:
            for t_pos, t_weight in targets:
                dist = abs(g - t_pos)
                if dist < 3.0:
                    score += t_weight * np.exp(-(dist * dist) / 2.0)

        if score > best_score:
            best_score = score
            best_grid = grid

    if best_grid:
        return _boundaries_from_positions(best_grid)
    else:
        return _boundaries_from_positions(ctx.expected_boundaries)


def place_standard(boundaries, ctx):
    """
    Place columns for a standard page.

    Strategy: build a grid at the established pitch, centred on
    R2. Slide it left/right to maximise alignment with:
      - Detected boundaries (strongest signal)
      - Ad region edges (supporting signal)
      - Text area edges (weak signal)
    Constrain to R3 bounds and expected column count.
    """
    num_cols = ctx.num_columns
    n_boundaries = num_cols + 1

    if not boundaries and not ctx.expected_boundaries:
        return []

    # ── Estimate pitch from this page's detected boundaries ───────
    # Use the issue pitch as a reference to distinguish single-pitch
    # gaps from doubled gaps (missed boundaries).
    ref_pitch = ctx.pitch
    page_gaps = []
    all_gaps = []
    if len(boundaries) >= 2:
        positions = sorted(b["x_pct"] for b in boundaries)
        for i in range(len(positions) - 1):
            g = positions[i + 1] - positions[i]
            all_gaps.append(g)
            if ref_pitch * 0.7 < g < ref_pitch * 1.4:
                page_gaps.append(g)
            elif ref_pitch * 1.4 <= g < ref_pitch * 2.5:
                page_gaps.append(g / 2)

    page_pitch_adopted = False
    if len(page_gaps) >= 3:
        # Remove outliers: reject gaps more than 1.5 IQR from median
        q1 = float(np.percentile(page_gaps, 25))
        q3 = float(np.percentile(page_gaps, 75))
        iqr = q3 - q1
        filtered = [g for g in page_gaps if q1 - 1.5 * iqr <= g <= q3 + 1.5 * iqr]
        pitch = round(float(np.median(filtered if filtered else page_gaps)), 2)
    elif page_gaps:
        pitch = round(float(np.median(page_gaps)), 2)
    else:
        # No gaps fit the issue-pitch acceptance window. Before falling
        # back to ref_pitch, check whether the page's detected gaps
        # form a coherent grid at a *different* pitch.
        #
        # This is the anomaly path: pages where the actual content has
        # been compressed into part of the page (e.g. a landscape scan
        # placed in a portrait PDF), so the per-page pitch in universal
        # %-of-page coords is significantly smaller (or larger) than the
        # issue's typical pitch. Pass 1's detected boundaries are
        # correct in absolute % terms; we should honour them rather
        # than stamp the issue grid over whitespace.
        #
        # Adoption rule: at least 4 gaps, tightly clustered (CV < 0.10),
        # and the cluster pitch is at least ~25% off the ref pitch (if
        # it's close, the existing window would have caught it). When
        # adopted, recompute num_cols from the page's R3 width.
        adopted = _maybe_adopt_page_pitch(all_gaps, ref_pitch)
        if adopted is not None:
            pitch = adopted
            page_pitch_adopted = True
            # On adoption, R3 is unreliable as a content extent (the
            # whole reason we're here is that the issue grid doesn't
            # fit, typically because R3 was inflated by an embedded
            # scan placed full-width on a portrait page). Trust the
            # detected boundaries' span as the content band, and
            # derive the column count from it.
            det_left = float(min(positions))
            det_right = float(max(positions))
            det_span = max(pitch, det_right - det_left)
            num_cols = max(2, int(round(det_span / pitch)))
            n_boundaries = num_cols + 1
            print(f"  P{ctx.gazette_page}: page-pitch adopted "
                  f"({pitch}% vs issue {ref_pitch}%), "
                  f"num_cols={num_cols} from detected span "
                  f"{det_left:.2f}-{det_right:.2f}")
        else:
            pitch = ref_pitch

    # ── Alignment targets ─────────────────────────────────────────
    # Detected boundaries are the primary signal for grid positioning.
    # Ad edges and text_area boost confidence of nearby boundaries
    # but don't influence position directly.
    targets = []
    for b in boundaries:
        # Use log of score to prevent any single boundary from
        # dominating. A boundary with score 35 vs 4 should be
        # stronger but not 9x stronger.
        raw_score = max(b.get("weighted_score", 1.0), 0.1)
        weight = 1.0 + np.log2(raw_score)
        pos = b["x_pct"]
        # Boost if an ad edge is within 2% — confirms this boundary
        for az in ctx.ad_zones:
            if abs(pos - az[0]) < 2.0 or abs(pos - az[1]) < 2.0:
                weight *= 1.3
                break
        # Mild boost if text_area edge is within 2%
        if abs(pos - ctx.text_area_left) < 2.0 or abs(pos - ctx.text_area_right) < 2.0:
            weight *= 1.1
        targets.append((pos, weight))

    # ── Build candidate grids ─────────────────────────────────────
    # Start from the centre of the content band, try offsets from
    # -pitch/2 to +pitch/2 in small steps. Score each grid against the
    # targets.
    #
    # Content band defaults to R3, but on the page-pitch-adopted path
    # R3 is the wrong extent (see adoption block above), so use the
    # detected boundary span as the band instead.
    if page_pitch_adopted:
        content_left = det_left
        content_right = det_right
    else:
        content_left = ctx.r3_left
        content_right = ctx.r3_right
    grid_center = (content_left + content_right) / 2
    half_span = (n_boundaries // 2) * pitch

    best_grid = None
    best_score = -1

    # Try 100 offsets across one pitch width
    for step in range(100):
        offset = -pitch / 2 + pitch * step / 100

        # Build grid centred on content centre + offset
        center = grid_center + offset
        grid = []
        for i in range(n_boundaries):
            pos = center - half_span + i * pitch
            grid.append(round(pos, 2))

        # Constrain to the content band, but allow the binding side
        # to extend slightly past it — the last column on the binding
        # side is often narrowed by page curvature into the spine.
        bind_slack = pitch * 0.5
        if ctx.binding_side == "left":
            left_limit = content_left - bind_slack
            right_limit = content_right
        else:
            left_limit = content_left
            right_limit = content_right + bind_slack
        grid = [g for g in grid if left_limit <= g <= right_limit]
        if len(grid) < 3:
            continue

        # Score: sum of weighted proximity to targets.
        # Gaussian-like falloff — rewards close matches strongly
        # but still gives partial credit up to 3% away.
        score = 0
        for g in grid:
            for t_pos, t_weight in targets:
                dist = abs(g - t_pos)
                if dist < 3.0:
                    score += t_weight * np.exp(-(dist * dist) / (2 * 1.0 * 1.0))

        if score > best_score:
            best_score = score
            best_grid = grid

    if best_grid:
        return _boundaries_from_positions(best_grid)
    else:
        return _boundaries_from_positions(ctx.expected_boundaries)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _maybe_adopt_page_pitch(all_gaps, ref_pitch,
                            min_gaps=4, max_cv=0.10, min_offset_frac=0.25):
    """
    Decide whether the page's detected gaps form a coherent grid at a
    pitch significantly different from the issue's reference pitch,
    and return that pitch if so.

    Used when the issue-pitch acceptance window in `place_standard`
    finds no usable gaps — typically pages with anomalous scan
    geometry where the universal-coordinate pitch is genuinely
    different from the issue norm.

    Args:
        all_gaps:         every gap between adjacent detected boundaries
                          (no window filter)
        ref_pitch:        the issue-wide reference pitch
        min_gaps:         require at least this many gaps in the cluster
                          (4 = enough for a 5-column grid, the smallest
                          case worth trusting)
        max_cv:           coefficient of variation threshold; tighter
                          clusters look more like a real grid than noise
        min_offset_frac:  cluster median must be at least this fraction
                          off ref_pitch (otherwise the original window
                          would have caught it)

    Returns:
        adopted pitch (rounded to 2 dp), or None if no coherent
        alternative cluster was found.
    """
    if len(all_gaps) < min_gaps:
        return None

    # Tightest cluster around the median gap. We don't try to find a
    # "best" cluster among multiple modes — anomaly pages observed so
    # far have a single dominant per-page pitch.
    arr = np.asarray(all_gaps, dtype=float)
    med = float(np.median(arr))
    if med <= 0:
        return None
    near = arr[np.abs(arr - med) < 0.20 * med]
    if len(near) < min_gaps:
        return None
    mean = float(near.mean())
    if mean <= 0:
        return None
    cv = float(near.std()) / mean
    if cv > max_cv:
        return None
    if abs(mean - ref_pitch) < min_offset_frac * ref_pitch:
        # Close enough to ref_pitch that the existing window would have
        # caught it; if it didn't, the gaps are likely too noisy to
        # trust as a grid.
        return None
    return round(mean, 2)


def _boundaries_from_positions(positions):
    """Convert a list of x_pct positions to boundary dicts."""
    return [{
        "x_pct": round(p, 2),
        "peak_darkness": 0, "row_std": 0, "valley_depth": 0,
        "confidence": "placed",
        "strips_hit": 0, "weighted_score": 0, "drift": 0,
    } for p in positions if 0 < p < 100]


def _merge_narrow(boundaries, min_width):
    """Merge boundaries closer than min_width, keeping the stronger one."""
    if len(boundaries) < 2:
        return boundaries
    result = [boundaries[0]]
    for b in boundaries[1:]:
        if b["x_pct"] - result[-1]["x_pct"] < min_width:
            if b.get("weighted_score", 0) > result[-1].get("weighted_score", 0):
                result[-1] = b
        else:
            result.append(b)
    return result
