"""
Shared CV pre-processing artefact for the MVTM detector pipeline.

`page_cv` produces a cleaned binary image and structural summaries that
are reused across detectors. Computing once per page and caching means
each detector consumes the artefact rather than re-running adaptive
threshold + connected-component filtering itself.

What it produces
----------------
- ``cleaned_binary`` (uint8 0/255, shape ``(H, W)``):
  adaptive threshold (MEAN_C, block 21, C 10) → CC filter ≥500 px →
  drop blobs matching shadow rule A or rule B → dilate 3×3.
- ``shadow_regions``: dropped CCs ``[(x, y, w, h, rule_id)]`` in pixels.
  ``rule_id`` is 'A' (page-edge mega-blob) or 'B' (bottom-band shadow).
- ``large_components``: surviving CCs ≥1% page area as
  ``[(x, y, w, h, area, fill)]`` in pixels — structural anchors for
  consumers (used as exclusion zones in some detectors).
- ``ink_projection_h``: per-column ink count, ``int32`` shape ``(W,)``.
  Primary signal for projection-derived text-area edges.
- ``ink_projection_v``: per-row ink count, ``int32`` shape ``(H,)``.

Pipeline reference
------------------
The validated steps were extracted from /tmp/closure_sweep_p8.py and
ratified against the Stage 0 ad bench on 1947-02-27 and 1947-11-06.

Public API
----------
    pcv = page_cv.compute_or_load(pdf_path, page_number=0,
                                  render_dpi=150, cache_dir=None)

When ``cache_dir`` is given:
- Cache is stored as ``page_cv.npz`` (binary + projections) plus
  ``page_cv.json`` (regions + metadata).
- Cache is reused if ``pipeline_version`` matches and the cache mtime
  is newer than the input PDF mtime.

When ``cache_dir`` is None, the result is computed fresh (no save).
"""

import json
import os
from dataclasses import dataclass, field

import cv2
import numpy as np

from pdf_utils import render_grey_uint8


PIPELINE_VERSION = 1

# Validated parameters (do not retune without bench evidence).
_BLOCK_SIZE = 21
_C = 10
_CC_AREA_MIN = 500
_DILATE_KERNEL = (3, 3)
_DILATE_ITERATIONS = 1
_LARGE_CC_AREA_FRAC = 0.01  # surface CCs occupying ≥1% page area


@dataclass
class PageCV:
    """In-memory bundle of one page's pre-processing artefact."""
    cleaned_binary: np.ndarray
    shadow_regions: list  # [(x, y, w, h, rule_id)]
    large_components: list  # [(x, y, w, h, area, fill)]
    ink_projection_h: np.ndarray  # shape (W,)
    ink_projection_v: np.ndarray  # shape (H,)
    width: int
    height: int
    pipeline_version: int = PIPELINE_VERSION
    source_meta: dict = field(default_factory=dict)


def _classify_components(st, W, H):
    """Vectorised classification of every CC.

    Returns a dict of boolean masks indexed by label:
      keep   — survives shadow filter (area ≥ min, not rule A, not rule B)
      ruleA  — page-edge mega-blob (bbox > 50% page AND fill < 0.05)
      ruleB  — bottom-band shadow (bottom > 90% AND width > 50% AND
               height < 10%)
      large  — area ≥ _LARGE_CC_AREA_FRAC of page AND not rule A
               (used for the structural-anchor surface)

    Label 0 is the background and is False in every mask.
    """
    area = st[:, cv2.CC_STAT_AREA]
    left = st[:, cv2.CC_STAT_LEFT]
    top  = st[:, cv2.CC_STAT_TOP]
    wid  = st[:, cv2.CC_STAT_WIDTH]
    hei  = st[:, cv2.CC_STAT_HEIGHT]
    bbox_area = (wid.astype(np.float64) * hei.astype(np.float64))
    fill = np.divide(area, np.maximum(bbox_area, 1),
                     out=np.zeros_like(bbox_area), where=bbox_area > 0)
    page_area = W * H

    ruleA = (bbox_area > 0.50 * page_area) & (fill < 0.05)
    ruleB = ((top + hei) > 0.90 * H) & (wid > 0.50 * W) & (hei < 0.10 * H)
    big_enough = area >= _CC_AREA_MIN
    keep = big_enough & ~ruleA & ~ruleB
    large = (area >= _LARGE_CC_AREA_FRAC * page_area) & ~ruleA

    # Background label is never in any output set.
    if len(area) > 0:
        keep[0] = False
        ruleA[0] = False
        ruleB[0] = False
        large[0] = False

    return {
        "keep": keep, "ruleA": ruleA, "ruleB": ruleB, "large": large,
        "left": left, "top": top, "wid": wid, "hei": hei,
        "area": area, "fill": fill,
    }


def _build_cleaned(lab, keep_mask):
    """Build the post-shadow-filter binary using a label LUT."""
    lut = np.where(keep_mask, np.uint8(255), np.uint8(0))
    return lut[lab]


def _regions_for(mask, classified, rule_id=None):
    """Materialise a list of bbox tuples for the labels selected by
    ``mask``. If ``rule_id`` is given, append it as the last tuple
    element (used for shadow_regions). Otherwise tuples carry
    (x, y, w, h, area, fill) — the large_components shape.
    """
    idx = np.flatnonzero(mask)
    out = []
    for L in idx:
        x = int(classified["left"][L]); y = int(classified["top"][L])
        w = int(classified["wid"][L]);  h = int(classified["hei"][L])
        if rule_id is not None:
            out.append((x, y, w, h, rule_id))
        else:
            a = int(classified["area"][L])
            f = float(classified["fill"][L])
            out.append((x, y, w, h, a, f))
    return out


def compute(grey: np.ndarray) -> PageCV:
    """Compute the page_cv artefact from a uint8 greyscale image.

    Args:
        grey: 2-D uint8 ndarray, shape (H, W). Use
              ``pdf_utils.render_grey_uint8`` to obtain it.

    Returns:
        PageCV bundle. Pure function — no I/O.
    """
    if grey.dtype != np.uint8:
        raise TypeError(
            f"page_cv.compute expects uint8 grey, got {grey.dtype}")
    if grey.ndim != 2:
        raise ValueError(
            f"page_cv.compute expects 2-D grey, got shape {grey.shape}")

    H, W = grey.shape

    binary = cv2.adaptiveThreshold(
        grey, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV, _BLOCK_SIZE, _C,
    )
    n_lab, lab, st, _centroids = cv2.connectedComponentsWithStats(binary, 8)

    classified = _classify_components(st, W, H)
    filtered = _build_cleaned(lab, classified["keep"])
    shadow_regions = (
        _regions_for(classified["ruleA"], classified, rule_id="A")
        + _regions_for(classified["ruleB"], classified, rule_id="B")
    )
    large = _regions_for(classified["large"], classified)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, _DILATE_KERNEL)
    cleaned = cv2.dilate(filtered, kernel, iterations=_DILATE_ITERATIONS)

    # Projections from the cleaned binary so shadow blobs / scan noise
    # don't pollute the signal.
    ink = (cleaned > 0).astype(np.int32)
    ink_projection_h = ink.sum(axis=0).astype(np.int32)  # shape (W,)
    ink_projection_v = ink.sum(axis=1).astype(np.int32)  # shape (H,)

    return PageCV(
        cleaned_binary=cleaned,
        shadow_regions=shadow_regions,
        large_components=large,
        ink_projection_h=ink_projection_h,
        ink_projection_v=ink_projection_v,
        width=W,
        height=H,
        pipeline_version=PIPELINE_VERSION,
    )


# ── Caching ─────────────────────────────────────────────────────────

_NPZ = "page_cv.npz"
_JSON = "page_cv.json"


def save(pcv: PageCV, cache_dir: str) -> None:
    """Persist a PageCV to ``{cache_dir}/page_cv.npz`` + ``page_cv.json``.

    Splits arrays (npz, compressed) from metadata (json) so the JSON
    sidecar is human-inspectable.
    """
    os.makedirs(cache_dir, exist_ok=True)
    np.savez_compressed(
        os.path.join(cache_dir, _NPZ),
        cleaned_binary=pcv.cleaned_binary,
        ink_projection_h=pcv.ink_projection_h,
        ink_projection_v=pcv.ink_projection_v,
    )
    meta = {
        "pipeline_version": pcv.pipeline_version,
        "width": pcv.width,
        "height": pcv.height,
        "shadow_regions": [
            {"x": x, "y": y, "w": w, "h": h, "rule": rule}
            for (x, y, w, h, rule) in pcv.shadow_regions
        ],
        "large_components": [
            {"x": x, "y": y, "w": w, "h": h, "area": a, "fill": f}
            for (x, y, w, h, a, f) in pcv.large_components
        ],
        "source_meta": pcv.source_meta,
    }
    with open(os.path.join(cache_dir, _JSON), "w") as fh:
        json.dump(meta, fh, indent=2)


def load(cache_dir: str) -> "PageCV | None":
    """Load a cached PageCV from ``cache_dir``. Returns None if missing
    or if the on-disk pipeline_version doesn't match the current module.
    """
    npz_path = os.path.join(cache_dir, _NPZ)
    json_path = os.path.join(cache_dir, _JSON)
    if not (os.path.exists(npz_path) and os.path.exists(json_path)):
        return None
    with open(json_path) as fh:
        meta = json.load(fh)
    if meta.get("pipeline_version") != PIPELINE_VERSION:
        return None
    arrays = np.load(npz_path)
    shadow_regions = [
        (r["x"], r["y"], r["w"], r["h"], r["rule"])
        for r in meta.get("shadow_regions", [])
    ]
    large_components = [
        (r["x"], r["y"], r["w"], r["h"], r["area"], r["fill"])
        for r in meta.get("large_components", [])
    ]
    return PageCV(
        cleaned_binary=arrays["cleaned_binary"],
        shadow_regions=shadow_regions,
        large_components=large_components,
        ink_projection_h=arrays["ink_projection_h"],
        ink_projection_v=arrays["ink_projection_v"],
        width=meta["width"],
        height=meta["height"],
        pipeline_version=meta["pipeline_version"],
        source_meta=meta.get("source_meta", {}),
    )


def _cache_is_fresh(cache_dir: str, source_path: str) -> bool:
    """True when both cache files exist AND the JSON is newer than the
    source. We compare against the JSON because both files are written
    in the same call — JSON last when ``save`` is normal — but they're
    written close enough that any stat ordering works.
    """
    json_path = os.path.join(cache_dir, _JSON)
    npz_path = os.path.join(cache_dir, _NPZ)
    if not (os.path.exists(json_path) and os.path.exists(npz_path)):
        return False
    try:
        cache_mtime = min(os.path.getmtime(json_path),
                          os.path.getmtime(npz_path))
        src_mtime = os.path.getmtime(source_path)
    except OSError:
        return False
    return cache_mtime >= src_mtime


def compute_or_load(pdf_path: str, page_number: int = 0,
                    render_dpi: int = 150,
                    cache_dir: "str | None" = None) -> PageCV:
    """Get a PageCV for ``pdf_path`` page ``page_number``.

    If ``cache_dir`` is provided and a fresh, version-matching cache
    exists there, it's returned without recomputing. Otherwise the page
    is rendered (via the P-shared ``render_grey_uint8`` cache), the
    artefact is computed, and — if ``cache_dir`` is set — saved.

    Cache freshness is decided by ``pipeline_version`` and by mtime
    against ``pdf_path``. Note: the plan originally proposed keying on
    ``page_raw.png`` mtime, but page_raw.png is generated *after*
    detect_ads runs, so it isn't available at the point page_cv is
    consumed. The PDF is the actual upstream input and is the right
    invalidation source.
    """
    if cache_dir is not None:
        cached = None
        if _cache_is_fresh(cache_dir, pdf_path):
            cached = load(cache_dir)
        if cached is not None:
            return cached

    grey = render_grey_uint8(pdf_path, page_number, render_dpi)
    pcv = compute(grey)
    pcv.source_meta = {
        "pdf_path": os.path.abspath(pdf_path),
        "page_number": page_number,
        "render_dpi": render_dpi,
    }

    if cache_dir is not None:
        save(pcv, cache_dir)

    return pcv
