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

import fitz
import numpy as np
import cv2


def _open_clean(pdf_path):
    """Open a PDF and strip red overlay lines."""
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc


def detect_ads(pdf_path, page_number=0, render_dpi=150,
               min_width_pct=15, min_height_pct=5,
               min_rect_ratio=0.85, column_pitch=None):
    """
    Detect bordered display advertisements on a PDF page.

    Uses adaptive thresholding and contour analysis to find
    rectangular bordered regions that span 2+ columns.

    Args:
        pdf_path:        Path to the PDF.
        page_number:     Zero-indexed page within the PDF.
        render_dpi:      DPI for rendering (150 is sufficient for ad detection).
        min_width_pct:   Minimum ad width as % of page width.
        min_height_pct:  Minimum ad height as % of page height.
        min_rect_ratio:  Minimum rectangularity (contour area / bounding rect area).
        column_pitch:    If known, used to estimate column span of each ad.

    Returns:
        List of ad dicts, sorted by area (largest first).
        Each dict has: x_pct, y_pct, w_pct, h_pct, rect_ratio,
        aspect, cols (estimated column span), confidence.
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pix = page.get_pixmap(dpi=render_dpi)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
    else:
        img = img.reshape(pix.h, pix.w)
    doc.close()

    if img.ndim == 3:
        grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        grey = img

    h, w = grey.shape

    # Adaptive threshold to handle varying scan darkness
    binary = cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 21, 10,
    )

    # Morphological close to connect broken border lines
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    # Find contours with hierarchy
    contours, hierarchy = cv2.findContours(
        closed, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE,
    )

    min_area = w * h * 0.005  # at least 0.5% of page area
    min_w = int(w * min_width_pct / 100)
    min_h = int(h * min_height_pct / 100)
    pitch = column_pitch or 12.0  # default guess if not provided

    ads = []
    for i, cnt in enumerate(contours):
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)
        rect_area = bw * bh
        if rect_area == 0:
            continue

        # Size filters
        if bw < min_w or bh < min_h:
            continue
        # Not the whole page
        if bw > w * 0.85 and bh > h * 0.85:
            continue

        rect_ratio = area / rect_area
        aspect = bw / bh if bh > 0 else 0

        # Rectangularity filter
        if rect_ratio < 0.40:
            continue
        # Aspect ratio filter: not a thin horizontal rule
        if aspect > 10.0 or aspect < 0.1:
            continue

        # Confidence scoring
        if rect_ratio > min_rect_ratio and 0.3 < aspect < 5.0:
            confidence = "high"
        elif rect_ratio > 0.70 and 0.2 < aspect < 8.0:
            confidence = "medium"
        else:
            confidence = "low"

        # Check for children (content inside the box)
        has_children = (hierarchy[0][i][2] != -1) if hierarchy is not None else False

        # Estimate column span
        cols = max(1, round(bw / w * 100 / pitch))

        ads.append({
            "x_pct": round(x / w * 100, 1),
            "y_pct": round(y / h * 100, 1),
            "w_pct": round(bw / w * 100, 1),
            "h_pct": round(bh / h * 100, 1),
            "x_end_pct": round((x + bw) / w * 100, 1),
            "y_end_pct": round((y + bh) / h * 100, 1),
            "rect_ratio": round(rect_ratio, 3),
            "aspect": round(aspect, 2),
            "cols": cols,
            "has_children": has_children,
            "confidence": confidence,
        })

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

    return deduped


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
