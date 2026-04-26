"""
Four-probe body text classifier v5.

Clean rewrite consolidating v4 comb matching + horizontal probe analysis.

Pipeline:
    Pass 1: Rough period from autocorrelation
    Pass 2: Rough body zones via periodicity filter
    Pass 3: Refined period from concatenated body signal
    Pass 4: Comb phase alignment
    Pass 5: Body text margin detection (per-line first-ink histogram)
    Pass 6: Per-tooth measurement and initial classification
    Pass 7: Contextual fixes (bridging, subheading detection, false-positive suppression)
    Pass 8: Paragraph assembly and boundary placement

Output:
    - Annotated five-panel chart (signal, 10%, 50%, marked column, paragraphs)
    - Paragraph boundary data as list of dicts
"""

from PIL import Image, ImageDraw
import numpy as np
from scipy.signal import find_peaks
from scipy.ndimage import uniform_filter1d


# ── Utilities ────────────────────────────────────────────────────────────────

def autocorrelate(signal, min_lag=10, max_lag=60):
    """Return normalised autocorrelation and first peak lag."""
    sig = signal - signal.mean()
    ac = np.zeros(max_lag + 1)
    norm = np.dot(sig, sig)
    if norm < 1e-10:
        return ac, None
    for lag in range(max_lag + 1):
        ac[lag] = np.dot(sig[:len(sig) - lag], sig[lag:]) / norm if lag else 1.0
    peaks, _ = find_peaks(ac[min_lag:], prominence=0.03)
    peaks += min_lag
    return ac, peaks[0] if len(peaks) > 0 else None


def find_periodic_zones(row_means, period, h, threshold=0.25):
    """Find vertical zones with strong body-text periodicity."""
    window = period * 6
    half = window // 2
    periodicity = np.zeros(h)
    for y in range(half, h - half - period):
        chunk = row_means[y - half:y + half].copy()
        chunk -= chunk.mean()
        norm = np.dot(chunk, chunk)
        if norm < 1e-6:
            continue
        shifted = row_means[y - half + period:y + half + period]
        if len(shifted) < len(chunk):
            continue
        shifted = shifted[:len(chunk)].copy()
        shifted -= shifted.mean()
        periodicity[y] = np.dot(chunk, shifted) / (norm + 1e-10)

    is_body = periodicity > threshold
    # Fill small gaps
    kernel = period * 2
    for i in range(h):
        if not is_body[i]:
            ahead = is_body[i:min(i + kernel, h)]
            behind = is_body[max(0, i - kernel):i]
            if ahead.any() and behind.any():
                is_body[i] = True
    # Remove short runs
    min_run = period * 3
    in_run, run_start = False, 0
    for i in range(h + 1):
        val = is_body[i] if i < h else False
        if val and not in_run:
            run_start, in_run = i, True
        elif not val and in_run:
            if i - run_start < min_run:
                is_body[run_start:i] = False
            in_run = False

    zones = []
    in_zone = False
    for i in range(h + 1):
        val = is_body[i] if i < h else False
        if val and not in_zone:
            zone_start, in_zone = i, True
        elif not val and in_zone:
            zones.append((zone_start, i))
            in_zone = False
    return zones


# ── Core analysis ────────────────────────────────────────────────────────────

def analyse_column(image_path):
    """Run full analysis pipeline. Returns results dict."""

    img = Image.open(image_path).convert("L")
    bw = img.point(lambda p: 0 if p < 128 else 255, "L")
    arr = np.array(bw)
    h, w = arr.shape
    inv = 255.0 - arr.astype(float)
    row_means = inv.mean(axis=1)

    results = {"image_path": image_path, "width": w, "height": h}
    INK_THRESH = 5.0

    # ── Pass 1–3: Period detection ───────────────────────────────────────
    _, rough_period = autocorrelate(row_means)
    if rough_period is None:
        # No periodicity found — likely a non-text page (full-page ad,
        # blank page, or severely damaged scan)
        results["period"] = None
        results["rough_zones"] = []
        results["error"] = "no_periodicity"
        results["error_detail"] = "Autocorrelation found no periodic signal"
        results["teeth"] = []
        results["paragraphs"] = []
        results["row_means"] = row_means
        results["inv"] = inv
        results["img"] = img
        return results

    rough_zones = find_periodic_zones(row_means, rough_period, h)
    if not rough_zones:
        results["period"] = rough_period
        results["rough_zones"] = []
        results["error"] = "no_body_zones"
        results["error_detail"] = "No body text zones found despite periodic signal"
        results["teeth"] = []
        results["paragraphs"] = []
        results["row_means"] = row_means
        results["inv"] = inv
        results["img"] = img
        return results

    body_signal = np.concatenate([row_means[s:e] for s, e in rough_zones])
    _, refined_period = autocorrelate(body_signal)
    period = refined_period or rough_period
    results["period"] = period
    results["rough_zones"] = rough_zones

    # ── Pass 4: Phase alignment ──────────────────────────────────────────
    sample_peaks = []
    for zs, ze in rough_zones:
        pks, _ = find_peaks(row_means[zs:ze], distance=period - 3, prominence=5)
        sample_peaks.extend(pks + zs)
    sample_heights = np.array([row_means[p] for p in sample_peaks])
    median_body_dark = float(np.median(sample_heights)) if len(sample_heights) else 50.0

    # Estimate typical body spike height from sample peaks.
    # Measure the vertical extent of ink at each peak.
    sample_spike_heights = []
    for py in sample_peaks:
        if row_means[py] < 20:
            continue
        top = py
        while top > 0 and row_means[top - 1] > INK_THRESH:
            top -= 1
        bot = py
        while bot < h - 1 and row_means[bot + 1] > INK_THRESH:
            bot += 1
        sample_spike_heights.append(bot - top + 1)
    est_body_spike = float(np.median(sample_spike_heights)) if sample_spike_heights else period * 0.6
    # Heading threshold: anything taller than ~2x body spike is a heading
    heading_spike_thresh = max(est_body_spike * 2.5, 25)

    results["est_body_spike"] = est_body_spike
    results["heading_spike_thresh"] = heading_spike_thresh

    best_score, best_phase = -1, 0
    for phase in range(period):
        score, count = 0.0, 0
        tooth = phase
        while tooth < h:
            if any(zs <= tooth < ze for zs, ze in rough_zones):
                lo, hi = max(0, tooth - 2), min(h, tooth + 3)
                score += row_means[lo:hi].max()
                count += 1
            tooth += period
        if count > 0:
            score /= count
        if score > best_score:
            best_score, best_phase = score, phase

    results["phase"] = best_phase
    results["median_body_dark"] = median_body_dark

    # ── Pass 5: Body text margins ────────────────────────────────────────
    # Collect per-line first-ink positions from body zones
    all_first_inks = []
    all_last_inks = []
    for zs, ze in rough_zones:
        for y in range(zs, ze):
            row = inv[y, :]
            if row.max() < 128 or row.mean() < 30:
                continue
            for x in range(w):
                if row[x] > 128:
                    all_first_inks.append(x)
                    break
            for x in range(w - 1, -1, -1):
                if row[x] > 128:
                    all_last_inks.append(x)
                    break

    # Left margin: use low percentile of first-ink positions.
    # This gives the flush-left margin regardless of where it falls.
    fi = np.array(all_first_inks)
    body_left = int(np.percentile(fi, 25)) if len(fi) > 0 else 0

    body_right = int(np.percentile(all_last_inks, 90)) if all_last_inks else w - 1
    text_width = body_right - body_left

    # Provisional indent threshold — will be refined after teeth are measured.
    indent_x = body_left + 5

    results["body_left"] = body_left
    results["body_right"] = body_right
    results["text_width"] = text_width
    results["indent_x"] = indent_x

    # Short-line threshold: a line ending before 75% of text width
    short_right_x = body_left + int(text_width * 0.75)

    # Centre probe region for rule detection
    centre_lo = body_left + int(text_width * 0.40)
    centre_hi = body_left + int(text_width * 0.60)

    # ── Pass 6: Per-tooth measurement and initial classification ─────────
    strong_dark = median_body_dark * 0.75
    body_spike_heights = []

    teeth = []
    tooth = best_phase
    while tooth < h:
        lo, hi = max(0, tooth - 3), min(h, tooth + 4)
        local = row_means[lo:hi]
        peak_y = lo + int(np.argmax(local))
        peak_dark = float(local.max())

        # Spike extent
        spike_top = peak_y
        while spike_top > 0 and row_means[spike_top - 1] > INK_THRESH:
            spike_top -= 1
        spike_bot = peak_y
        while spike_bot < h - 1 and row_means[spike_bot + 1] > INK_THRESH:
            spike_bot += 1
        spike_height = spike_bot - spike_top + 1

        # Per-line ink positions
        if peak_dark >= INK_THRESH:
            firsts, lasts = [], []
            for yr in range(spike_top, spike_bot + 1):
                row = inv[yr, :]
                if row.max() < 128:
                    continue
                for x in range(w):
                    if row[x] > 128:
                        firsts.append(x)
                        break
                for x in range(w - 1, -1, -1):
                    if row[x] > 128:
                        lasts.append(x)
                        break
            line_left = int(np.median(firsts)) if firsts else w
            line_right = int(np.median(lasts)) if lasts else 0
            probe_rows = inv[spike_top:spike_bot + 1, :]
            centre_ink = float(probe_rows[:, centre_lo:centre_hi].mean())
        else:
            line_left, line_right = w, 0
            centre_ink = 0.0

        t = {
            "idx": len(teeth),
            "tooth": tooth,
            "peak_y": peak_y,
            "dark": peak_dark,
            "spike_top": spike_top,
            "spike_bot": spike_bot,
            "spike_h": spike_height,
            "l_x": line_left,
            "r_x": line_right,
            "centre": centre_ink,
        }

        # Initial classification
        is_flush_left = line_left <= indent_x
        is_indented = line_left > indent_x
        is_short = line_right < short_right_x
        has_centre = centre_ink > 30.0

        if peak_dark < INK_THRESH:
            t["cls"] = "empty"
        elif spike_height <= 5 and line_left <= 5 and line_right >= body_right - 15:
            # Thin full-width ink = rule line (horizontal separator)
            t["cls"] = "rule"
        elif spike_height <= 3 and peak_dark >= INK_THRESH and line_right - line_left > period * 2:
            # Very thin short ink = short rule (also a separator)
            t["cls"] = "rule"
        elif spike_height >= heading_spike_thresh:
            t["cls"] = "heading"
        elif peak_dark >= strong_dark:
            if is_flush_left and not is_short:
                t["cls"] = "body_cont"
            elif is_indented and not is_short:
                t["cls"] = "body_indent"
            elif is_flush_left and is_short:
                t["cls"] = "body_short"
            elif is_indented and is_short:
                t["cls"] = "body_short"
            else:
                t["cls"] = "body_cont"
        elif peak_dark >= INK_THRESH:
            # Weak darkness: same positional logic but lower confidence
            if is_flush_left and not is_short:
                t["cls"] = "body_cont"
                t["weak"] = True
            elif is_indented and not is_short:
                t["cls"] = "body_indent"
                t["weak"] = True
            elif is_flush_left and is_short:
                t["cls"] = "body_short"
            elif is_indented and is_short:
                t["cls"] = "body_short"
            elif not is_flush_left and not is_short and has_centre:
                t["cls"] = "centred"
            else:
                t["cls"] = "body_cont"
                t["weak"] = True

        if peak_dark >= strong_dark and spike_height < heading_spike_thresh:
            body_spike_heights.append(spike_height)

        teeth.append(t)
        tooth += period

    median_spike = float(np.median(body_spike_heights)) if body_spike_heights else period * 0.6

    # ── Pass 6b: Refine indent_x from actual tooth l_x distribution ───────
    # Collect l_x from all body-height, inked teeth (not headings/rules/empty)
    body_lx = [t["l_x"] for t in teeth
               if t["dark"] >= INK_THRESH
               and t["spike_h"] < heading_spike_thresh
               and t["l_x"] < w // 2]  # exclude any outliers
    if len(body_lx) >= 10:
        lx_arr = np.array(body_lx)
        # The flush-left cluster is the majority — find its upper edge.
        # Use the 75th percentile of the dominant low cluster.
        lx_hist = np.bincount(lx_arr, minlength=w)
        lx_smooth = uniform_filter1d(lx_hist.astype(float), size=3)

        # Walk from x=0 upward: find where the histogram first drops to
        # near-zero (the gap between flush and indent clusters).
        flush_peak = int(np.argmax(lx_smooth[:30]))
        gap_x = flush_peak + 3
        for x in range(flush_peak + 3, min(flush_peak + 40, w)):
            if lx_smooth[x] < 1.0:
                gap_x = x
                break
        indent_x = max(gap_x, body_left + 5)

        # Update stored value and recalculate short_right_x
        results["indent_x"] = indent_x
        short_right_x = body_left + int(text_width * 0.75)
        centre_lo = body_left + int(text_width * 0.40)
        centre_hi = body_left + int(text_width * 0.60)

        # Reclassify all teeth with refined indent_x
        for t in teeth:
            if t["cls"] in ("empty", "heading", "rule"):
                continue
            line_left = t["l_x"]
            line_right = t["r_x"]
            is_flush_left = line_left <= indent_x
            is_indented = line_left > indent_x
            is_short = line_right < short_right_x

            was_weak = t.get("weak", False)
            peak_dark = t["dark"]
            spike_height = t["spike_h"]

            if peak_dark >= strong_dark:
                if is_flush_left and not is_short:
                    t["cls"] = "body_cont"
                elif is_indented and not is_short:
                    t["cls"] = "body_indent"
                elif is_short:
                    t["cls"] = "body_short"
                else:
                    t["cls"] = "body_cont"
                t.pop("weak", None)
            elif peak_dark >= INK_THRESH:
                has_centre = t.get("centre", 0) > 30.0
                if is_flush_left and not is_short:
                    t["cls"] = "body_cont"
                    t["weak"] = True
                elif is_indented and not is_short:
                    t["cls"] = "body_indent"
                    t["weak"] = True
                elif is_short:
                    t["cls"] = "body_short"
                elif not is_flush_left and not is_short and has_centre:
                    t["cls"] = "centred"
                else:
                    t["cls"] = "body_cont"
                    t["weak"] = True

    # ── Pass 7: Contextual fixes ─────────────────────────────────────────

    body_set = {"body_cont", "body_indent", "body_short"}

    # 7a. False short-line suppression:
    # If a "body_short" line is followed immediately by another "body_short"
    # and the first one's right edge is within 85% of text width, reclassify
    # the first as body_cont (it was just a slightly narrow line).
    for i in range(len(teeth) - 1):
        t = teeth[i]
        nxt = teeth[i + 1]
        if (t["cls"] == "body_short" and nxt["cls"] == "body_short"
                and t["r_x"] >= short_right_x * 0.85):
            t["cls"] = "body_cont"
            t["fix"] = "false_short_suppressed"

    # 7b. Bridge single non-body teeth:
    # If a tooth is empty/other and is flanked by body text teeth,
    # bridge it to maintain the paragraph run.
    # IMPORTANT: don't cascade — if the previous tooth was also bridged,
    # this gap is part of a larger non-body region (heading box, etc).
    for i in range(1, len(teeth) - 1):
        t = teeth[i]
        prev = teeth[i - 1]
        nxt = teeth[i + 1]
        if t["cls"] in body_set or t["cls"] in ("heading", "rule"):
            continue
        if prev["cls"] not in body_set or nxt["cls"] not in body_set:
            continue
        # Don't cascade: if any of the last 4 teeth were bridged,
        # we're likely in a heading box or other non-body region.
        recent_bridges = sum(1 for j in range(max(0, i - 4), i)
                            if teeth[j].get("fix", "").startswith("bridged"))
        if recent_bridges >= 1:
            continue
        if t["cls"] == "empty":
            # Only bridge if prev is mid-paragraph (not end-of-paragraph)
            if prev["cls"] in ("body_cont", "body_indent"):
                t["cls"] = "body_cont"
                t["fix"] = "bridged_empty"
        elif t["dark"] >= INK_THRESH and t["spike_h"] < heading_spike_thresh:
            # Non-empty gap tooth: reclassify based on position
            is_fl = t["l_x"] <= indent_x
            is_sh = t["r_x"] < short_right_x
            if is_fl and not is_sh:
                t["cls"] = "body_cont"
            elif not is_fl and not is_sh:
                t["cls"] = "body_indent"
            elif is_fl and is_sh:
                t["cls"] = "body_short"
            else:
                t["cls"] = "body_cont"
            t["fix"] = "bridged"

    # 7c. Subheading detection:
    # Subheadings are characterised by:
    #   - Single line with actual ink, flush left, not short
    #   - Preceded (within 2 teeth) by body_short or empty (pattern A)
    #   - OR: notably lower darkness than surrounding body text (pattern B)
    #   - Followed (within 2 teeth) by body_indent
    low_dark_thresh = median_body_dark * 0.35
    for i in range(len(teeth)):
        t = teeth[i]
        if t["cls"] != "body_cont":
            continue
        if t["dark"] < INK_THRESH:
            continue  # must have actual ink to be a subheading

        # Check IMMEDIATE predecessor only for break detection.
        # Looking 2 teeth back caused false positives in short-item sections
        # (eg "Twenty Five Years Ago" snippets).
        prev_cls = teeth[i - 1]["cls"] if i > 0 else "empty"
        next_classes = [teeth[j]["cls"] for j in range(i + 1, min(len(teeth), i + 3))]

        has_preceding_break = prev_cls in ("body_short", "empty", "subheading", "rule")
        has_following_indent = any(c == "body_indent" for c in next_classes)
        is_low_dark = t["dark"] < low_dark_thresh

        if has_preceding_break and has_following_indent:
            t["cls"] = "subheading"
            t["fix"] = "subheading_preceded_by_break"
        elif is_low_dark and has_following_indent:
            # Low-darkness line before an indent — likely a subheading
            # even if preceded by body_cont (paragraph didn't end short)
            t["cls"] = "subheading"
            t["fix"] = "subheading_low_dark"
        elif has_preceding_break:
            # Also check: is the next non-empty tooth's left edge indented?
            for j in range(i + 1, min(len(teeth), i + 3)):
                nxt = teeth[j]
                if nxt["cls"] != "empty" and nxt["l_x"] > indent_x:
                    t["cls"] = "subheading"
                    t["fix"] = "subheading_indent_lookahead"
                    break

    results["teeth"] = teeth
    results["median_spike"] = median_spike

    # ── Pass 7d: Local margin adaptation ─────────────────────────────────
    # Different articles may have different left margins. A run of 3+
    # consecutive body_indent teeth means the local margin has shifted,
    # not that every line is indented. Reclassify using local context.

    # First pass: identify runs of consecutive body teeth and compute
    # their local median l_x
    i = 0
    while i < len(teeth):
        # Collect a run of body teeth
        run = []
        while i < len(teeth) and teeth[i]["cls"] in body_set:
            run.append(i)
            i += 1
        if len(run) >= 3:
            l_xs = [teeth[j]["l_x"] for j in run]
            local_median_lx = float(np.median(l_xs))
            # Reclassify: only teeth whose l_x exceeds local median by
            # indent_delta (8px ≈ half an em) are genuine indents.
            # The FIRST tooth in the run keeps its indent status.
            indent_delta = 12
            for k, j in enumerate(run):
                t = teeth[j]
                if t["cls"] == "body_indent":
                    if k == 0:
                        # First tooth in run: keep indent (new paragraph)
                        pass
                    elif t["l_x"] <= local_median_lx + indent_delta:
                        # Within local margin range: reclassify
                        t["cls"] = "body_cont"
                        t["fix"] = "local_margin_adapt"
        i += 1

    # ── Pass 8: Paragraph assembly ───────────────────────────────────────

    # Content bounds: first to last body text tooth + margin
    body_teeth = [t for t in teeth if t["cls"] in body_set]
    if body_teeth:
        content_top = body_teeth[0]["peak_y"] - period
        content_bot = body_teeth[-1]["peak_y"] + period
    else:
        content_top, content_bot = 0, h
    # Clamp to rough zones
    if rough_zones:
        content_top = max(content_top, rough_zones[0][0])
        content_bot = min(content_bot, rough_zones[-1][1])

    # Build paragraphs: runs of body text teeth.
    # A body_indent within a run starts a new paragraph (unless it's the
    # very first tooth in the run — that's expected).
    paragraphs = []
    run_start = None
    run_end = None
    for t in teeth:
        if t["cls"] in body_set:
            if t["peak_y"] < content_top or t["peak_y"] > content_bot:
                continue
            if run_start is None:
                # Starting a new run
                run_start = t["peak_y"]
                run_end = t["peak_y"]
            elif t["cls"] == "body_indent":
                # Indent mid-run: close current paragraph, start new one
                paragraphs.append({"start": run_start, "end": run_end,
                                   "ended_by": "indent"})
                run_start = t["peak_y"]
                run_end = t["peak_y"]
            else:
                run_end = t["peak_y"]
            if t["cls"] == "body_short":
                paragraphs.append({"start": run_start, "end": run_end,
                                   "ended_by": "short_line"})
                run_start = run_end = None
        else:
            if run_start is not None:
                paragraphs.append({"start": run_start, "end": run_end,
                                   "ended_by": t["cls"]})
                run_start = run_end = None
    if run_start is not None:
        paragraphs.append({"start": run_start, "end": run_end,
                           "ended_by": "column_end"})

    # Quality filter: single-line paragraphs must have strong darkness.
    # This prevents noise (nearly-invisible ink) from creating spurious paras
    # while preserving legitimate short news items.
    filtered = []
    for p in paragraphs:
        lines = round((p["end"] - p["start"]) / period) + 1
        if lines >= 2:
            filtered.append(p)
        else:
            # Single-line: check darkness of the tooth at this y
            tooth_dark = 0
            for t in teeth:
                if abs(t["peak_y"] - p["start"]) <= period // 2:
                    tooth_dark = t["dark"]
                    break
            if tooth_dark >= 20:
                filtered.append(p)
    paragraphs = filtered

    # Compute boundaries between consecutive paragraphs
    for i in range(len(paragraphs)):
        p = paragraphs[i]
        p["num"] = i + 1
        if i < len(paragraphs) - 1:
            nxt = paragraphs[i + 1]
            gap = nxt["start"] - p["end"]
            if gap < period * 2:
                # Small gap: single midpoint line
                p["boundary_y"] = (p["end"] + nxt["start"]) // 2
                p["boundary_mode"] = "mid"
            else:
                # Large gap (subheading etc): place end at last line + half period,
                # start at first line - half period
                p["boundary_y_end"] = p["end"] + period // 2
                p["boundary_y_start"] = nxt["start"] - period // 2
                p["boundary_mode"] = "split"

    results["paragraphs"] = paragraphs
    results["content_top"] = content_top
    results["content_bot"] = content_bot
    results["row_means"] = row_means
    results["inv"] = inv
    results["img"] = img

    return results


# ── Visualisation ────────────────────────────────────────────────────────────

def render_chart(results, output_path):
    """Render five-panel chart with paragraph annotations."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.gridspec as gridspec
    from matplotlib.patches import ConnectionPatch

    img = results["img"]
    h, w = results["height"], results["width"]
    row_means = results["row_means"]
    inv = results["inv"]
    teeth = results["teeth"]
    paragraphs = results["paragraphs"]
    period = results["period"]

    # Build marked column overlay
    overlay = img.convert("RGBA")
    draw_layer = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(draw_layer)

    colours = {
        "body_cont":   (70, 130, 200, 80),
        "body_indent": (40, 200, 40, 120),
        "body_short":  (240, 160, 40, 120),
        "centred":     (200, 40, 200, 120),
        "subheading":  (200, 40, 200, 120),
        "heading":     (200, 40, 40, 80),
        "rule":        (120, 120, 120, 180),
        "full_rule":   (100, 100, 100, 120),
        "short_rule":  (140, 140, 140, 120),
        "faint":       (180, 180, 180, 60),
        "empty":       None,
        "other":       (150, 100, 50, 80),
    }

    for t in teeth:
        col = colours.get(t["cls"])
        if col is None:
            continue
        draw.rectangle([(0, t["spike_top"]), (w, t["spike_bot"])], fill=col)
        draw.line([(0, t["peak_y"]), (w, t["peak_y"])],
                  fill=(col[0], col[1], col[2], 160), width=1)

    # Paragraph break markers on column
    for p in paragraphs:
        if "boundary_y" in p:
            y = p["boundary_y"]
            draw.line([(0, y), (w, y)], fill=(220, 40, 40, 200), width=2)
        elif "boundary_y_end" in p:
            ye = p["boundary_y_end"]
            ys = p["boundary_y_start"]
            draw.line([(0, ye), (w, ye)], fill=(220, 40, 40, 160), width=2)
            draw.line([(0, ys), (w, ys)], fill=(40, 180, 40, 160), width=2)

    # Margin lines
    draw.line([(results["body_left"], 0), (results["body_left"], h)],
              fill=(40, 180, 40, 60), width=1)
    draw.line([(results["indent_x"], 0), (results["indent_x"], h)],
              fill=(200, 200, 40, 40), width=1)

    marked = Image.alpha_composite(overlay, draw_layer)
    marked_arr = np.array(marked.convert("RGB"))

    # Chart
    normalised = row_means / (row_means.max() or 1) * 100.0
    binary_10 = np.where(normalised >= 10, 100.0, 0.0)
    binary_50 = np.where(normalised >= 50, 100.0, 0.0)

    aspect = h / w
    col_w_in = 1.8
    fig_h = col_w_in * aspect
    fig = plt.figure(figsize=(18, fig_h))
    gs = gridspec.GridSpec(1, 5, width_ratios=[1.0, 0.3, 0.3, 1.0, 0.8],
                           wspace=0.04)
    ys = np.arange(h)
    blue = "#1a3a6b"

    ax_sig = fig.add_subplot(gs[0])
    ax_sig.plot(row_means, ys, linewidth=0.3, color=blue)
    ax_sig.set_xlim(0, 260)
    ax_sig.set_ylim(h, 0)
    ax_sig.set_xlabel("Mean darkness", fontsize=8)
    ax_sig.set_title("Signal", fontsize=9)
    ax_sig.set_ylabel("Row (px)", fontsize=8)
    ax_sig.tick_params(labelsize=7)

    ax_10 = fig.add_subplot(gs[1], sharey=ax_sig)
    ax_10.barh(ys, binary_10, height=1, color=blue, linewidth=0)
    ax_10.set_xlim(0, 110)
    ax_10.set_title("10%", fontsize=9)
    ax_10.set_xticks([])
    plt.setp(ax_10.get_yticklabels(), visible=False)

    ax_50 = fig.add_subplot(gs[2], sharey=ax_sig)
    ax_50.barh(ys, binary_50, height=1, color=blue, linewidth=0)
    ax_50.set_xlim(0, 110)
    ax_50.set_title("50%", fontsize=9)
    ax_50.set_xticks([])
    plt.setp(ax_50.get_yticklabels(), visible=False)

    ax_img = fig.add_subplot(gs[3], sharey=ax_sig)
    ax_img.imshow(marked_arr, aspect="equal")
    ax_img.set_title("Four-probe v5", fontsize=9)
    ax_img.set_xticks([])
    plt.setp(ax_img.get_yticklabels(), visible=False)

    ax_ann = fig.add_subplot(gs[4], sharey=ax_sig)
    ax_ann.set_xlim(0, 100)
    ax_ann.set_ylim(h, 0)
    ax_ann.set_title("Paragraphs", fontsize=9)
    ax_ann.set_xticks([])
    ax_ann.set_yticks([])
    ax_ann.set_facecolor("white")
    for spine in ax_ann.spines.values():
        spine.set_visible(False)

    # Annotation: bridge lines from column edge into annotation panel
    lx = 8
    fs = 5.5
    lc = "#333333"
    ec = "#666666"

    def bridge(y_pos):
        con = ConnectionPatch(
            xyA=(w, y_pos), coordsA=ax_img.transData,
            xyB=(60, y_pos), coordsB=ax_ann.transData,
            color=lc, linewidth=0.6, clip_on=False)
        fig.add_artist(con)

    for p in paragraphs:
        num = p["num"]

        if num == 1:
            # First paragraph start
            bridge(p["start"])
            ax_ann.text(lx, p["start"] + period * 0.35,
                        f"{num:02d} start", fontsize=fs,
                        color=lc, va="top", fontfamily="monospace")

        if "boundary_y" in p:
            # Small gap: single midpoint line
            y = p["boundary_y"]
            bridge(y)
            ax_ann.text(lx, y - period * 0.15,
                        f"{num:02d} end", fontsize=fs,
                        color=ec, va="bottom", fontfamily="monospace")
            ax_ann.text(lx, y + period * 0.35,
                        f"{num+1:02d} start", fontsize=fs,
                        color=lc, va="top", fontfamily="monospace")
        elif "boundary_y_end" in p:
            # Large gap: separate end and start lines
            ye = p["boundary_y_end"]
            ys_line = p["boundary_y_start"]
            bridge(ye)
            ax_ann.text(lx, ye - period * 0.15,
                        f"{num:02d} end", fontsize=fs,
                        color=ec, va="bottom", fontfamily="monospace")
            bridge(ys_line)
            ax_ann.text(lx, ys_line + period * 0.35,
                        f"{num+1:02d} start", fontsize=fs,
                        color=lc, va="top", fontfamily="monospace")
        elif num == len(paragraphs):
            # Last paragraph end
            bridge(p["end"])
            ax_ann.text(lx, p["end"] - period * 0.15,
                        f"{num:02d} end", fontsize=fs,
                        color=ec, va="bottom", fontfamily="monospace")

    # Grid lines
    for pct in range(0, 101, 10):
        y_line = int(h * pct / 100)
        for ax in [ax_sig, ax_10, ax_50, ax_img, ax_ann]:
            ax.axhline(y_line, color="blue", alpha=0.10, linewidth=0.5)

    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()

    # Save marked column separately
    col_path = output_path.replace(".png", "_col.png")
    marked.save(col_path)

    return output_path, col_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(image_path=None, output_path=None):
    if image_path is None:
        print("Usage: python four_probe_v5.py <column_image.png> [output.png]")
        return None
    if output_path is None:
        stem = image_path.rsplit(".", 1)[0]
        output_path = f"{stem}_v5.png"

    print(f"Analysing {image_path}...")
    r = analyse_column(image_path)

    if r.get("error"):
        print(f"  ERROR: {r['error']} — {r.get('error_detail', '')}")
        return r

    print(f"  Period: {r['period']}px")
    print(f"  Phase: {r['phase']}")
    print(f"  Median body darkness: {r['median_body_dark']:.1f}")
    print(f"  Body left margin: x={r['body_left']}")
    print(f"  Indent boundary: x={r['indent_x']}")
    print(f"  Body right margin: x={r['body_right']}")
    print(f"  Short-line cutoff: x={r['body_left'] + int(r['text_width'] * 0.75)}")
    print(f"  Content: y={r['content_top']}\u2013{r['content_bot']}")

    from collections import Counter
    counts = Counter(t["cls"] for t in r["teeth"])
    print("\n  Classification:")
    for cls, n in sorted(counts.items(), key=lambda x: -x[1]):
        fix_count = sum(1 for t in r["teeth"] if t["cls"] == cls and "fix" in t)
        fix_note = f" ({fix_count} fixed)" if fix_count else ""
        print(f"    {cls}: {n}{fix_note}")

    print(f"\n  Paragraphs: {len(r['paragraphs'])}")
    for p in r["paragraphs"]:
        lines = round((p["end"] - p["start"]) / r["period"]) + 1
        mode = p.get("boundary_mode", "terminal")
        print(f"    {p['num']:02d}: y={p['start']:4d}\u2013{p['end']:4d} "
              f"({lines:2d} lines, ended by {p['ended_by']}, boundary: {mode})")

    # Detailed teeth dump for piano article
    print("\n  Teeth y=750\u20132500:")
    for t in r["teeth"]:
        if 750 <= t["peak_y"] <= 2500:
            fix = t.get("fix", "")
            weak = " WEAK" if t.get("weak") else ""
            print(f"    y={t['peak_y']:4d} d={t['dark']:5.1f} "
                  f"spk={t['spike_h']:3d} l={t['l_x']:3d} r={t['r_x']:3d} "
                  f"{t['cls']:12}{weak} {fix}")

    print("\nRendering chart...")
    chart, col = render_chart(r, output_path)
    print(f"  Saved: {chart}")
    print(f"  Saved: {col}")

    return r


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python four_probe_v5.py <column_image.png> [output.png]")
        sys.exit(1)
    img = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    main(img, out)
