"""
Centralised coordinate conversion for the MVTM pipeline.

All positions in the pipeline are expressed as percentages of the
full PDF page dimensions. This is the universal coordinate system:
  - Origin: top-left of the PDF page (0, 0)
  - X axis: 0% (left) to 100% (right) of PDF page width
  - Y axis: 0% (top) to 100% (bottom) of PDF page height

These functions convert between page percentages and pixel positions
at any render DPI. Using them consistently prevents the rounding
drift and truncation errors that arise from inline arithmetic.
"""


def pct_to_px(pct, dimension):
    """Page percentage → pixel position. Rounds to nearest integer."""
    return round(pct / 100 * dimension)


def px_to_pct(px, dimension):
    """Pixel position → page percentage. 2 decimal places."""
    return round(px / dimension * 100, 2)


def pct_to_frac(pct):
    """Page percentage (0-100) → fraction (0.0-1.0)."""
    return pct / 100


def frac_to_pct(frac):
    """Fraction (0.0-1.0) → page percentage (0-100). 2 decimal places."""
    return round(frac * 100, 2)


def clamp_pct(pct, lo=0, hi=100):
    """Clamp a percentage to valid page bounds."""
    return max(lo, min(hi, pct))
