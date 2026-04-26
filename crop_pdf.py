"""
Crop a PDF page to a rectangle specified in flexible units.

Unit system:
%    1% of the relevant dimension (x/w use page width, y/h use page height)
vw   1% of PDF page width (usable on any axis)
vh   1% of PDF page height (usable on any axis)
px   pixels at the specified dpi

    Prefix an integer to create block units, eg:
    10%  each unit = 10% of the dimension (our grid system)
    5vw  each unit = 5% of page width

    Block units (multiplier > 1) are 1-indexed for position:
        x=8, xunits="10%" → starts at (8-1)*10% = 70%
    Direct units (multiplier = 1) use the value directly:
        x=37.3, xunits="vw" → starts at 37.3% of page width

Usage:
    # Grid square (backward compatible)
    crop_pdf(pdf, 8, 6, 1, 1, "10%", "10%")

    # Column extraction with bufferzone
    crop_pdf(pdf, 37.3, 0, 13.2, 100, "vw", "vh")

    # Pixel-based crop at 450 dpi
    crop_pdf(pdf, 100, 200, 500, 800, "px", "px", dpi=450)

"""

import re
import fitz

from pdf_utils import open_clean_pdf as _open_clean

def _parse_unit(unit_str):
    """Parse a unit string into (multiplier, base_unit).

    '10%' → (10, '%')
    'vw'  → (1, 'vw')
    '%'   → (1, '%')
    '5vh' → (5, 'vh')
    'px'  → (1, 'px')
    """
    m = re.match(r"^(\d+)?(%-?|vw|vh|px)$", unit_str.strip())
    if not m:
        raise ValueError(f"Unrecognised unit: {unit_str!r}")
    multiplier = int(m.group(1)) if m.group(1) else 1
    base = m.group(2)
    if base == "%-":
        base = "%"
    return multiplier, base

def _to_points(value, unit_str, ref_width, ref_height, axis, is_position, dpi):
    """Convert a value with units to PDF points.

    axis: 'x' or 'y' — determines which dimension '%' refers to.
    is_position: if True and using block units, apply 1-indexing.
    """
    multiplier, base = _parse_unit(unit_str)

    # Determine reference dimension in points
    if base == "vw":
        ref = ref_width
    elif base == "vh":
        ref = ref_height
    elif base == "%":
        ref = ref_width if axis == "x" else ref_height
    elif base == "px":
        ref = None
    else:
        raise ValueError(f"Unknown base unit: {base!r}")

    if base == "px":
        return value * 72.0 / dpi

    if multiplier > 1:
        # Block mode
        if is_position:
            return (value - 1) * multiplier / 100.0 * ref
        else:
            return value * multiplier / 100.0 * ref
    else:
        # Direct mode
        return value / 100.0 * ref

def crop_pdf(
    pdf_path,
    x, y, w, h,
    xunits="10%", yunits="10%",
    page_number=0,
    dpi=450,
    output_path=None,
):
    """
    Crop a PDF page to a rectangle.

    x, y:          position (top-left corner)
    w, h:          size of the rectangle
    xunits:        units for x and w
    yunits:        units for y and h
    page_number:   zero-indexed page
    dpi:           render resolution
    output_path:   where to save (auto-generated if None)
    """
    doc = _open_clean(pdf_path)
    page = doc[page_number]
    pw, ph = page.rect.width, page.rect.height

    x0 = _to_points(x, xunits, pw, ph, "x", True, dpi)
    y0 = _to_points(y, yunits, pw, ph, "y", True, dpi)
    w0 = _to_points(w, xunits, pw, ph, "x", False, dpi)
    h0 = _to_points(h, yunits, pw, ph, "y", False, dpi)

    # Clamp to page boundaries
    x0 = max(0, min(x0, pw))
    y0 = max(0, min(y0, ph))
    x1 = max(0, min(x0 + w0, pw))
    y1 = max(0, min(y0 + h0, ph))

    clip = fitz.Rect(x0, y0, x1, y1)
    pix = page.get_pixmap(clip=clip, dpi=dpi)

    if output_path is None:
        stem = pdf_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        output_path = f"{stem}_crop.png"

    pix.save(output_path)
    doc.close()
    return output_path
