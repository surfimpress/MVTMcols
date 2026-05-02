"""Shared low-level PDF helpers.

Single home for utilities that were previously copy-pasted across the
detector modules. Body verified byte-identical against all five prior
copies (find_columns, detect_ads, detect_sliver, page_profile,
crop_pdf) before consolidation.

Module-level full-page render cache (P-shared Tier 1):
    render_grey / render_grey_uint8 transparently share a single
    full-page rasterisation per (pdf_path, mtime, page, dpi) key. Earlier
    each call did its own open_clean_pdf + get_pixmap, so the same page
    was rasterised 9× by detect_strips alone. With the cache the first
    call renders the full page; subsequent calls slice the cached array
    in numpy, no re-rasterisation.

    The cache holds up to `_RENDER_CACHE_MAXSIZE` (currently 12) full-page
    entries. Sized to fit a whole issue's pages so the phase-then-page
    pipeline structure (detect_ads → profile → Pass 1 → Pass 2 → headlines
    → body-text → overlays, each a per-page loop) never re-renders a page
    that was already canonical-rendered in an earlier phase. With the old
    size of 2, profiling showed `_native_render` running 24× for an
    8-page issue (3 phase visits × 8 pages with constant eviction); at
    12 the same issue runs it 8× — once per page. Use `clear_render_cache()`
    to drop everything (e.g. between issues) when memory matters.
"""

import os
from collections import OrderedDict

import cv2
import fitz
import numpy as np


# ── Full-page render cache ───────────────────────────────────────────
#
# Keyed by (pdf_path, mtime, page_number, dpi). Value is a dict with:
#   'pix'      — fitz.Pixmap (canonical entry: from MuPDF; derived
#                entry: built from downsampled samples)
#   'doc'      — the fitz.Document the pixmap came from (canonical
#                only; derived entries set this to None)
#   'rgb'      — full-page RGB uint8 numpy array (h, w, 3), lazy
#   'grey_f64' — RGB-mean float64 grey (for render_grey), lazy
#   'grey_u8'  — ITU-R 601 luma uint8 grey (for render_grey_uint8), lazy
#
# Tier 2 (P-shared): when a non-canonical DPI is requested, the cache
# eagerly ensures a canonical-DPI entry first and downsamples its RGB
# array via cv2.INTER_AREA to populate the lower-DPI entry. Saves the
# native MuPDF render at 75/150/300 DPI when the page also needs the
# canonical 450-DPI render (true for every page in process_issue).
#
# Memory: a 1947-era page at 450 DPI is ~5400×7000 px, so RGB is
# ~113 MB and each grey is ~37–75 MB. Two slots × (canonical + a few
# derived) peaks around 600 MB — acceptable per the user's "memory is
# not constrained" directive.

CANONICAL_DPI = 450
_RENDER_CACHE_MAXSIZE = 12
_RENDER_CACHE = OrderedDict()


def _cache_key(pdf_path, page_number, dpi):
    try:
        mtime = os.path.getmtime(pdf_path)
    except OSError:
        mtime = 0
    return (os.path.abspath(pdf_path), mtime, page_number, dpi)


def _try_embedded_bitmap_render(doc, page, dpi):
    """If the page contains a single bilevel (1-bit) embedded image — the
    typical case for the corpus's JBIG2-encoded scans — decode that
    bitmap directly and resample to the requested DPI, bypassing
    `page.get_pixmap`. Returns a cache-entry dict on success, or None
    if the page doesn't fit the fast path.

    Gated by env var `MVTM_USE_EMBEDDED_BITMAP=1` for the duration of
    the 2026-05 experiment. When the flag is unset, this returns None
    and the caller falls back to the historical fitz render path.
    """
    if os.environ.get("MVTM_USE_EMBEDDED_BITMAP") != "1":
        return None
    try:
        imgs = page.get_images(full=True)
        if len(imgs) != 1:
            return None
        xref = imgs[0][0]
        info = doc.extract_image(xref)
        if info.get("bpc") != 1:
            return None
        # Decode to numpy. PyMuPDF transcodes JBIG2 to PNG-of-mode-L
        # but only emits 0/255 values (verified on 1922-06-30 — zero
        # intermediate pixels), so the data is bilevel even though the
        # container is 8-bit greyscale.
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(info["image"]))
        native = np.asarray(im.convert("L"))  # (H, W) uint8

        # Resample to the DPI the cache contract expects. Page rect is
        # in PDF points; (page_pts * dpi / 72) gives the pixel count
        # MuPDF would produce at this DPI for a full-page render.
        target_w = int(round(page.rect.width * dpi / 72.0))
        target_h = int(round(page.rect.height * dpi / 72.0))
        if (native.shape[1], native.shape[0]) == (target_w, target_h):
            grey_u8 = native
        else:
            grey_u8 = cv2.resize(
                native, (target_w, target_h),
                interpolation=cv2.INTER_AREA)

        # Slim path: cache only `grey_u8`. RGB triple-stack and the
        # fitz.Pixmap are built lazily in `_entry_rgb` / `_entry_pix` if
        # a consumer actually needs them. For detector reads (which go
        # via `render_grey` / `render_grey_uint8` → `_entry_grey_f64` /
        # `_entry_grey_u8`) the bilevel uint8 array is the destination
        # format, so most pages never trigger RGB allocation at all.
        return {
            "pix": None,
            "doc": doc,
            "page_w_pts": page.rect.width,
            "page_h_pts": page.rect.height,
            "rgb": None,
            "grey_f64": None,
            "grey_u8": grey_u8,
        }
    except Exception:
        # Any decode failure → fall back to the fitz path. We don't
        # want a malformed embedded image to brick the pipeline.
        return None


def _native_render(pdf_path, page_number, dpi):
    """Open the PDF clean and render the full page at `dpi`. Returns a
    new cache entry — caller is responsible for storing it."""
    doc = open_clean_pdf(pdf_path)
    page = doc[page_number]
    fast = _try_embedded_bitmap_render(doc, page, dpi)
    if fast is not None:
        return fast
    pix = page.get_pixmap(dpi=dpi)
    return {
        "pix": pix,
        "doc": doc,
        "page_w_pts": page.rect.width,
        "page_h_pts": page.rect.height,
        "rgb": None,
        "grey_f64": None,
        "grey_u8": None,
    }


def _derive_entry(canon, target_dpi):
    """Build a lower-DPI cache entry by downsampling the canonical
    entry. Slim path: when canonical was rendered via the bitmap fast
    path (no RGB / no fitz.Pixmap), downsample the bilevel `grey_u8`
    array directly and leave RGB / Pixmap lazy in the derived entry.
    Legacy path: canonical has RGB → downsample RGB and pre-build the
    derived fitz.Pixmap (preserves byte-identical Pixmap behaviour for
    the existing `pix.save` consumers).
    """
    # Target pixel dims at requested DPI, mirroring MuPDF's full-page
    # rounding (which is `round(page_pts * dpi / 72)` for full pages).
    target_w = int(round(canon["page_w_pts"] * target_dpi / 72.0))
    target_h = int(round(canon["page_h_pts"] * target_dpi / 72.0))

    if canon.get("rgb") is None and canon.get("grey_u8") is not None:
        # Slim path: no RGB triple-stack, no derived fitz.Pixmap until
        # something demands one.
        canon_grey = canon["grey_u8"]
        derived_grey = cv2.resize(
            canon_grey, (target_w, target_h),
            interpolation=cv2.INTER_AREA)
        return {
            "pix": None,
            "doc": None,
            "page_w_pts": canon["page_w_pts"],
            "page_h_pts": canon["page_h_pts"],
            "rgb": None,
            "grey_f64": None,
            "grey_u8": derived_grey,
        }

    # Legacy path: canonical was rendered via fitz get_pixmap → has
    # RGB samples. Downsample RGB and pre-build the derived Pixmap so
    # `pix.save` consumers see byte-identical behaviour.
    canon_rgb = _entry_rgb(canon)
    derived_rgb = cv2.resize(
        canon_rgb, (target_w, target_h), interpolation=cv2.INTER_AREA)
    derived_pix = fitz.Pixmap(
        fitz.csRGB, target_w, target_h,
        np.ascontiguousarray(derived_rgb).tobytes(), 0)
    return {
        "pix": derived_pix,
        "doc": None,
        "page_w_pts": canon["page_w_pts"],
        "page_h_pts": canon["page_h_pts"],
        "rgb": derived_rgb,
        "grey_f64": None,
        "grey_u8": None,
    }


def _ensure_full_render(pdf_path, page_number, dpi):
    """Return the cache entry for (path, page, dpi). On miss:
    - dpi == CANONICAL_DPI or higher: render natively via MuPDF.
    - dpi <  CANONICAL_DPI: ensure the canonical entry first, then
      derive this DPI by downsampling.
    """
    key = _cache_key(pdf_path, page_number, dpi)
    entry = _RENDER_CACHE.get(key)
    if entry is not None:
        _RENDER_CACHE.move_to_end(key)
        return entry

    if dpi < CANONICAL_DPI:
        canon = _ensure_full_render(pdf_path, page_number, CANONICAL_DPI)
        entry = _derive_entry(canon, dpi)
    else:
        entry = _native_render(pdf_path, page_number, dpi)

    _RENDER_CACHE[key] = entry

    while len(_RENDER_CACHE) > _RENDER_CACHE_MAXSIZE:
        _, evicted = _RENDER_CACHE.popitem(last=False)
        evicted_doc = evicted.get("doc")
        if evicted_doc is not None:
            try:
                evicted_doc.close()
            except Exception:
                pass

    return entry


def _entry_dims(entry):
    """Return (h, w) of the cached page in pixels without forcing RGB
    or Pixmap materialisation. Slim-path entries (bitmap source) have
    only `grey_u8`; legacy entries have a fitz.Pixmap from MuPDF."""
    if entry.get("pix") is not None:
        return entry["pix"].h, entry["pix"].w
    if entry.get("grey_u8") is not None:
        return entry["grey_u8"].shape
    if entry.get("rgb") is not None:
        return entry["rgb"].shape[0], entry["rgb"].shape[1]
    raise RuntimeError("cache entry has no pixel source")


def _entry_pix(entry):
    """Lazy: return a fitz.Pixmap for the cached entry. Slim-path
    entries build the Pixmap from the RGB triple-stack on first call;
    that triple-stack is itself lazy via `_entry_rgb`. The Pixmap is
    cached on the entry so subsequent calls reuse it."""
    if entry.get("pix") is None:
        rgb = _entry_rgb(entry)
        h, w, _ = rgb.shape
        entry["pix"] = fitz.Pixmap(
            fitz.csRGB, w, h,
            np.ascontiguousarray(rgb).tobytes(), 0)
    return entry["pix"]


def _entry_rgb(entry):
    if entry["rgb"] is None:
        if entry.get("grey_u8") is not None:
            # Slim-path: triple-stack the bilevel uint8 only when a
            # consumer actually demands RGB.
            grey = entry["grey_u8"]
            entry["rgb"] = np.stack([grey, grey, grey], axis=-1)
        elif entry.get("pix") is not None:
            pix = entry["pix"]
            arr = np.frombuffer(pix.samples, dtype=np.uint8)
            if pix.n >= 3:
                entry["rgb"] = arr.reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()
            else:
                grey = arr.reshape(pix.h, pix.w)
                entry["rgb"] = np.stack([grey, grey, grey], axis=-1)
        else:
            raise RuntimeError("cache entry has no pixel source")
    return entry["rgb"]


def _entry_grey_f64(entry):
    if entry["grey_f64"] is None:
        if entry.get("grey_u8") is not None:
            # Slim-path: cast the bilevel uint8 directly to float64.
            # Equivalent to np.mean(triple_stack(grey_u8), axis=2)
            # because all three channels are identical.
            entry["grey_f64"] = entry["grey_u8"].astype(np.float64)
        else:
            entry["grey_f64"] = np.mean(_entry_rgb(entry), axis=2)
    return entry["grey_f64"]


def _entry_grey_u8(entry):
    if entry["grey_u8"] is None:
        entry["grey_u8"] = cv2.cvtColor(_entry_rgb(entry), cv2.COLOR_RGB2GRAY)
    return entry["grey_u8"]


def _slice_indices(clip, page_w_pts, page_h_pts, full_w, full_h):
    """Map a clip (None | tuple of fractions | fitz.Rect) to (x0,y0,x1,y1)
    pixel indices in the full-page array.

    Mirrors MuPDF's `fz_round_rect` semantics — floor on x0/y0, ceil on
    x1/y1 — so a slice from the cached full-page pixmap is byte-identical
    to what `page.get_pixmap(clip=...)` would have produced. Verified
    against the direct-clip path on 1947-11-06: zero pixels differ.
    """
    import math
    if clip is None:
        return 0, 0, full_w, full_h
    if isinstance(clip, tuple):
        x0_frac, y0_frac, x1_frac, y1_frac = clip
        x0_pts = x0_frac * page_w_pts
        y0_pts = y0_frac * page_h_pts
        x1_pts = x1_frac * page_w_pts
        y1_pts = y1_frac * page_h_pts
    else:  # fitz.Rect
        x0_pts, y0_pts = clip.x0, clip.y0
        x1_pts, y1_pts = clip.x1, clip.y1

    sx = full_w / page_w_pts
    sy = full_h / page_h_pts
    x0 = max(0, math.floor(x0_pts * sx))
    y0 = max(0, math.floor(y0_pts * sy))
    x1 = min(full_w, math.ceil(x1_pts * sx))
    y1 = min(full_h, math.ceil(y1_pts * sy))
    return x0, y0, x1, y1


def get_page_size_pts(pdf_path, page_number, dpi):
    """Return (width, height) of the page in PDF points.

    Uses the cache entry's stored dimensions, populating it (via a full
    render) if needed. Callers that just need page dimensions and have
    not yet rasterised should prefer opening the PDF directly — this
    helper exists for paths that are about to render anyway.
    """
    entry = _ensure_full_render(pdf_path, page_number, dpi)
    return entry["page_w_pts"], entry["page_h_pts"]


def get_full_pixmap(pdf_path, page_number, dpi):
    """Return the cached full-page MuPDF Pixmap (rendering on first call).

    Callers that need a MuPDF Pixmap (e.g. to take a sub-region via
    `get_clip_pixmap` or to inspect width/height) should use this
    instead of opening the PDF and rendering themselves. The pixmap is
    shared with the grey-array path — one render serves all consumers.

    For slim-path entries (1-bit bitmap source) the Pixmap is built
    lazily on first call from the cached `grey_u8` array.
    """
    entry = _ensure_full_render(pdf_path, page_number, dpi)
    return _entry_pix(entry)


def get_clip_pixmap(pdf_path, page_number, dpi, clip):
    """Return a MuPDF Pixmap for a clip region, sliced from the cached
    full-page render.

    Pixel content is byte-identical to what `page.get_pixmap(clip=...,
    dpi=dpi)` would return ONLY when a full-page render at the same DPI
    is performed first; with the cache that ordering is guaranteed.
    Without the cache, MuPDF's clip render would produce slightly
    different anti-aliasing at glyph edges (validated on 1947-11-06:
    ~20% of pixels differ by up to 207 grey-levels at glyph contours,
    no positional drift). For darkness-profile detectors that's
    invisible; for human-facing column/ad PNGs it's an
    indistinguishable rendering of the same page.

    The returned pixmap's `pix.x` / `pix.y` carry the irect origin so
    downstream coordinate maths still works.

    Args:
        pdf_path: PDF on disk.
        page_number: zero-indexed page.
        dpi: render DPI.
        clip: a `fitz.Rect` in PDF points, or a 4-tuple of fractions.

    Returns:
        `fitz.Pixmap`. Caller owns it; safe to `.save(...)` directly.
    """
    entry = _ensure_full_render(pdf_path, page_number, dpi)
    full_h, full_w = _entry_dims(entry)
    x0, y0, x1, y1 = _slice_indices(
        clip, entry["page_w_pts"], entry["page_h_pts"], full_w, full_h)
    rgb = _entry_rgb(entry)
    sub = np.ascontiguousarray(rgb[y0:y1, x0:x1])
    sh, sw, sn = sub.shape
    cs = fitz.csRGB if sn == 3 else fitz.csGRAY
    pix = fitz.Pixmap(cs, sw, sh, sub.tobytes(), 0)
    # Preserve the irect origin so callers can map back to page coords.
    pix.set_origin(x0, y0)
    return pix


def try_embedded_bitmap_pil(pdf_path, page_number):
    """If the page's source is a single bilevel (1-bit) embedded image
    — the typical case for the corpus's JBIG2-encoded scans — decode
    that bitmap and return it as a PIL Image at native resolution,
    in mode='1' (PIL bilevel). Otherwise return None.

    Auto-enabled per page: callers always invoke this helper, and it
    short-circuits to None for pages whose source isn't a single 1-bit
    embedded image (so legacy paths run unchanged for those). For
    pages that pass the format guard, callers can write the returned
    image to disk; mode='1' PNGs preserve the source bilevel encoding
    and are smaller than the equivalent RGB / mode=L re-rendered
    raster.

    Validated on 1923-06-22 and 1946-01-03. The off switch
    `MVTM_USE_EMBEDDED_BITMAP=0` forces the legacy path even for
    eligible pages, kept available as an escape hatch.
    """
    if os.environ.get("MVTM_USE_EMBEDDED_BITMAP") == "0":
        return None
    try:
        doc = open_clean_pdf(pdf_path)
        try:
            page = doc[page_number]
            imgs = page.get_images(full=True)
            if len(imgs) != 1:
                return None
            xref = imgs[0][0]
            info = doc.extract_image(xref)
            if info.get("bpc") != 1:
                return None
            import io
            from PIL import Image
            # PyMuPDF transcodes JBIG2 to a mode=L PNG with only
            # {0, 255} values — verified empirically. Threshold at 128
            # to recover the strict bilevel image (mode='1') without
            # introducing intermediate greys.
            im = Image.open(io.BytesIO(info["image"]))
            if im.mode != "1":
                im = im.convert("L").point(lambda v: 255 if v >= 128 else 0, mode="1")
            return im
        finally:
            doc.close()
    except Exception:
        return None


def clear_render_cache():
    """Drop every cached page render. Call between issues if memory matters."""
    for entry in _RENDER_CACHE.values():
        try:
            entry["doc"].close()
        except Exception:
            pass
    _RENDER_CACHE.clear()


def open_clean_pdf(pdf_path):
    """Open a PDF and strip red overlay lines from all pages.

    Heritage scans in this corpus may carry red rule overlays from a
    previous annotation pass (PDF content stream `1 0 0 RG`). Those
    rules contaminate darkness profiles and contour detection, so
    every detector wants the document with them blanked out before
    rendering. This helper does the strip and returns the in-memory
    fitz.Document — caller is responsible for `.close()`.
    """
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc


def _open_and_pixmap(pdf_path, page_number, dpi, clip):
    """Internal: open the doc clean, return (pix, doc) for either render
    helper. `clip` may be None (full page), a `fitz.Rect`, or a 4-tuple
    `(x0, y0, x1, y1)` of fractions in [0, 1] of the page's PDF rect.
    The fractional form lets callers describe a sub-region without
    needing to read `page.rect` themselves.
    """
    doc = open_clean_pdf(pdf_path)
    page = doc[page_number]
    if clip is None:
        pix = page.get_pixmap(dpi=dpi)
    elif isinstance(clip, tuple):
        x0_frac, y0_frac, x1_frac, y1_frac = clip
        pw, ph = page.rect.width, page.rect.height
        rect = fitz.Rect(pw * x0_frac, ph * y0_frac,
                         pw * x1_frac, ph * y1_frac)
        pix = page.get_pixmap(clip=rect, dpi=dpi)
    else:
        pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix, doc


def render_grey(pdf_path, page_number, dpi, clip=None):
    """Render a page as a 2-D float64 greyscale array.

    Uses `open_clean_pdf` so red overlays are stripped, then collapses
    RGB(A) to greyscale via unweighted channel mean (`np.mean(axis=2)`),
    matching the historical body of every detector that does darkness-
    profile work. Float64 because callers compute `255.0 - grey` and
    take projection means; uint8 would silently wrap.

    For uint8 output (`cv2.threshold` / `cv2.adaptiveThreshold` paths),
    use `render_grey_uint8` instead — the dtype split is functional, not
    cosmetic.

    P-shared: when called repeatedly for the same (path, page, dpi)
    with different clip regions, the full-page render is shared across
    calls via the module-level cache. A new clip is just a numpy slice.

    Args:
        pdf_path: PDF on disk.
        page_number: zero-indexed page.
        dpi: render DPI (caller chooses per-stage; see DPI conventions).
        clip: optional sub-region — either a `fitz.Rect` or a 4-tuple
              `(x0, y0, x1, y1)` of fractions in [0, 1] of the page rect.

    Returns:
        2-D `numpy.ndarray` of dtype float64, shape (h, w).
    """
    entry = _ensure_full_render(pdf_path, page_number, dpi)
    grey = _entry_grey_f64(entry)
    full_h, full_w = grey.shape
    x0, y0, x1, y1 = _slice_indices(
        clip, entry["page_w_pts"], entry["page_h_pts"], full_w, full_h)
    return grey[y0:y1, x0:x1]


def render_grey_uint8(pdf_path, page_number, dpi, clip=None):
    """Render a page as a 2-D uint8 greyscale array.

    Same overlay-stripping as `render_grey`, but uses ITU-R 601 luma
    (`cv2.cvtColor(..., COLOR_RGB2GRAY)` — `0.299R + 0.587G + 0.114B`)
    and keeps the result as uint8 so it can feed `cv2.threshold` and
    `cv2.adaptiveThreshold` directly without a copy.

    For darkness-profile work that subtracts from 255.0, use
    `render_grey` (float64) instead.

    P-shared: shares the full-page render with `render_grey` via the
    module-level cache; a clipped call is a numpy slice.

    Args:
        pdf_path: PDF on disk.
        page_number: zero-indexed page.
        dpi: render DPI.
        clip: optional sub-region — either a `fitz.Rect` or a 4-tuple
              `(x0, y0, x1, y1)` of fractions in [0, 1] of the page rect.

    Returns:
        2-D `numpy.ndarray` of dtype uint8, shape (h, w).
    """
    entry = _ensure_full_render(pdf_path, page_number, dpi)
    grey = _entry_grey_u8(entry)
    full_h, full_w = grey.shape
    x0, y0, x1, y1 = _slice_indices(
        clip, entry["page_w_pts"], entry["page_h_pts"], full_w, full_h)
    return grey[y0:y1, x0:x1]
