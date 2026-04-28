"""Visualise the CV pre-processing pipeline stage by stage.

A reusable diagnostic for "what does each step do to the page?".
Given a page_raw PNG, produces a 4-tile comparison sheet showing:

  1. Original page
  2. After cv2.connectedComponentsWithStats size-filter (drops blobs
     smaller than MIN_AREA px from the adaptive-threshold binary —
     strips body text while keeping ad frames, headline type, logos)
  3. Canny edges of the size-filtered binary
  4. Hough probabilistic line detection on those edges, rendered over
     the original page in green/blue (horizontal/vertical)

Run from the repo root:

    python3 explore_pipeline.py columns/1947-02-27/p8/page_raw.png
    python3 explore_pipeline.py columns/1947-02-27/p1/page_raw.png \\
            --out screenshots/exploration/p1.png

The defaults match what we tuned on 1947-02-27 p8. All thresholds are
exposed as CLI flags so this can be re-run with different parameters
without editing the file.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ── Stage parameters ──────────────────────────────────────────────
# Defaults tuned on 1947-02-27 p8 in screenshots/hough_p8/. The
# Variant-B sweep showed MIN_AREA in [300, 500] strips text cleanly
# while preserving frames; 500 leaves a cleaner picture.

DEFAULTS = dict(
    adaptive_block=21,        # cv2.adaptiveThreshold neighbourhood
    adaptive_C=10,            # threshold offset
    cc_min_area=500,          # drop blobs smaller than this (px)
    canny_low=50,
    canny_high=150,
    canny_aperture=3,
    hough_threshold=80,       # Hough accumulator votes
    hough_min_length=40,      # minimum segment length (px)
    hough_max_gap=8,          # bridge breaks up to this many px
    long_line_pct=10,         # "long" segment >= N% of page dim
    orientation_tol_deg=3,    # H/V classification tolerance
    # Rectangle-detection gates (matches detect_ads.py's basic shape
    # filter — no rect_ratio threshold, no edge-touching downgrade).
    rect_min_area_pct=0.5,    # >= N% of page area to be a candidate
    rect_min_dim_pct=5,       # at least one dim >= N% of that page dim
    rect_max_area_pct=50,     # reject candidates >= N% of page area
    # Morphological close — bridges small gaps in Canny edges so the
    # contour finder sees rectangles as closed loops. 0 = no close.
    close_kernel=0,           # square kernel size (px); 0 = off
    close_iter=1,             # cv2.morphologyEx iterations
    # 1px ink frame drawn at this inset from each edge of the cc
    # binary — closes ad frames that run open into the image edge
    # (e.g. top-right corner of an outer-column ad). 0 = off.
    border_inset=0,           # px inset from each edge; 0 = off
)


@dataclass
class Stage:
    """One stage of the pipeline. `image` is a PIL.Image RGB ready to tile."""
    name: str
    image: Image.Image
    note: str = ""


def _font(size: int = 18):
    try:
        return ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _label_top(im: Image.Image, label: str) -> Image.Image:
    """Black bar with white label across the top of the tile."""
    out = im.copy()
    draw = ImageDraw.Draw(out)
    draw.rectangle([0, 0, out.size[0], 36], fill=(0, 0, 0))
    draw.text((10, 6), label, fill=(255, 255, 255), font=_font(18))
    return out


def stage_original(page_raw: Image.Image) -> Stage:
    return Stage("original", page_raw.copy(), "page_raw input")


def stage_cc_filter(gray: np.ndarray, params: dict) -> tuple[Stage, np.ndarray]:
    """Adaptive-threshold the page, then drop CC blobs below cc_min_area.

    Returns the visual stage AND the cleaned binary so downstream
    stages (Canny, Hough) can chain off it without recomputing."""
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV,
        params["adaptive_block"], params["adaptive_C"])
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
        binary, connectivity=8)
    filt = np.zeros_like(binary)
    n_kept = 0
    for lbl in range(1, n_labels):
        if stats[lbl, cv2.CC_STAT_AREA] >= params["cc_min_area"]:
            filt[labels == lbl] = 255
            n_kept += 1
    inv = 255 - filt  # paper=white, ink=black for display
    pim = Image.fromarray(cv2.cvtColor(inv, cv2.COLOR_GRAY2RGB))
    note = (f"connectedComponents >= {params['cc_min_area']}px: "
            f"{n_kept} of {n_labels - 1} blobs kept")
    return Stage("cc_filter", pim, note), filt


def stage_border(filt: np.ndarray, params: dict) -> tuple[Stage, np.ndarray]:
    """Paint a 1-px ink line at `border_inset` from each edge of the
    cc binary. Hypothesis: ads whose frame runs into the image edge
    (open at a corner because the line stops at the page boundary) get
    closed when we add a parallel line just inside, which intersects
    the frame's open ends and completes the loop.

    Mutates a copy — original `filt` is preserved for caller."""
    out = filt.copy()
    n = int(params["border_inset"])
    H, W = out.shape
    if n > 0 and n < H // 2 and n < W // 2:
        out[n, :] = 255
        out[H - 1 - n, :] = 255
        out[:, n] = 255
        out[:, W - 1 - n] = 255
    inv = 255 - out
    pim = Image.fromarray(cv2.cvtColor(inv, cv2.COLOR_GRAY2RGB))
    note = (f"1-px ink frame at inset={n} (closes edge-touching ads)")
    return Stage("border", pim, note), out


def stage_canny(filtered_bin: np.ndarray, params: dict) -> tuple[Stage, np.ndarray]:
    """Canny edges on the cleaned binary."""
    edges = cv2.Canny(filtered_bin,
                      params["canny_low"], params["canny_high"],
                      apertureSize=params["canny_aperture"])
    inv = 255 - edges
    pim = Image.fromarray(cv2.cvtColor(inv, cv2.COLOR_GRAY2RGB))
    note = (f"Canny({params['canny_low']},{params['canny_high']}): "
            f"{int(np.count_nonzero(edges)):,} edge px")
    return Stage("canny", pim, note), edges


def stage_close(edges: np.ndarray, params: dict) -> tuple[Stage, np.ndarray]:
    """Morphological close (dilate then erode) on Canny edges.

    Bridges gaps smaller than the kernel so a Canny outline of a
    rectangle becomes a closed loop, allowing findContours to enclose
    the interior. Kernel of 0 returns the input unchanged (caller
    should not invoke this stage when close_kernel == 0)."""
    k = params["close_kernel"]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel,
                              iterations=params["close_iter"])
    inv = 255 - closed
    pim = Image.fromarray(cv2.cvtColor(inv, cv2.COLOR_GRAY2RGB))
    note = (f"MORPH_CLOSE k={k} iter={params['close_iter']}: "
            f"{int(np.count_nonzero(closed)):,} px "
            f"(was {int(np.count_nonzero(edges)):,})")
    return Stage("close", pim, note), closed


def stage_rects(canvas: Image.Image, source: np.ndarray,
                params: dict, source_label: str = "edges") -> Stage:
    """Rectangle detection via cv2.findContours + bounding-box gates.

    `source` is the binary image fed to findContours (Canny edges or
    a filled binary). `canvas` is the image to draw rectangles on top
    of — usually the page_raw, but for "rects on cc binary" the caller
    can pass the cc binary rendered as RGB so the rectangles land on
    the substrate they came from with no other distractions.

    The gates mirror detect_ads.py's basic shape filter (area, min
    dim, max area) but stop short of rect_ratio scoring or
    edge-touching downgrades — we want to see what enters the
    pipeline, not what survives every filter."""
    W, H = canvas.size
    contours, _ = cv2.findContours(
        source, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    min_area = params["rect_min_area_pct"] / 100 * W * H
    max_area = params["rect_max_area_pct"] / 100 * W * H
    min_dim_w = params["rect_min_dim_pct"] / 100 * W
    min_dim_h = params["rect_min_dim_pct"] / 100 * H

    cands = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        if w * h == 0:
            continue
        # Whole-page contour — skip
        if w > 0.85 * W and h > 0.85 * H:
            continue
        if w * h > max_area:
            continue
        if w < min_dim_w and h < min_dim_h:
            continue
        rr = area / (w * h)
        cands.append((x, y, w, h, rr))

    canvas = canvas.copy()
    draw = ImageDraw.Draw(canvas)
    for x, y, w, h, rr in cands:
        draw.rectangle([x, y, x + w, y + h], outline=(0, 100, 240), width=3)
        draw.text((x + 4, y + 4), f"rr={rr:.2f}",
                  fill=(0, 60, 200), font=_font(14))

    note = (f"{len(contours):,} contours from {source_label} -> "
            f"{len(cands)} candidate rects (blue)")
    return Stage("rects", canvas, note)


def stage_hough(page_raw: Image.Image, edges: np.ndarray,
                params: dict) -> Stage:
    """Hough probabilistic line detection rendered over the original."""
    W, H = page_raw.size
    lines = cv2.HoughLinesP(
        edges, rho=1, theta=np.pi / 180,
        threshold=params["hough_threshold"],
        minLineLength=params["hough_min_length"],
        maxLineGap=params["hough_max_gap"])
    canvas = page_raw.copy()
    draw = ImageDraw.Draw(canvas)
    n_h, n_v, n_d = 0, 0, 0
    if lines is not None:
        long_pct = params["long_line_pct"] / 100.0
        tol = params["orientation_tol_deg"]
        import math
        for s in lines[:, 0, :]:
            x1, y1, x2, y2 = s
            L = math.hypot(x2 - x1, y2 - y1)
            a = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180
            if a < tol or a > 180 - tol:
                if L >= long_pct * W:
                    draw.line([(int(x1), int(y1)), (int(x2), int(y2))],
                              fill=(0, 200, 0), width=2)
                    n_h += 1
            elif abs(a - 90) < tol:
                if L >= long_pct * H:
                    draw.line([(int(x1), int(y1)), (int(x2), int(y2))],
                              fill=(0, 100, 240), width=2)
                    n_v += 1
            else:
                n_d += 1  # not drawn — we only paint long H/V
    note = f"long horiz={n_h} (green), long vert={n_v} (blue)"
    return Stage("hough", canvas, note)


def build_sheet(stages: list[Stage], out_path: str,
                tile_height: int = 700, gap: int = 12,
                full_size: bool = False) -> None:
    """Tile stages in a single horizontal row with labels.

    full_size=True keeps each tile at native resolution (no LANCZOS
    downscaling). Useful when fine pixel structure is the thing being
    inspected — e.g. asking whether a thin frame line is intact —
    since LANCZOS softens 1-2 px features below visibility."""
    labelled = []
    for st in stages:
        lab = f"{st.name}  —  {st.note}" if st.note else st.name
        labelled.append(_label_top(st.image, lab))
    if full_size:
        prepared = labelled
        common_h = max(im.size[1] for im in prepared)
    else:
        prepared = []
        for im in labelled:
            s = tile_height / im.size[1]
            prepared.append(im.resize((int(im.size[0] * s), tile_height),
                                      Image.LANCZOS))
        common_h = tile_height
    total_w = sum(im.size[0] for im in prepared) + gap * (len(prepared) - 1)
    sheet = Image.new("RGB", (total_w, common_h), (240, 240, 240))
    x = 0
    for im in prepared:
        sheet.paste(im, (x, 0))
        x += im.size[0] + gap
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    sheet.save(out_path)


FINAL_STAGES = ("hough", "rects", "rects_from_cc")


def run(page_raw_path: str, out_path: str, params: dict,
        final: str = "hough", full_size: bool = False) -> None:
    """final ∈ FINAL_STAGES picks which fourth tile to render:
       - hough:         Hough lines on Canny edges (default)
       - rects:         findContours rectangles on Canny edges
       - rects_from_cc: findContours rectangles on cc-filtered binary
                        (skips the Canny step entirely)"""
    if final not in FINAL_STAGES:
        raise SystemExit(f"--final must be one of {FINAL_STAGES}")

    page_pil = Image.open(page_raw_path).convert("RGB")
    gray = cv2.cvtColor(np.array(page_pil), cv2.COLOR_RGB2GRAY)

    stages = []
    stages.append(stage_original(page_pil))
    s_cc, filt_bin = stage_cc_filter(gray, params)
    stages.append(s_cc)

    # rects_from_cc skips Canny and morph-close entirely — the contour
    # pass runs against the cc binary directly, and we draw the
    # rectangles ON the cc binary itself (not the page) so the user
    # sees rects landing on the substrate they came from.
    if final == "rects_from_cc":
        canvas = s_cc.image
        source_bin = filt_bin
        source_label = "cc binary"
        if params["border_inset"] > 0:
            s_border, bordered = stage_border(filt_bin, params)
            stages.append(s_border)
            canvas = s_border.image
            source_bin = bordered
            source_label = f"cc binary + border inset={params['border_inset']}"
        s_final = stage_rects(canvas, source_bin, params,
                              source_label=source_label)
        stages.append(s_final)
        for st in stages:
            print(f"  {st.name:<12}  {st.note}")
        build_sheet(stages, out_path, full_size=full_size)
        print(f"\nSheet: {out_path}")
        return

    # All other final stages route through Canny.
    s_canny, edges = stage_canny(filt_bin, params)
    stages.append(s_canny)

    # Optional morph-close on Canny output. Inserted as its own tile
    # so the visual effect is observable; downstream stages use the
    # closed map instead of raw edges.
    edges_for_final = edges
    if params["close_kernel"] > 0:
        s_close, closed = stage_close(edges, params)
        stages.append(s_close)
        edges_for_final = closed

    if final == "hough":
        s_final = stage_hough(page_pil, edges_for_final, params)
    else:  # rects
        src_label = ("canny+close" if params["close_kernel"] > 0
                     else "canny")
        s_final = stage_rects(page_pil, edges_for_final, params,
                              source_label=src_label)
    stages.append(s_final)

    for st in stages:
        print(f"  {st.name:<12}  {st.note}")

    build_sheet(stages, out_path)
    print(f"\nSheet: {out_path}")


def _argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Visualise pipeline stages on a single page.")
    ap.add_argument("page_raw", help="Path to page_raw.png")
    ap.add_argument("--out",
                    help="Output sheet path "
                         "(default: screenshots/exploration/<page>.png)")
    ap.add_argument("--final", choices=FINAL_STAGES, default="hough",
                    help="Which final stage to render (default: hough). "
                         "rects = findContours on Canny; "
                         "rects_from_cc = findContours on cc binary "
                         "(skips Canny).")
    ap.add_argument("--full-size", action="store_true",
                    help="Render tiles at native resolution (no LANCZOS "
                         "downscaling). Use when 1-2 px features matter.")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k.replace('_', '-')}", type=type(v), default=v,
                        help=f"(default {v})")
    return ap


def main(argv=None):
    args = _argparser().parse_args(argv)
    params = {k: getattr(args, k) for k in DEFAULTS}

    out = args.out
    if out is None:
        # Derive a default path from the input page_raw location:
        # columns/1947-02-27/p8/page_raw.png ->
        # screenshots/exploration/1947-02-27_p8_hough.png
        # (Suffix the final-stage name so different runs don't clobber.)
        parts = os.path.normpath(args.page_raw).split(os.sep)
        try:
            i = parts.index("columns")
            issue, page_dir = parts[i + 1], parts[i + 2]
            stem = f"{issue}_{page_dir}"
        except (ValueError, IndexError):
            stem = os.path.splitext(os.path.basename(args.page_raw))[0]
        out = os.path.join("screenshots", "exploration",
                           f"{stem}_{args.final}.png")

    run(args.page_raw, out, params, final=args.final,
        full_size=args.full_size)


if __name__ == "__main__":
    main()
