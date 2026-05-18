"""Negative-space pass: find regions of the page that are not covered
by any detected article / ad / photo / pull-quote but contain visible
ink. Those are candidate missed regions worth re-running detection on
(or at minimum, surfacing to the user as "this area was not classified").

The 1995-02-15 p1 case is the canonical example — the Sirman lead's
body sits in the bottom-left of the page but the headline was missing
from the text layer, so no article block was built and the body was
left uncovered. This pass surfaces those orphan body regions.
"""
import fitz
import numpy as np


def _connected_components(mask):
    """Label connected True regions in a 2D boolean mask with
    4-connectivity. Returns (labels, count). No scipy dependency.
    """
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    n = 0
    for i in range(h):
        for j in range(w):
            if mask[i, j] and labels[i, j] == 0:
                n += 1
                stack = [(i, j)]
                while stack:
                    y, x = stack.pop()
                    if y < 0 or y >= h or x < 0 or x >= w:
                        continue
                    if not mask[y, x] or labels[y, x] != 0:
                        continue
                    labels[y, x] = n
                    stack.append((y + 1, x))
                    stack.append((y - 1, x))
                    stack.append((y, x + 1))
                    stack.append((y, x - 1))
    return labels, n


def find_uncovered_content(page, masthead_bottom, claimed_bboxes,
                           page_w, page_h,
                           grid_pt=20, dpi=100, dark_thr=130,
                           min_dark_frac=0.04, min_cells=12):
    """Return list of bboxes (x0, y0, x1, y1) for regions of the page
    below the masthead that:
      - are not inside any claimed rectangle (articles, ads, photos,
        pull-quotes — anything you pass in via claimed_bboxes), AND
      - have visible ink in the rendered raster.

    Resolution is set by grid_pt (default 20pt cells) — coarse enough
    to be fast, fine enough to localise within ~1cm.

    Components smaller than `min_cells` cells are discarded as noise.
    """
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    dark = img < dark_thr

    gw = int(page_w / grid_pt) + 1
    gh = int(page_h / grid_pt) + 1

    # Coverage grid: True = claimed by some detection or the masthead.
    covered = np.zeros((gh, gw), dtype=bool)
    mast_g = max(0, int(masthead_bottom / grid_pt))
    if mast_g > 0:
        covered[:mast_g + 1, :] = True
    for bbox in claimed_bboxes:
        x0, y0, x1, y1 = bbox
        gx0 = max(0, int(x0 / grid_pt))
        gy0 = max(0, int(y0 / grid_pt))
        gx1 = min(gw, int(x1 / grid_pt) + 1)
        gy1 = min(gh, int(y1 / grid_pt) + 1)
        covered[gy0:gy1, gx0:gx1] = True

    # Ink grid: True = cell has enough dark pixels to be content.
    has_ink = np.zeros((gh, gw), dtype=bool)
    cell_w_px = max(1, int(round(grid_pt * zoom)))
    cell_h_px = max(1, int(round(grid_pt * zoom)))
    for gy in range(gh):
        py0 = gy * cell_h_px
        py1 = min(py0 + cell_h_px, dark.shape[0])
        if py1 <= py0:
            continue
        for gx in range(gw):
            if covered[gy, gx]:
                continue
            px0 = gx * cell_w_px
            px1 = min(px0 + cell_w_px, dark.shape[1])
            if px1 <= px0:
                continue
            cell = dark[py0:py1, px0:px1]
            if cell.size == 0:
                continue
            if cell.mean() > min_dark_frac:
                has_ink[gy, gx] = True

    labels, n = _connected_components(has_ink)
    bboxes = []
    edge_band = 30.0    # pt — tall slivers in this margin band are dropped
    min_dim_pt = 60     # pt — drop if both width AND height are below this
    min_area_pt = 6000  # sq pt — drop if total area is below this
    for region_id in range(1, n + 1):
        ys, xs = np.where(labels == region_id)
        if len(ys) < min_cells:
            continue
        x0 = float(int(xs.min()) * grid_pt)
        y0 = float(int(ys.min()) * grid_pt)
        x1 = float((int(xs.max()) + 1) * grid_pt)
        y1 = float((int(ys.max()) + 1) * grid_pt)
        w = x1 - x0
        h = y1 - y0
        # Drop tall slivers near the page edges (these are usually
        # binding-edge artefacts or thin margin noise, not content).
        if w <= 40 and (x0 < edge_band or x1 > page_w - edge_band):
            continue
        # Drop tiny boxes anywhere on the page
        if w < min_dim_pt and h < min_dim_pt:
            continue
        if w * h < min_area_pt:
            continue
        bboxes.append((x0, y0, x1, y1))
    return bboxes
