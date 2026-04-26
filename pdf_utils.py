"""Shared low-level PDF helpers.

Single home for utilities that were previously copy-pasted across the
detector modules. Body verified byte-identical against all five prior
copies (find_columns, detect_ads, detect_sliver, page_profile,
crop_pdf) before consolidation.
"""

import cv2
import fitz
import numpy as np


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

    Args:
        pdf_path: PDF on disk.
        page_number: zero-indexed page.
        dpi: render DPI (caller chooses per-stage; see DPI conventions).
        clip: optional sub-region — either a `fitz.Rect` or a 4-tuple
              `(x0, y0, x1, y1)` of fractions in [0, 1] of the page rect.

    Returns:
        2-D `numpy.ndarray` of dtype float64, shape (h, w).
    """
    pix, doc = _open_and_pixmap(pdf_path, page_number, dpi, clip)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = np.mean(img, axis=2)
    else:
        grey = img.reshape(pix.h, pix.w).astype(float)
    doc.close()
    return grey


def render_grey_uint8(pdf_path, page_number, dpi, clip=None):
    """Render a page as a 2-D uint8 greyscale array.

    Same overlay-stripping as `render_grey`, but uses ITU-R 601 luma
    (`cv2.cvtColor(..., COLOR_RGB2GRAY)` — `0.299R + 0.587G + 0.114B`)
    and keeps the result as uint8 so it can feed `cv2.threshold` and
    `cv2.adaptiveThreshold` directly without a copy.

    For darkness-profile work that subtracts from 255.0, use
    `render_grey` (float64) instead.

    Args:
        pdf_path: PDF on disk.
        page_number: zero-indexed page.
        dpi: render DPI.
        clip: optional sub-region — either a `fitz.Rect` or a 4-tuple
              `(x0, y0, x1, y1)` of fractions in [0, 1] of the page rect.

    Returns:
        2-D `numpy.ndarray` of dtype uint8, shape (h, w).
    """
    pix, doc = _open_and_pixmap(pdf_path, page_number, dpi, clip)
    img = np.frombuffer(pix.samples, dtype=np.uint8)
    if pix.n >= 3:
        img = img.reshape(pix.h, pix.w, pix.n)[:, :, :3]
        grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        grey = img.reshape(pix.h, pix.w)
    doc.close()
    return grey
