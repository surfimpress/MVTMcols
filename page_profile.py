"""
Per-page scan quality profiler for the Almonte Gazette pipeline.

Analyses a full PDF page at low resolution to establish:
- Paper baseline and noise level
- Ink darkness range and density
- Binding shadow location and severity
- Dynamic range and contrast quality
- Adaptive thresholds for column detection

This profile is computed once per page and passed to downstream
stages so they can adapt to the specific scan conditions.

Usage:
    from page_profile import profile_page

    prof = profile_page("1920-01-02-03.pdf")
    print(f"Paper: {prof['paper_mean']:.0f}, Ink: {prof['ink_mean']:.0f}")
    print(f"Column threshold: {prof['column_darkness_threshold']:.0f}")
"""

import fitz
import numpy as np


def _open_clean(pdf_path):
    """Open a PDF and strip red overlay lines."""
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc


def profile_page(pdf_path, page_number=0, profile_dpi=150):
    """
    Analyse a page and return a calibration profile.

    Uses low DPI (150) for speed — this is structural analysis,
    not content reading. Takes ~0.5s per page.

    Args:
        pdf_path:     Path to the PDF.
        page_number:  Zero-indexed page.
        profile_dpi:  DPI for the profile render (150 is sufficient).

    Returns:
        dict with calibration data and derived thresholds.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height
    pix = page.get_pixmap(dpi=profile_dpi)

    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = np.mean(img, axis=2)
    else:
        grey = img.reshape(pix.h, pix.w).astype(float)

    h, w = grey.shape
    inv = 255.0 - grey  # dark ink = high values
    doc.close()

    # ── Paper baseline ───────────────────────────────────────────────
    # Sample from top/bottom margins, inner 40% of width (avoid edges)
    margin_w_lo = int(w * 0.30)
    margin_w_hi = int(w * 0.70)
    margin_h = max(1, int(h * 0.03))

    top_margin = inv[:margin_h, margin_w_lo:margin_w_hi]
    bot_margin = inv[h - margin_h:, margin_w_lo:margin_w_hi]
    margin_samples = np.concatenate([top_margin.ravel(), bot_margin.ravel()])

    paper_mean = float(np.median(margin_samples))
    paper_std = float(np.std(margin_samples))

    # ── Binding shadow ───────────────────────────────────────────────
    # Check left and right 5% strips for sustained darkness
    edge_w = max(3, int(w * 0.05))
    left_strip = inv[int(h * 0.1):int(h * 0.9), :edge_w]
    right_strip = inv[int(h * 0.1):int(h * 0.9), w - edge_w:]

    left_edge_mean = float(left_strip.mean())
    right_edge_mean = float(right_strip.mean())

    # Shadow is on the side with higher mean darkness
    shadow_side = "left" if left_edge_mean > right_edge_mean else "right"
    shadow_severity = float(max(left_edge_mean, right_edge_mean))
    has_shadow = shadow_severity > 50

    # Determine content bounds: exclude shadow side
    if has_shadow and shadow_side == "left":
        content_x_lo = int(w * 0.08)
        content_x_hi = int(w * 0.95)
    elif has_shadow and shadow_side == "right":
        content_x_lo = int(w * 0.05)
        content_x_hi = int(w * 0.92)
    else:
        content_x_lo = int(w * 0.05)
        content_x_hi = int(w * 0.95)

    # ── Body region statistics ───────────────────────────────────────
    body = inv[int(h * 0.15):int(h * 0.85), content_x_lo:content_x_hi]

    body_mean = float(body.mean())
    body_std = float(body.std())

    # Ink vs paper separation
    ink_mask = body > 128
    ink_coverage = float(ink_mask.mean())

    ink_pixels = body[ink_mask]
    ink_mean = float(np.median(ink_pixels)) if len(ink_pixels) > 100 else 200.0
    ink_std = float(np.std(ink_pixels)) if len(ink_pixels) > 100 else 30.0

    paper_pixels = body[~ink_mask]
    paper_body_mean = float(np.median(paper_pixels)) if len(paper_pixels) > 100 else 0.0

    # Dynamic range
    p5 = float(np.percentile(body, 5))
    p95 = float(np.percentile(body, 95))
    dynamic_range = p95 - p5

    # ── Column profile (vertical lines) ──────────────────────────────
    # Mean darkness per pixel column across the body region.
    # Column rules show as peaks; text averages out.
    col_profile = body.mean(axis=0)
    col_median = float(np.median(col_profile))
    col_p90 = float(np.percentile(col_profile, 90))
    col_max = float(col_profile.max())

    # ── Derived thresholds ───────────────────────────────────────────

    # Column detection threshold: set relative to the page's own statistics.
    # A column rule must be darker than the median text column.
    # Use the median + 20% of the range from median to p90.
    # This adapts to both faint scans (low threshold) and dense scans (high).
    column_darkness_threshold = max(
        col_median + (col_p90 - col_median) * 0.3,
        paper_mean + 20,  # absolute minimum: must be above paper noise
        40,               # floor for very clean scans
    )

    # Row std threshold for boundary validation.
    # On noisy scans, allow higher std; on clean scans, be strict.
    row_std_threshold = min(45, max(25, paper_std * 3 + 15))

    # Valley depth threshold: relative to the page's contrast
    valley_depth_threshold = max(20, dynamic_range * 0.05)

    # ── Quality flags ────────────────────────────────────────────────
    flags = []
    if dynamic_range < 100:
        flags.append("low_contrast")
    if paper_mean > 30:
        flags.append("show_through")
    if paper_std > 15:
        flags.append("noisy_paper")
    if has_shadow:
        flags.append(f"binding_shadow_{shadow_side}")
    if ink_coverage < 0.05:
        flags.append("sparse_content")
    if ink_coverage > 0.40:
        flags.append("dense_content")

    return {
        # Page dimensions
        "pdf_path": pdf_path,
        "page_number": page_number,
        "width_px": w,
        "height_px": h,
        "profile_dpi": profile_dpi,

        # Paper
        "paper_mean": round(paper_mean, 1),
        "paper_std": round(paper_std, 1),
        "paper_body_mean": round(paper_body_mean, 1),

        # Ink
        "ink_mean": round(ink_mean, 1),
        "ink_std": round(ink_std, 1),
        "ink_coverage": round(ink_coverage, 4),

        # Contrast
        "dynamic_range": round(dynamic_range, 1),
        "body_mean": round(body_mean, 1),
        "body_std": round(body_std, 1),

        # Binding shadow
        "shadow_side": shadow_side if has_shadow else None,
        "shadow_severity": round(shadow_severity, 1),
        "left_edge_mean": round(left_edge_mean, 1),
        "right_edge_mean": round(right_edge_mean, 1),

        # Content bounds (as fraction of page width)
        "content_x_start_frac": content_x_lo / w,
        "content_x_end_frac": content_x_hi / w,

        # Column profile
        "col_profile_median": round(col_median, 1),
        "col_profile_p90": round(col_p90, 1),
        "col_profile_max": round(col_max, 1),

        # Derived thresholds for column detection
        "column_darkness_threshold": round(column_darkness_threshold, 0),
        "row_std_threshold": round(row_std_threshold, 0),
        "valley_depth_threshold": round(valley_depth_threshold, 0),

        # Quality
        "quality_flags": flags,
    }


def print_profile(prof):
    """Human-readable summary."""
    print(f"Page profile: {prof['pdf_path']} (page {prof['page_number']})")
    print(f"  Size: {prof['width_px']}x{prof['height_px']} at {prof['profile_dpi']} dpi")
    print(f"  Paper: mean={prof['paper_mean']:.0f}  std={prof['paper_std']:.0f}")
    print(f"  Ink: mean={prof['ink_mean']:.0f}  coverage={prof['ink_coverage']*100:.1f}%")
    print(f"  Dynamic range: {prof['dynamic_range']:.0f}")
    if prof["shadow_side"]:
        print(f"  Binding shadow: {prof['shadow_side']} (severity={prof['shadow_severity']:.0f})")
    print(f"  Column profile: median={prof['col_profile_median']:.0f}  "
          f"p90={prof['col_profile_p90']:.0f}  max={prof['col_profile_max']:.0f}")
    print(f"  Derived thresholds:")
    print(f"    darkness >= {prof['column_darkness_threshold']:.0f}")
    print(f"    row_std  <= {prof['row_std_threshold']:.0f}")
    print(f"    valley   >= {prof['valley_depth_threshold']:.0f}")
    if prof["quality_flags"]:
        print(f"  Flags: {', '.join(prof['quality_flags'])}")
    else:
        print(f"  Quality: good")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python page_profile.py <page.pdf>")
        sys.exit(1)
    prof = profile_page(sys.argv[1])
    print_profile(prof)
