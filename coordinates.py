"""
Centralised coordinate conversion for the MVTM pipeline.

Every detector in this pipeline expresses positions as percentages of
the **full PDF page** dimensions:

    Origin: top-left of the PDF page (0, 0)
    X axis: 0 % (left)  → 100 % (right)
    Y axis: 0 % (top)   → 100 % (bottom)

These helpers are the **single point of truth** for every pct ↔ px
conversion. All inline arithmetic of the form ``int(pct / 100 * w)``
or ``round(px / h * 100, n)`` has been replaced with calls into this
module, so behaviour stays consistent and changes happen in one place.

Why centralisation matters here
-------------------------------
The most common bug class in this pipeline has been **wrong-origin
errors**: feeding a percentage that's measured against one reference
(e.g. the full PDF page) into a conversion that uses a different
reference (e.g. a clipped strip's raster width, or the cropped column
image, or the PDF page in points instead of pixels). The bug looks
plausible at the call site — the names and the arithmetic both check
out — but the answer is wrong by the ratio of the two dimensions.

These helpers can't enforce that you pass the right `dim`, but funnelling
every conversion through them gives one canonical place to:
  - state the origin convention,
  - audit every callsite for the right reference,
  - flag exceptions explicitly.

Per-stage rules
---------------
- pct values are page-relative unless the variable name says otherwise
  (e.g. `local_pct`, `region_pct`).
- raster operations use the rendered image's `(h, w) = grey.shape` —
  that is the dimension you pass to ``pct_to_px`` to get a pixel index
  into the rendered raster.
- if you have a region (a column, a clipped strip, etc.) and want a
  position relative to that region, do the conversion in two steps with
  named intermediates — never inline the arithmetic.

Canonical precision
-------------------
- ``pct_to_px`` rounds to nearest integer.
- ``px_to_pct`` rounds to **2 decimals**. This is the single chosen
  precision for the whole pipeline; legacy 1-decimal outputs were
  standardised up to 2 decimals during the centralisation pass.
- ``pct_to_px_float`` is the escape hatch for chained arithmetic that
  needs sub-pixel precision before its final rounding.
"""


def pct_to_px(pct, dim):
    """Page percentage → integer pixel position.

    `dim` is the dimension of the image (raster) you want to map onto —
    typically ``w`` or ``h`` from ``grey.shape``. Rounds to nearest;
    the result is suitable as a slice index or a coordinate.
    """
    return round(pct / 100 * dim)


def pct_to_px_float(pct, dim):
    """Page percentage → float pixel position.

    Use this when the result feeds further arithmetic (area calcs,
    weighted thresholds, etc.) where rounding mid-computation would
    accumulate error. Round to int at the end with ``int(...)`` or
    ``round(...)`` as the use-site requires.
    """
    return pct / 100 * dim


def px_to_pct(px, dim):
    """Pixel position → page percentage, rounded to 2 decimals.

    `dim` is the same dimension you'd pass to ``pct_to_px`` — pass the
    raster width or height the px value was measured in.
    """
    return round(px / dim * 100, 2)


def pct_to_frac(pct):
    """Page percentage (0-100) → fraction (0.0-1.0)."""
    return pct / 100


def frac_to_pct(frac):
    """Fraction (0.0-1.0) → page percentage (0-100), rounded to 2 dp."""
    return round(frac * 100, 2)


def clamp_pct(pct, lo=0, hi=100):
    """Clamp a percentage to [lo, hi]. Defaults to valid page bounds."""
    return max(lo, min(hi, pct))


def clamp_px(px, dim):
    """Clamp a pixel position to [0, dim]."""
    return max(0, min(dim, px))
