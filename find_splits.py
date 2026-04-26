"""
Detect horizontal split points within a newspaper column image.

Uses per-column calibration so that all thresholds are relative to the
column's own statistics, handling variation in scan darkness, quality,
show-through, and localised damage.

Pipeline:

1. calibrate_column()   — establish reference points from the image
1. find_features()      — detect rules, gaps, and headlines
1. find_item_boundaries() — group features into article boundaries
1. split_column()       — extract item images

The calibration and feature data can be saved as a reusable profile
for subsequent pages in the same issue.

Usage:
    from find_splits import process_column

    results = process_column("col3.png")
    for item in results["items"]:
        print(f"{item['index']}: y={item['y_start']}-{item['y_end']}")

    # Save profile for reuse
    save_profile(results["profile"], "gazette_1920-01-02_profile.json")

"""

import json
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, gaussian_filter1d

from coordinates import px_to_pct

# —————————————————————————
# Calibration
# —————————————————————————

def calibrate_column(image_path, inner_margin=0.25, hblur_sigma=15, smooth_sigma=3):
    """
    Analyse a column image and establish calibration reference points.

    All subsequent detection uses these references rather than absolute
    thresholds, making the system robust to variation in scan quality.

    Args:
        image_path:    Path to column PNG.
        inner_margin:  Fraction of width to exclude from each edge (0.25 = centre 50%).
        hblur_sigma:   Horizontal Gaussian blur sigma (smears characters into bands).
        smooth_sigma:  Vertical smoothing sigma for row profiles.

    Returns:
        dict with calibration data, raw profiles, and quality flags.
    """
    img = Image.open(image_path).convert("L")
    arr = np.array(img)
    h, w = arr.shape

    col_lo = int(w * inner_margin)
    col_hi = int(w * (1 - inner_margin))
    mw = col_hi - col_lo
    strip = arr[:, col_lo:col_hi]
    inv = 255.0 - strip.astype(float)

    # Horizontal blur: smears characters into uniform bands,
    # preserves vertical structure (rules stay sharp, text becomes flat)
    blurred = gaussian_filter(inv, sigma=[0, hblur_sigma])

    # Row-level statistics from the blurred strip
    blur_means = blurred.mean(axis=1)
    smooth = gaussian_filter1d(blur_means, sigma=smooth_sigma)

    # Raw (unblurred) statistics for complexity measurements
    raw_means = inv.mean(axis=1)
    raw_stds = inv.std(axis=1)

    # --- Three-probe vertical analysis ---
    # Sample at left edge, centre, and right edge of the text block
    # (within the inner strip, not into the gutter)
    probe_w = max(1, int(mw * 0.10))  # 10% wide sampling strips
    left_lo = int(mw * 0.05)
    left_hi = left_lo + probe_w
    centre_lo = int(mw * 0.45)
    centre_hi = int(mw * 0.55)
    right_hi = int(mw * 0.95)
    right_lo = right_hi - probe_w

    # H-blur each probe strip independently
    left_blur = gaussian_filter(inv[:, left_lo:left_hi], sigma=[0, hblur_sigma])
    centre_blur = gaussian_filter(inv[:, centre_lo:centre_hi], sigma=[0, hblur_sigma])
    right_blur = gaussian_filter(inv[:, right_lo:right_hi], sigma=[0, hblur_sigma])

    left_smooth = gaussian_filter1d(left_blur.mean(axis=1), sigma=smooth_sigma)
    centre_smooth = gaussian_filter1d(centre_blur.mean(axis=1), sigma=smooth_sigma)
    right_smooth = gaussian_filter1d(right_blur.mean(axis=1), sigma=smooth_sigma)

    # Derived spatial signals
    edge_mean = (left_smooth + right_smooth) / 2.0

    # Edge balance: positive = edges darker than centre (box rails)
    #               negative = centre darker than edges (centred headline)
    edge_balance = edge_mean - centre_smooth

    # Fill symmetry: low = symmetric, high = asymmetric (para ends, shadow)
    fill_symmetry = np.abs(left_smooth - right_smooth)

    # Edge presence: minimum of both edges — high = both edges dark (box rails)
    edge_presence = np.minimum(left_smooth, right_smooth)

    # Text rhythm: autocorrelation of raw darkness for line height detection
    # Computed per-region later, but store the raw unblurred means
    raw_left = gaussian_filter1d(inv[:, left_lo:left_hi].mean(axis=1), sigma=1)
    raw_centre = gaussian_filter1d(inv[:, centre_lo:centre_hi].mean(axis=1), sigma=1)
    raw_right = gaussian_filter1d(inv[:, right_lo:right_hi].mean(axis=1), sigma=1)

    # Narrow-edge max: detect thin vertical lines (box borders)
    # at the very edge of the strip (first/last ~5%)
    edge_band = max(3, int(mw * 0.05))
    left_edge_max = np.array([inv[y, :edge_band].max() for y in range(h)])
    right_edge_max = np.array([inv[y, mw - edge_band:mw].max() for y in range(h)])
    left_edge_max_smooth = gaussian_filter1d(left_edge_max.astype(float), sigma=5)
    right_edge_max_smooth = gaussian_filter1d(right_edge_max.astype(float), sigma=5)

    # --- Per-row coverage, span, offset, complexity ---
    coverages = np.zeros(h)
    spans = np.zeros(h)
    offsets = np.zeros(h)
    complexities = np.zeros(h)

    dark_threshold = 80  # pixel-level threshold for "dark"
    for y in range(h):
        row = inv[y, :]
        dark = row > dark_threshold
        if dark.any():
            indices = np.where(dark)[0]
            left, right = indices[0], indices[-1]
            coverages[y] = dark.sum() / mw
            spans[y] = (right - left + 1) / mw
            centre = (left + right) / 2.0
            offsets[y] = abs(centre - mw / 2.0) / (mw / 2.0)
            dark_pixels = row[dark]
            complexities[y] = dark_pixels.std() if len(dark_pixels) > 5 else 0

    # --- Calibration reference points ---

    # Margin characterisation: top and bottom 5% should be non-content
    margin_slice = max(1, h // 20)
    margin_rows = np.concatenate([smooth[:margin_slice], smooth[-margin_slice:]])
    paper_baseline = float(np.median(margin_rows))
    paper_noise = float(np.std(margin_rows))

    # White level: 10th percentile of smoothed darkness
    white_level = float(np.percentile(smooth, 10))

    # Content rows: everything substantially above white
    content_mask = smooth > white_level * 2.5
    if content_mask.sum() > 10:
        content_values = smooth[content_mask]
        text_level = float(np.median(content_values))
        text_q25 = float(np.percentile(content_values, 25))
        text_q75 = float(np.percentile(content_values, 75))
    else:
        text_level = float(np.median(smooth))
        text_q25 = text_level * 0.7
        text_q75 = text_level * 1.3

    # Peak level: 98th percentile
    peak_level = float(np.percentile(smooth, 98))

    # Dynamic range
    dynamic_range = peak_level - white_level

    # --- Derived thresholds (all relative) ---
    ws_threshold = white_level + dynamic_range * 0.04
    rule_threshold = white_level + dynamic_range * 0.70
    headline_threshold = white_level + dynamic_range * 0.40

    # --- Body text line height estimation ---
    # Find regular peak spacing in a known body-text region (middle 40% of column)
    mid_start = int(h * 0.3)
    mid_end = int(h * 0.7)
    mid_smooth = smooth[mid_start:mid_end]

    # Find peaks in the middle region
    peaks = []
    for i in range(1, len(mid_smooth) - 1):
        if mid_smooth[i] > text_q25 and mid_smooth[i] > mid_smooth[i-1] and mid_smooth[i] > mid_smooth[i+1]:
            peaks.append(i)

    if len(peaks) > 5:
        spacings = np.diff(peaks)
        # Filter to plausible line heights (10-60px at 450dpi)
        plausible = spacings[(spacings > 10) & (spacings < 60)]
        if len(plausible) > 3:
            line_height = float(np.median(plausible))
        else:
            line_height = None
    else:
        line_height = None

    # --- Quality flags ---
    low_contrast = dynamic_range < 80
    show_through = paper_baseline > 20
    noisy = paper_noise > 10
    high_quality = not low_contrast and not show_through and not noisy

    # --- Content bounds ---
    content_threshold = white_level + dynamic_range * 0.10
    content_rows = np.where(smooth > content_threshold)[0]
    if len(content_rows) > 0:
        content_top = int(content_rows[0])
        content_bottom = int(content_rows[-1])
    else:
        content_top = 0
        content_bottom = h

    return {
        # Image dimensions
        "image_path": image_path,
        "height_px": h,
        "width_px": w,
        "strip_width_px": mw,
        "inner_margin": inner_margin,

        # Reference points
        "white_level": white_level,
        "text_level": text_level,
        "text_q25": text_q25,
        "text_q75": text_q75,
        "peak_level": peak_level,
        "dynamic_range": dynamic_range,
        "paper_baseline": paper_baseline,
        "paper_noise": paper_noise,

        # Derived thresholds
        "ws_threshold": ws_threshold,
        "rule_threshold": rule_threshold,
        "headline_threshold": headline_threshold,

        # Typography
        "line_height_px": line_height,

        # Content bounds
        "content_top": content_top,
        "content_bottom": content_bottom,

        # Quality
        "low_contrast": low_contrast,
        "show_through": show_through,
        "noisy": noisy,
        "high_quality": high_quality,

        # Full profiles (for charts and further analysis)
        "profiles": {
            "smooth": smooth,
            "blur_means": blur_means,
            "raw_means": raw_means,
            "raw_stds": raw_stds,
            "coverages": coverages,
            "spans": spans,
            "offsets": offsets,
            "complexities": complexities,
            # Three-probe spatial analysis
            "left_smooth": left_smooth,
            "centre_smooth": centre_smooth,
            "right_smooth": right_smooth,
            "edge_balance": edge_balance,
            "fill_symmetry": fill_symmetry,
            "edge_presence": edge_presence,
            # Raw unblurred probes (for rhythm analysis)
            "raw_left": raw_left,
            "raw_centre": raw_centre,
            "raw_right": raw_right,
            # Narrow-edge max (for box border detection)
            "left_edge_max_smooth": left_edge_max_smooth,
            "right_edge_max_smooth": right_edge_max_smooth,
        },
    }


# —————————————————————————
# Feature detection
# —————————————————————————

def find_features(cal):
    """
    Detect horizontal features (rules, whitespace gaps, headlines) using
    calibrated thresholds.

    Args:
        cal: dict from calibrate_column().

    Returns:
        dict with lists of detected rules, gaps, and headline bands.
    """
    h = cal["height_px"]
    smooth = cal["profiles"]["smooth"]
    coverages = cal["profiles"]["coverages"]
    spans = cal["profiles"]["spans"]
    offsets = cal["profiles"]["offsets"]
    complexities = cal["profiles"]["complexities"]

    ws_thresh = cal["ws_threshold"]
    rule_thresh = cal["rule_threshold"]
    headline_thresh = cal["headline_threshold"]
    content_top = cal["content_top"]
    content_bottom = cal["content_bottom"]

    # Minimum gap height: if we know line height, require at least 40% of it
    min_gap = 4
    if cal.get("line_height_px"):
        min_gap = max(4, int(cal["line_height_px"] * 0.4))

    # --- Whitespace gaps ---
    in_gap = False
    gaps = []
    for y in range(content_top, content_bottom):
        if smooth[y] < ws_thresh and not in_gap:
            gap_start = y
            in_gap = True
        elif smooth[y] >= ws_thresh and in_gap:
            gap_height = y - gap_start
            if gap_height >= min_gap:
                gaps.append({
                    "y_start": gap_start,
                    "y_end": y,
                    "y_mid": (gap_start + y) // 2,
                    "height": gap_height,
                    "min_darkness": float(smooth[gap_start:y].min()),
                })
            in_gap = False

    # --- Horizontal rules ---
    complexity_smooth = gaussian_filter1d(complexities, sigma=3)

    rules = []
    line_height = cal["line_height_px"] or 25

    # Full-width rules: first find BANDS of darkness above the rule
    # threshold, then classify each band as a rule or shadow.
    # A real printed rule is thin (1-5 rows). Leaf shadow or binding
    # shadow is a broad band (20+ rows) of uniform high darkness.
    in_band = False
    band_start = 0
    full_rule_bands = []

    for y in range(content_top, content_bottom):
        d = smooth[y]
        cov = coverages[y]
        comp = complexity_smooth[y]
        is_dark_full = d > rule_thresh and cov > 0.75 and comp < 40

        if is_dark_full and not in_band:
            band_start = y
            in_band = True
        elif not is_dark_full and in_band:
            band_height = y - band_start
            full_rule_bands.append({
                "y_start": band_start,
                "y_end": y,
                "height": band_height,
                "peak_y": band_start + int(smooth[band_start:y].argmax()),
                "peak_darkness": float(smooth[band_start:y].max()),
                "mean_complexity": float(complexity_smooth[band_start:y].mean()),
            })
            in_band = False

    for band in full_rule_bands:
        # Shadow filter: broad bands (> half a text line) that sit near
        # the top or bottom margins are leaf/binding shadow, not rules.
        # Also reject bands with very low complexity (uniform darkness).
        is_shadow = False

        if band["height"] > line_height * 0.5:
            # Broad dark band — likely shadow
            is_shadow = True

        if band["mean_complexity"] < 5 and band["height"] > 10:
            # Near-zero complexity over many rows = solid dark region
            is_shadow = True

        # Check with three-probe data: shadow is uniformly dark across
        # all probes. Real content varies across the width.
        zone_left = cal["profiles"]["left_smooth"][band["y_start"]:band["y_end"]]
        zone_centre = cal["profiles"]["centre_smooth"][band["y_start"]:band["y_end"]]
        zone_right = cal["profiles"]["right_smooth"][band["y_start"]:band["y_end"]]
        if len(zone_left) > 5:
            # All three probes uniformly maxed out = shadow
            all_maxed = (zone_left.mean() > 200 and
                         zone_centre.mean() > 200 and
                         zone_right.mean() > 200)
            if all_maxed and band["height"] > 10:
                is_shadow = True

        if is_shadow:
            continue

        # Thin band: this is a real rule. Take the peak row.
        rules.append({
            "y": band["peak_y"], "type": "full",
            "darkness": band["peak_darkness"],
            "coverage": float(coverages[band["peak_y"]]),
            "span": float(spans[band["peak_y"]]),
            "offset": float(offsets[band["peak_y"]]),
            "complexity": float(complexity_smooth[band["peak_y"]]),
            "band_height": band["height"],
        })

    # Centred rules: only search within ±30px of whitespace gaps
    # This prevents body text fragments from being detected as rules
    gap_zones = set()
    search_margin = 30
    for gap in gaps:
        for y in range(max(content_top, gap["y_start"] - search_margin),
                       min(content_bottom, gap["y_end"] + search_margin)):
            gap_zones.add(y)

    for y in sorted(gap_zones):
        d = smooth[y]
        span = spans[y]
        off = offsets[y]
        comp = complexity_smooth[y]

        if (d > 20
                and 0.10 < span < 0.65
                and off < 0.15
                and comp < 30):
            # Check it's not already captured as a full rule
            if any(r["y"] == y for r in rules):
                continue
            if rules and abs(y - rules[-1]["y"]) < 5:
                if d > smooth[rules[-1]["y"]]:
                    rules[-1] = {
                        "y": y, "type": "centred", "darkness": float(d),
                        "coverage": float(coverages[y]), "span": float(span),
                        "offset": float(off), "complexity": float(comp),
                    }
                continue
            rules.append({
                "y": y, "type": "centred", "darkness": float(d),
                "coverage": float(coverages[y]), "span": float(span),
                "offset": float(off), "complexity": float(comp),
            })

    rules.sort(key=lambda r: r["y"])

    # --- Headline bands (enhanced with probe data) ---
    # Use centre probe vs edge probes: headlines have dark centre with
    # lighter edges (centred text) or are simply much darker than body
    left_smooth = cal["profiles"]["left_smooth"]
    centre_smooth = cal["profiles"]["centre_smooth"]
    right_smooth = cal["profiles"]["right_smooth"]
    edge_balance = cal["profiles"]["edge_balance"]
    fill_symmetry = cal["profiles"]["fill_symmetry"]
    edge_presence = cal["profiles"]["edge_presence"]

    edge_balance_smooth = gaussian_filter1d(edge_balance, sigma=5)
    fill_sym_smooth = gaussian_filter1d(fill_symmetry, sigma=5)
    edge_pres_smooth = gaussian_filter1d(edge_presence, sigma=5)

    # Headlines: dark overall AND (centred = negative edge_balance, OR
    # simply much darker than body text level)
    headlines = []
    in_headline = False
    for y in range(content_top, content_bottom):
        d = smooth[y]
        comp = complexity_smooth[y]
        eb = edge_balance_smooth[y]

        # Primary: dark with letterform complexity
        is_dark_text = d > headline_thresh and comp > 35
        # Secondary: centred text = centre much darker than edges
        is_centred_text = (centre_smooth[y] > cal["text_level"] * 1.5
                           and eb < -10)

        is_hl = is_dark_text or (is_centred_text and d > cal["text_level"])

        if is_hl and not in_headline:
            hl_start = y
            in_headline = True
        elif not is_hl and in_headline:
            hl_height = y - hl_start
            if hl_height >= 12:
                # Classify: centred vs full-width headline
                zone_eb = edge_balance_smooth[hl_start:y].mean()
                zone_centre = centre_smooth[hl_start:y].mean()
                zone_edges = edge_pres_smooth[hl_start:y].mean()
                hl_type = "centred" if zone_eb < -8 else "full_width"

                headlines.append({
                    "y_start": hl_start,
                    "y_end": y,
                    "y_mid": (hl_start + y) // 2,
                    "height": hl_height,
                    "peak_darkness": float(smooth[hl_start:y].max()),
                    "type": hl_type,
                    "edge_balance": float(zone_eb),
                })
            in_headline = False

    # --- Boxed features detection ---
    # Two complementary strategies:
    # 1. Rule clusters: dense groups of full-width horizontal rules indicate
    #    box borders. Normal content has 1-2 full rules; boxes have many.
    # 2. Vertical line fragments: short runs of edge darkness that
    #    individually are too short but collectively indicate box sides.
    #
    # Strategy 1: find zones with dense full-width rules
    full_rules = [r for r in rules if r["type"] == "full"]
    rule_clusters = []

    if full_rules:
        cluster_start = full_rules[0]["y"]
        cluster_end = full_rules[0]["y"]
        cluster_count = 1

        for r in full_rules[1:]:
            if r["y"] - cluster_end < 80:  # rules within 80px = same cluster
                cluster_end = r["y"]
                cluster_count += 1
            else:
                if cluster_count >= 3:  # 3+ full rules = significant cluster
                    rule_clusters.append({
                        "y_start": cluster_start,
                        "y_end": cluster_end,
                        "count": cluster_count,
                    })
                cluster_start = r["y"]
                cluster_end = r["y"]
                cluster_count = 1
        if cluster_count >= 3:
            rule_clusters.append({
                "y_start": cluster_start,
                "y_end": cluster_end,
                "count": cluster_count,
            })

    # Strategy 2: vertical line presence from narrow-edge max
    left_edge_smooth = cal["profiles"]["left_edge_max_smooth"]
    right_edge_smooth = cal["profiles"]["right_edge_max_smooth"]
    edge_min_smooth = np.minimum(left_edge_smooth, right_edge_smooth)
    box_line_thresh = cal["peak_level"] * 0.30  # slightly relaxed

    # Build box candidates from adjacent rule clusters.
    # Dense clusters of full-width rules (3+) are almost exclusively
    # found at box borders. Body text rarely has more than 1 full rule.
    # Simply: the zone between two adjacent clusters = inside a box.
    boxes = []

    for i in range(len(rule_clusters) - 1):
        top_c = rule_clusters[i]
        bot_c = rule_clusters[i + 1]
        gap = bot_c["y_start"] - top_c["y_end"]

        # Reasonable box: gap between 50px and 1500px
        if gap < 50 or gap > 1500:
            continue

        boxes.append({
            "y_start": top_c["y_start"],
            "y_end": bot_c["y_end"],
            "height": bot_c["y_end"] - top_c["y_start"],
            "has_top_rule": True,
            "has_bottom_rule": True,
            "top_rules": top_c["count"],
            "bottom_rules": bot_c["count"],
        })

    # Merge overlapping boxes into larger units
    if len(boxes) > 1:
        merged = [boxes[0]]
        for box in boxes[1:]:
            prev = merged[-1]
            if box["y_start"] <= prev["y_end"]:
                merged[-1] = {
                    "y_start": prev["y_start"],
                    "y_end": max(prev["y_end"], box["y_end"]),
                    "height": max(prev["y_end"], box["y_end"]) - prev["y_start"],
                    "has_top_rule": prev["has_top_rule"],
                    "has_bottom_rule": box["has_bottom_rule"],
                    "top_rules": prev.get("top_rules", 0),
                    "bottom_rules": box.get("bottom_rules", 0),
                }
            else:
                merged.append(box)
        boxes = merged

    # --- Illustration detection ---
    # Photographs/halftones: sustained moderate-to-high darkness over
    # many rows with LOW row-to-row variance (no text sawtooth rhythm).
    # Use the raw (unblurred) centre probe to check for rhythm.
    raw_centre = cal["profiles"]["raw_centre"]
    illustrations = []
    window = 30  # check rhythm in 30-row windows

    y = content_top
    while y < content_bottom - window:
        zone = raw_centre[y:y + window]
        zone_mean = zone.mean()
        zone_std = zone.std()

        # Illustration: moderate darkness, low row-to-row variance
        # (text has high variance due to sawtooth, illustrations don't)
        if zone_mean > cal["text_q25"] * 0.8 and zone_std < cal["text_level"] * 0.25:
            ill_start = y
            # Extend while the pattern continues
            while y < content_bottom - window:
                zone = raw_centre[y:y + window]
                if zone.mean() < cal["text_q25"] * 0.5 or zone.std() > cal["text_level"] * 0.35:
                    break
                y += window // 2
            if y - ill_start > 60:  # at least 60px tall
                illustrations.append({
                    "y_start": ill_start,
                    "y_end": y,
                    "height": y - ill_start,
                    "mean_darkness": float(raw_centre[ill_start:y].mean()),
                })
        y += window // 2

    # --- Paragraph end detection ---
    # Last line of a paragraph: left probe has content, right probe
    # is light. The fill_symmetry spikes and the right probe drops.
    para_ends = []
    line_h = cal["line_height_px"] or 25

    for y in range(content_top + int(line_h), content_bottom - int(line_h)):
        # Is this row part of text? (overall darkness above body threshold)
        if smooth[y] < cal["text_q25"] * 0.5:
            continue

        # Left has content, right is empty
        if (left_smooth[y] > cal["text_q25"] * 0.5
                and right_smooth[y] < cal["ws_threshold"] * 3):
            # Verify: the next few rows should be whitespace or a new
            # paragraph starting at the left margin
            below = smooth[y + 1:y + int(line_h)]
            if len(below) > 0 and below.min() < cal["ws_threshold"] * 2:
                para_ends.append({
                    "y": y,
                    "left_darkness": float(left_smooth[y]),
                    "right_darkness": float(right_smooth[y]),
                })

    # Deduplicate para_ends (take one per cluster)
    if para_ends:
        deduped_pe = [para_ends[0]]
        for pe in para_ends[1:]:
            if pe["y"] - deduped_pe[-1]["y"] > line_h * 0.5:
                deduped_pe.append(pe)
        para_ends = deduped_pe

    return {
        "gaps": gaps,
        "rules": rules,
        "headlines": headlines,
        "boxes": boxes,
        "illustrations": illustrations,
        "para_ends": para_ends,
    }


# —————————————————————————
# Item boundary detection
# —————————————————————————

def find_item_boundaries(cal, features):
    """
    Determine article boundaries from detected features.

    Strategy:
    1. Box edges are strong boundaries (score high)
    2. Gaps/rules INSIDE boxes are suppressed (internal structure)
    3. Gaps/rules OUTSIDE boxes scored by headline proximity
    4. Illustration edges are suppressed (not item boundaries)

    Args:
        cal:      dict from calibrate_column().
        features: dict from find_features().

    Returns:
        List of boundary y-positions (pixels) between items.
    """
    h = cal["height_px"]
    smooth = cal["profiles"]["smooth"]
    ws_thresh = cal["ws_threshold"]
    line_height = cal["line_height_px"] or 30
    content_top = cal["content_top"]
    content_bottom = cal["content_bottom"]

    gaps = features["gaps"]
    rules = features["rules"]
    headlines = features["headlines"]
    boxes = features.get("boxes", [])
    illustrations = features.get("illustrations", [])

    def inside_box(y):
        for box in boxes:
            if box["y_start"] < y < box["y_end"]:
                return True
        return False

    def inside_illustration(y):
        for ill in illustrations:
            if ill["y_start"] < y < ill["y_end"]:
                return True
        return False

    candidates = []

    # --- Box edges as boundaries ---
    for box in boxes:
        # Top of box
        top_y = box["y_start"]
        top_gaps = [g for g in gaps if abs(g["y_mid"] - top_y) < 40
                    and g["y_mid"] <= top_y]
        if top_gaps:
            best_gap = min(top_gaps, key=lambda g: abs(g["y_mid"] - top_y))
            candidates.append({
                "y": best_gap["y_mid"], "y_start": best_gap["y_start"],
                "y_end": best_gap["y_end"], "score": 50,
                "source": "box_top", "gap_height": best_gap["height"],
            })
        else:
            candidates.append({
                "y": top_y, "y_start": top_y, "y_end": top_y,
                "score": 45, "source": "box_top", "gap_height": 0,
            })

        # Bottom of box
        bot_y = box["y_end"]
        bot_gaps = [g for g in gaps if abs(g["y_mid"] - bot_y) < 40
                    and g["y_mid"] >= bot_y]
        if bot_gaps:
            best_gap = min(bot_gaps, key=lambda g: abs(g["y_mid"] - bot_y))
            candidates.append({
                "y": best_gap["y_mid"], "y_start": best_gap["y_start"],
                "y_end": best_gap["y_end"], "score": 50,
                "source": "box_bottom", "gap_height": best_gap["height"],
            })
        else:
            candidates.append({
                "y": bot_y, "y_start": bot_y, "y_end": bot_y,
                "score": 45, "source": "box_bottom", "gap_height": 0,
            })

    # --- Score gaps outside boxes ---
    for gap in gaps:
        if inside_box(gap["y_mid"]):
            continue
        if inside_illustration(gap["y_mid"]):
            continue

        height_ratio = gap["height"] / line_height
        score = height_ratio * 10

        nearby_rules = [
            r for r in rules
            if abs(r["y"] - gap["y_mid"]) < 50 and not inside_box(r["y"])
        ]
        if nearby_rules:
            score += 10
            for r in nearby_rules:
                if r["type"] == "full":
                    score += 10
                elif r["type"] == "centred":
                    score += 5

        # Headline following the gap
        nearby_headlines = [
            hl for hl in headlines
            if 0 < hl["y_start"] - gap["y_mid"] < 80
        ]
        if nearby_headlines:
            score += 30

        candidates.append({
            "y": gap["y_mid"], "y_start": gap["y_start"],
            "y_end": gap["y_end"], "score": score,
            "source": "gap", "gap_height": gap["height"],
        })

    # --- Rules outside boxes as fallback ---
    for rule in rules:
        if inside_box(rule["y"]):
            continue
        already_near = any(abs(c["y"] - rule["y"]) < 30 for c in candidates)
        if not already_near:
            score = 5
            if rule["type"] == "full":
                score += 15
            elif rule["type"] == "centred":
                score += 10
            candidates.append({
                "y": rule["y"], "y_start": rule["y"],
                "y_end": rule["y"], "score": score,
                "source": "rule_only", "gap_height": 0,
            })

    if not candidates:
        return []

    candidates.sort(key=lambda c: c["y"])

    merge_dist = int(line_height * 1.5)
    merged = []
    cluster = [candidates[0]]
    for c in candidates[1:]:
        if c["y"] - cluster[-1]["y"] < merge_dist:
            cluster.append(c)
        else:
            best = max(cluster, key=lambda x: x["score"])
            merged.append(best)
            cluster = [c]
    best = max(cluster, key=lambda x: x["score"])
    merged.append(best)

    min_score = 20
    boundaries = [c["y"] for c in merged if c["score"] >= min_score]

    return boundaries


# —————————————————————————
# Column splitting
# —————————————————————————

def split_column(image_path, boundaries, content_top, content_bottom, padding=0):
    """
    Split a column image into items at the given boundaries.

    Args:
        image_path:     Path to the column PNG.
        boundaries:     List of y-positions (pixels) from find_item_boundaries().
        content_top:    Top of content area (pixels).
        content_bottom: Bottom of content area (pixels).
        padding:        Extra pixels above/below each item.

    Returns:
        List of item dicts with y_start, y_end, index.
    """
    edges = [content_top] + list(boundaries) + [content_bottom]
    items = []
    for i in range(len(edges) - 1):
        items.append({
            "index": i,
            "y_start": edges[i],
            "y_end": edges[i + 1],
            "y_start_padded": max(0, edges[i] - padding),
            "y_end_padded": edges[i + 1] + padding,
        })
    return items


# —————————————————————————
# Profile capture
# —————————————————————————

def build_profile(cal, features, boundaries):
    """
    Build a reusable profile from the calibration and detection results.

    The profile captures the typographic and structural measurements
    that can be reused for subsequent pages in the same issue, and
    compared across issues for historical analysis.

    Args:
        cal:        dict from calibrate_column().
        features:   dict from find_features().
        boundaries: list from find_item_boundaries().

    Returns:
        dict suitable for JSON serialisation (no numpy arrays).
    """
    rule_spans = sorted(set(
        round(r["span"] * 100) for r in features["rules"]
    ))

    hl_heights = [hl["height"] for hl in features["headlines"]]
    hl_darknesses = [hl["peak_darkness"] for hl in features["headlines"]]

    return {
        "calibration": {
            "white_level": round(cal["white_level"], 1),
            "text_level": round(cal["text_level"], 1),
            "peak_level": round(cal["peak_level"], 1),
            "dynamic_range": round(cal["dynamic_range"], 1),
            "paper_baseline": round(cal["paper_baseline"], 1),
            "paper_noise": round(cal["paper_noise"], 1),
        },
        "quality": {
            "high_quality": cal["high_quality"],
            "low_contrast": cal["low_contrast"],
            "show_through": cal["show_through"],
            "noisy": cal["noisy"],
        },
        "typography": {
            "body_line_height_px": round(cal["line_height_px"], 1) if cal["line_height_px"] else None,
            "rule_spans_pct": rule_spans,
            "headline_count": len(features["headlines"]),
            "headline_heights_px": hl_heights,
            "headline_peak_darknesses": [round(d, 1) for d in hl_darknesses],
        },
        "structure": {
            "content_top_pct": round(cal["content_top"] / cal["height_px"] * 100, 2),
            "content_bottom_pct": round(cal["content_bottom"] / cal["height_px"] * 100, 2),
            "item_count": len(boundaries) + 1,
            "boundary_positions_pct": [
                round(b / cal["height_px"] * 100, 2) for b in boundaries
            ],
            "gap_count": len(features["gaps"]),
            "rule_count": len(features["rules"]),
        },
    }


def save_profile(profile, path):
    """Save a profile to JSON."""
    with open(path, "w") as f:
        json.dump(profile, f, indent=2)

def load_profile(path):
    """Load a profile from JSON."""
    with open(path) as f:
        return json.load(f)


# —————————————————————————
# Chart generation
# —————————————————————————

def generate_charts(cal, features, boundaries, output_path):
    """
    Generate the standardised seven-panel analysis chart.

    Args:
        cal:         dict from calibrate_column().
        features:    dict from find_features().
        boundaries:  list from find_item_boundaries().
        output_path: where to save the chart PNG.
    """
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    import matplotlib.gridspec as gridspec

    h = cal["height_px"]
    mw = cal["strip_width_px"]
    smooth = cal["profiles"]["smooth"]
    coverages = cal["profiles"]["coverages"]
    spans = cal["profiles"]["spans"]
    offsets = cal["profiles"]["offsets"]
    complexities = cal["profiles"]["complexities"]
    complexity_smooth = gaussian_filter1d(complexities, sigma=3)

    img = Image.open(cal["image_path"]).convert("L")
    arr = np.array(img)
    col_lo = int(arr.shape[1] * cal["inner_margin"])
    col_hi = int(arr.shape[1] * (1 - cal["inner_margin"]))
    strip = arr[:, col_lo:col_hi]

    yy = np.arange(h)

    fig = plt.figure(figsize=(18, 36))
    gs = gridspec.GridSpec(1, 7, width_ratios=[1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.8])

    ws_t = cal["ws_threshold"]
    rule_t = cal["rule_threshold"]
    hl_t = cal["headline_threshold"]

    # P1: Smoothed darkness with thresholds
    ax1 = fig.add_subplot(gs[0])
    ax1.plot(smooth, yy, color="blue", linewidth=0.8)
    ax1.axvline(ws_t, color="green", linewidth=0.5, alpha=0.5, linestyle="--")
    ax1.axvline(hl_t, color="orange", linewidth=0.5, alpha=0.5, linestyle="--")
    ax1.axvline(rule_t, color="red", linewidth=0.5, alpha=0.5, linestyle="--")
    ax1.set_title("Darkness\n(h-blurred)", fontsize=8)
    ax1.set_xlim(0, max(260, cal["peak_level"] * 1.1))
    ax1.invert_yaxis()
    ax1.set_ylabel("Pixel row (y)")
    ax1.tick_params(labelsize=6)

    # P2: Coverage
    ax2 = fig.add_subplot(gs[1], sharey=ax1)
    cov_smooth = gaussian_filter1d(coverages, sigma=5)
    ax2.plot(cov_smooth, yy, color="purple", linewidth=0.5)
    ax2.set_title("Coverage\n(frac dark)", fontsize=8)
    ax2.set_xlim(0, 1.05)
    ax2.tick_params(labelsize=6)
    plt.setp(ax2.get_yticklabels(), visible=False)

    # P3: Span
    ax3 = fig.add_subplot(gs[2], sharey=ax1)
    span_smooth = gaussian_filter1d(spans, sigma=5)
    ax3.plot(span_smooth, yy, color="teal", linewidth=0.5)
    ax3.set_title("Span\n(dark extent)", fontsize=8)
    ax3.set_xlim(0, 1.05)
    ax3.tick_params(labelsize=6)
    plt.setp(ax3.get_yticklabels(), visible=False)

    # P4: Complexity
    ax4 = fig.add_subplot(gs[3], sharey=ax1)
    ax4.plot(complexity_smooth, yy, color="red", linewidth=0.5)
    ax4.set_title("Complexity\n(within-row std)", fontsize=8)
    ax4.set_xlim(0, 100)
    ax4.tick_params(labelsize=6)
    plt.setp(ax4.get_yticklabels(), visible=False)

    # P5: Centre offset
    ax5 = fig.add_subplot(gs[4], sharey=ax1)
    off_smooth = gaussian_filter1d(offsets, sigma=5)
    ax5.plot(off_smooth, yy, color="brown", linewidth=0.5)
    ax5.set_title("Centre offset\n(0=centred)", fontsize=8)
    ax5.set_xlim(0, 1)
    ax5.tick_params(labelsize=6)
    plt.setp(ax5.get_yticklabels(), visible=False)

    # P6: Classification
    ax6 = fig.add_subplot(gs[5], sharey=ax1)
    for r in features["rules"]:
        colour = "red" if r["type"] == "full" else "green"
        ax6.axhline(r["y"], color=colour, linewidth=1, alpha=0.8)
    for hl in features["headlines"]:
        ax6.axhspan(hl["y_start"], hl["y_end"], color="blue", alpha=0.15)
    for b in boundaries:
        ax6.axhline(b, color="black", linewidth=1.5, linestyle="-")
    ax6.set_title("Classification", fontsize=8)
    ax6.set_xlim(0, 1)
    ax6.tick_params(labelsize=6)
    plt.setp(ax6.get_yticklabels(), visible=False)
    p1 = mpatches.Patch(color="red", label="Full rule")
    p2 = mpatches.Patch(color="green", label="Centred rule")
    p3 = mpatches.Patch(color="blue", alpha=0.3, label="Headline")
    p4 = mpatches.Patch(color="black", label="Item boundary")
    ax6.legend(handles=[p1, p2, p3, p4], fontsize=5, loc="lower left")

    # P7: Strip image
    ax7 = fig.add_subplot(gs[6], sharey=ax1)
    ax7.imshow(strip, cmap="gray", aspect="equal")
    ax7.set_title("Centre 50% strip", fontsize=8)
    ax7.tick_params(labelsize=6)
    plt.setp(ax7.get_yticklabels(), visible=False)

    # Draw boundaries on all panels
    for b in boundaries:
        for ax in [ax1, ax2, ax3, ax4, ax5]:
            ax.axhline(b, color="black", linewidth=0.6, alpha=0.5, linestyle="-")

    # Mark rules on image panel
    for r in features["rules"]:
        colour = "red" if r["type"] == "full" else "green"
        label = f"{r['type']} rule ({r['span']:.0%})"
        ax7.annotate(
            label, xy=(mw, r["y"]), xytext=(mw + 8, r["y"]),
            fontsize=5, color=colour, va="center", annotation_clip=False,
        )

    ax7.set_xlim(-5, mw + 120)

    # Calibration annotation
    cal_text = (
        f"white={cal['white_level']:.0f}  text={cal['text_level']:.0f}  "
        f"peak={cal['peak_level']:.0f}  DR={cal['dynamic_range']:.0f}\n"
        f"line_h={cal['line_height_px']:.0f}px  "
        f"paper={cal['paper_baseline']:.0f}\u00b1{cal['paper_noise']:.1f}"
    ) if cal["line_height_px"] else (
        f"white={cal['white_level']:.0f}  text={cal['text_level']:.0f}  "
        f"peak={cal['peak_level']:.0f}  DR={cal['dynamic_range']:.0f}\n"
        f"paper={cal['paper_baseline']:.0f}\u00b1{cal['paper_noise']:.1f}"
    )
    fig.text(0.02, 0.005, cal_text, fontsize=6, family="monospace",
             va="bottom", color="grey")

    plt.tight_layout()
    plt.savefig(output_path, dpi=200)
    plt.close(fig)


# —————————————————————————
# Convenience: full pipeline
# —————————————————————————

def process_column(image_path, inner_margin=0.25, chart_path=None, profile_path=None):
    """
    Run the full analysis pipeline on a column image.

    Args:
        image_path:   Path to column PNG.
        inner_margin: Fraction of width to exclude from each edge.
        chart_path:   If set, save the analysis chart here.
        profile_path: If set, save the profile JSON here.

    Returns:
        dict with keys: cal, features, boundaries, items, profile.
    """
    cal = calibrate_column(image_path, inner_margin=inner_margin)
    features = find_features(cal)
    boundaries = find_item_boundaries(cal, features)
    items = split_column(image_path, boundaries, cal["content_top"], cal["content_bottom"])
    profile = build_profile(cal, features, boundaries)

    if chart_path:
        generate_charts(cal, features, boundaries, chart_path)

    if profile_path:
        save_profile(profile, profile_path)

    return {
        "cal": cal,
        "features": features,
        "boundaries": boundaries,
        "items": items,
        "profile": profile,
    }


# —————————————————————————
# Pretty printing
# —————————————————————————

def print_results(results):
    """Print a human-readable summary of the analysis."""
    cal = results["cal"]
    features = results["features"]
    boundaries = results["boundaries"]
    items = results["items"]
    h = cal["height_px"]

    print(f"Column: {cal['image_path']}")
    print(f"  Size: {cal['width_px']}x{h}  Strip: centre {int((1-2*cal['inner_margin'])*100)}%")
    print()

    print("Calibration:")
    print(f"  White={cal['white_level']:.1f}  Text={cal['text_level']:.1f}  "
          f"Peak={cal['peak_level']:.1f}  DR={cal['dynamic_range']:.1f}")
    print(f"  Paper baseline={cal['paper_baseline']:.1f} \u00b1{cal['paper_noise']:.1f}")
    if cal["line_height_px"]:
        print(f"  Line height: {cal['line_height_px']:.0f}px")
    else:
        print("  Line height: unknown")
    flags = []
    if cal["low_contrast"]: flags.append("LOW CONTRAST")
    if cal["show_through"]: flags.append("SHOW-THROUGH")
    if cal["noisy"]: flags.append("NOISY")
    if flags:
        print(f"  Quality warnings: {', '.join(flags)}")
    else:
        print("  Quality: good")
    print()

    print(f"Features: {len(features['gaps'])} gaps, {len(features['rules'])} rules, "
          f"{len(features['headlines'])} headlines, {len(features.get('boxes',[]))} boxes, "
          f"{len(features.get('illustrations',[]))} illustrations, "
          f"{len(features.get('para_ends',[]))} para_ends")
    for r in features["rules"]:
        print(f"  Rule y={r['y']} ({r['y']/h*100:.1f}%)  {r['type']:8s}  "
              f"span={r['span']:.0%}  darkness={r['darkness']:.0f}")
    for box in features.get("boxes", []):
        print(f"  Box  y={box['y_start']}-{box['y_end']} ({box['y_start']/h*100:.1f}-{box['y_end']/h*100:.1f}%)  "
              f"height={box['height']}px  top_rule={box['has_top_rule']}  bot_rule={box['has_bottom_rule']}")
    for ill in features.get("illustrations", []):
        print(f"  Illustration y={ill['y_start']}-{ill['y_end']} ({ill['y_start']/h*100:.1f}-{ill['y_end']/h*100:.1f}%)  "
              f"height={ill['height']}px")
    for hl in features.get("headlines", []):
        hl_type = hl.get("type", "?")
        print(f"  Headline y={hl['y_start']}-{hl['y_end']} ({hl['y_start']/h*100:.1f}-{hl['y_end']/h*100:.1f}%)  "
              f"{hl_type}  height={hl['height']}px  darkness={hl['peak_darkness']:.0f}")
    print()

    print(f"Item boundaries: {len(boundaries)}")
    for b in boundaries:
        print(f"  y={b} ({b/h*100:.1f}%)")
    print()

    print(f"Items: {len(items)}")
    for item in items:
        pct_start = px_to_pct(item["y_start"], h)
        pct_end = px_to_pct(item["y_end"], h)
        print(f"  [{item['index']}] y={item['y_start']}-{item['y_end']}  "
              f"({pct_start:.1f}%-{pct_end:.1f}%)  "
              f"height={item['y_end']-item['y_start']}px")


# —————————————————————————
# CLI
# —————————————————————————

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python find_splits.py <column_image.png> [--chart] [--profile]")
        sys.exit(1)

    image = sys.argv[1]
    stem = image.rsplit(".", 1)[0]
    chart = f"{stem}_analysis.png" if "--chart" in sys.argv else None
    profile = f"{stem}_profile.json" if "--profile" in sys.argv else None

    results = process_column(image, chart_path=chart, profile_path=profile)
    print_results(results)

    if chart:
        print(f"\nChart saved: {chart}")
    if profile:
        print(f"Profile saved: {profile}")
