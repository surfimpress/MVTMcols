"""Image and vector-drawing regions: photos, display-ad borders, line rules.

Pulls from page.get_images() (raster images embedded in the PDF) and
page.get_drawings() (vector primitives — line/rect/curve). Used to
identify:

  - Photos       : raster images, usually inside an article block
  - Display ads  : drawn rectangles enclosing little/no body text,
                   often containing their own raster image
  - Hairline rules: thin drawn lines used as article dividers

Note: in the Adobe Paper Capture PDFs (1990-2007), the whole page
image is usually a single page-sized raster — `get_images` will
return 1 huge image. We filter that out (image covering ≥80% of the
page).
"""
from dataclasses import dataclass


@dataclass
class ImageRegion:
    x0: float; y0: float; x1: float; y1: float
    @property
    def bbox(self): return (self.x0, self.y0, self.x1, self.y1)
    @property
    def area(self): return (self.x1 - self.x0) * (self.y1 - self.y0)


@dataclass
class DrawnRect:
    x0: float; y0: float; x1: float; y1: float
    @property
    def bbox(self): return (self.x0, self.y0, self.x1, self.y1)
    @property
    def area(self): return (self.x1 - self.x0) * (self.y1 - self.y0)


def extract_image_regions(page, page_w, page_h):
    """Return raster image rectangles on this page, excluding the
    full-page background image (Paper-Capture artefact)."""
    regions = []
    try:
        infos = page.get_images(full=True)
    except Exception:
        return regions
    page_area = page_w * page_h
    for info in infos:
        xref = info[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for r in rects:
            w, h = r.width, r.height
            if w < 30 or h < 30:
                continue
            if (w * h) > page_area * 0.8:
                continue   # whole-page background image
            regions.append(ImageRegion(x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1))
    return regions


def extract_drawn_rects(page, page_w, page_h, min_w=80, min_h=80):
    """Return rectangles drawn on the page.

    Adobe Paper Capture PDFs almost never expose enclosed rectangles as
    single drawings; they expose horizontal and vertical stroke lines.
    So this does two passes:

    (1) Direct: any drawing whose .rect is at least min_w × min_h
        and isn't whole-page.
    (2) Constructed: find pairs of horizontal strokes ≥ min_w wide
        that share x-extent, with a vertical gap ≥ min_h, and
        verticals at the left+right joining them — that's an ad
        frame.

    Filtering out the page-edge frame and tiny boxes.
    """
    rects = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return rects
    page_area = page_w * page_h

    horiz = []   # (x0, x1, y)
    vert = []    # (x, y0, y1)
    for d in drawings:
        r = d.get("rect")
        if r is None:
            continue
        w, h = r.width, r.height
        # Direct pass — full rectangle
        if w >= min_w and h >= min_h and (w * h) <= page_area * 0.9:
            rects.append(DrawnRect(x0=r.x0, y0=r.y0, x1=r.x1, y1=r.y1))
        # Index strokes for the constructed pass
        if h <= 3 and w >= 30:
            horiz.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
        elif w <= 3 and h >= 30:
            vert.append(((r.x0 + r.x1) / 2, r.y0, r.y1))

    # Constructed pass: top + bottom horizontal + 2 vertical connectors
    seen = set()
    for i in range(len(horiz)):
        x0_t, x1_t, y_t = horiz[i]
        for j in range(i + 1, len(horiz)):
            x0_b, x1_b, y_b = horiz[j]
            if y_b <= y_t:
                continue
            if (y_b - y_t) < min_h:
                continue
            # Common x-extent of the two horizontals
            x0 = max(x0_t, x0_b)
            x1 = min(x1_t, x1_b)
            if (x1 - x0) < min_w:
                continue
            # Need a vertical near each side connecting top→bottom
            left_match = right_match = None
            for vx, vy0, vy1 in vert:
                if abs(vy0 - y_t) > 12 or abs(vy1 - y_b) > 12:
                    continue
                if abs(vx - x0) < 18:
                    left_match = vx
                elif abs(vx - x1) < 18:
                    right_match = vx
            if left_match is not None and right_match is not None:
                key = (round(left_match), round(y_t),
                       round(right_match), round(y_b))
                if key in seen:
                    continue
                seen.add(key)
                if (right_match - left_match) * (y_b - y_t) > page_area * 0.9:
                    continue
                rects.append(DrawnRect(
                    x0=left_match, y0=y_t,
                    x1=right_match, y1=y_b,
                ))
    return rects


def overlap_area(a, b):
    """Intersection area of two bbox tuples (x0, y0, x1, y1)."""
    ix0 = max(a[0], b[0]); iy0 = max(a[1], b[1])
    ix1 = min(a[2], b[2]); iy1 = min(a[3], b[3])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def classify_display_ads(drawn_rects, body_spans):
    """A drawn rectangle is treated as a display ad if fewer than 10
    body-size spans fall inside it. Heuristic for MVP; refined later
    once we see how many false positives this generates.
    """
    ads = []
    for r in drawn_rects:
        inside = 0
        for s in body_spans:
            if (s.x0 >= r.x0 and s.x1 <= r.x1
                    and s.y0 >= r.y0 and s.y1 <= r.y1):
                inside += 1
                if inside >= 10:
                    break
        if inside < 10:
            ads.append(r)
    return ads


def detect_display_ads_classical(pdf_path, page_number, page_w_pt, page_h_pt):
    """Call the classical detect_ads() pass and convert its
    pct-based results to absolute-point DrawnRect objects in this
    page's coordinate system.

    The classical detector does adaptive thresholding + contour
    analysis on the rendered greyscale raster — exactly the signal
    we need for the post-1980 corpus, whose ad borders are baked
    into the scan raster rather than encoded as PDF vectors.

    Returns [] on any exception so an ad-detection failure doesn't
    break the whole page cut.
    """
    try:
        import sys, os
        repo = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from detect_ads import detect_ads as _classical_detect_ads
        raw = _classical_detect_ads(pdf_path, page_number=page_number,
                                    render_dpi=150)
    except Exception:
        return []
    rects = []
    for ad in raw or []:
        x0 = ad["x_pct"] / 100.0 * page_w_pt
        y0 = ad["y_pct"] / 100.0 * page_h_pt
        w  = ad["w_pct"] / 100.0 * page_w_pt
        h  = ad["h_pct"] / 100.0 * page_h_pt
        rects.append(DrawnRect(x0=x0, y0=y0, x1=x0 + w, y1=y0 + h))
    return rects


def find_visual_rectangles(page, masthead_bottom,
                           min_w_pt=100, min_h_pt=80,
                           dpi=150, dark_thr=130,
                           edge_fill_frac=0.55, edge_thick_pt=1.0):
    """Find rectangular frames in the rendered page raster below the
    masthead band. Adobe Paper Capture PDFs and 1985 TCPDF re-wraps
    flatten the scan into one full-page raster, so display-ad and
    photo borders live in the pixels, not in page.get_drawings().

    Algorithm:
      1. Render greyscale at `dpi`.
      2. For each row of pixels (below masthead), compute the fraction
         of "dark" pixels. Rows where that fraction exceeds
         `edge_fill_frac` *over a contiguous stretch* are candidate
         horizontal rules.
      3. Same for columns → vertical rules.
      4. For every pair of horizontal rules separated by ≥ min_h_pt,
         find vertical rules that connect them at left + right.
      5. Emit DrawnRect for each closed rectangle.

    Tuned permissively — false positives will be filtered downstream
    by the "few body spans inside" test in classify_display_ads.
    """
    import numpy as np
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    dark = img < dark_thr
    H, W = dark.shape

    start_row = max(0, int(round(masthead_bottom * zoom)))
    edge_thick_px = max(1, int(round(edge_thick_pt * zoom)))

    # Helper: in a 1-D mask, find runs of consecutive Trues
    def runs(mask, min_thick):
        i = 0
        n = len(mask)
        out = []
        while i < n:
            if mask[i]:
                j = i
                while j < n and mask[j]:
                    j += 1
                if (j - i) >= min_thick:
                    out.append((i, j - 1))
                i = j
            else:
                i += 1
        return out

    # Detect candidate horizontal rules: for each row, find the longest
    # contiguous dark stretch; if it covers >= edge_fill_frac of W, the
    # row is "ruled". Then runs of consecutive ruled rows (>= edge_thick_px
    # thick) are horizontal rules.
    def row_long_dark_frac(row_mask):
        # longest contiguous True
        max_len = cur = 0
        for v in row_mask:
            if v:
                cur += 1
                if cur > max_len:
                    max_len = cur
            else:
                cur = 0
        return max_len / max(W, 1)

    ruled_row = np.zeros(H, dtype=bool)
    for y in range(start_row, H):
        if row_long_dark_frac(dark[y]) >= edge_fill_frac:
            ruled_row[y] = True
    h_rules = runs(ruled_row, edge_thick_px)

    # Same idea for columns
    def col_long_dark_frac(col_mask):
        max_len = cur = 0
        for v in col_mask:
            if v:
                cur += 1
                if cur > max_len:
                    max_len = cur
            else:
                cur = 0
        return max_len / max(H - start_row, 1)

    ruled_col = np.zeros(W, dtype=bool)
    for x in range(W):
        col = dark[start_row:, x]
        if col_long_dark_frac(col) >= edge_fill_frac:
            ruled_col[x] = True
    v_rules = runs(ruled_col, edge_thick_px)

    # Convert to points and find rectangles
    h_rules_pt = [((s + e) / 2 / zoom, s / zoom, e / zoom) for s, e in h_rules]
    v_rules_pt = [((s + e) / 2 / zoom, s / zoom, e / zoom) for s, e in v_rules]

    # For each pair of horizontal rules (top, bottom), find vertical
    # rules that span from top to bottom at left + right edges
    rects = []
    seen = set()
    for i, (y_top, _, _) in enumerate(h_rules_pt):
        for j in range(i + 1, len(h_rules_pt)):
            y_bot = h_rules_pt[j][0]
            if y_bot - y_top < min_h_pt:
                continue
            # Find a vertical to left and to right of "most of the page"
            # whose y-range covers [y_top, y_bot]
            #
            # We need to determine the x-extent. Use the leftmost and
            # rightmost verticals whose y-range covers this gap.
            candidates = []
            for vx, _, _ in v_rules_pt:
                # vertical's y-range is the actual run; we approximated
                # to centre. Use the page-wide y-range instead.
                candidates.append(vx)
            if len(candidates) < 2:
                continue
            # For each pair of verticals at x=left, x=right with
            # (right - left) >= min_w_pt, check that the rectangle is
            # reasonable (not all of page, not zero area inside)
            cand_sorted = sorted(candidates)
            for li in range(len(cand_sorted)):
                left = cand_sorted[li]
                for ri in range(len(cand_sorted) - 1, li, -1):
                    right = cand_sorted[ri]
                    if (right - left) < min_w_pt:
                        break
                    key = (round(left), round(y_top),
                           round(right), round(y_bot))
                    if key in seen:
                        continue
                    seen.add(key)
                    rects.append(DrawnRect(
                        x0=left, y0=y_top, x1=right, y1=y_bot,
                    ))
                    # Largest rectangle anchored at this top/bottom is
                    # usually the most interesting; smaller subs are
                    # usually false positives.
                    break
    return rects
