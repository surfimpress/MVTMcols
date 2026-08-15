"""EXPERIMENT — where should missing box rules come from?

Not part of the pipeline. Kept runnable so the numbers below can be
reproduced rather than taken on trust.

THE QUESTION
------------
`detect_boxes` finds a box from its printed rules, but some obvious boxes
have no complete rule set in Tesseract's output -- the CBO 920 ad on
1980-04-06 p5 and the I.D.A. ad on p6. Three candidate answers were
proposed: escalate to an LLM, tune Tesseract, or both.

RESULT 1 — TUNING TESSERACT DOES NOT WORK (measured)
-----------------------------------------------------
Re-ran Tesseract on p5 and p6 with five configurations:

    variant   thresholding        psm  tables    p5 seps  p6 seps
    base      Sauvola (=2)          3  on             12       45
    otsu      Otsu (=0)             3  on             12       45
    lepotsu   Leptonica Otsu (=1)   3  on             12       45
    psm1      Sauvola               1  on             12       45
    notab     Sauvola               3  OFF            12       45

**Separator output is byte-identical in count across every variant.**

This is NOT a broken experiment -- the control proves the variants really
applied. p6 hOCR output differs between them:

    variant   bytes    words   sha256[:8]
    base      374573    2270   ceaa49d9
    otsu      376395    2265   88537934     <- differs from base
    lepotsu   376395    2265   88537934
    psm1      374573    2270   ceaa49d9
    notab     382545    2267   74e2cf03     <- differs again

So the OCR text changes while the layout analysis does not. Tesseract
exposes no parameter that governs rule sensitivity -- `--print-parameters`
lists only debug/visualisation switches for `textord`. **Do not spend
more time trying to tune this.**

RESULT 2 — PIXEL-LEVEL RULE DETECTION FINDS WHAT TESSERACT MISSES
-------------------------------------------------------------------
A rule is THIN as well as long. Filtering for dark pixels that have no
dark neighbour `thin_px` above and below (a one-line morphological
opening) and then taking long runs finds, in the I.D.A. ad region of p6:

    y = 54.4%   x 53.3 - 90.9%
    y = 95.2%   x 52.7 - 90.7%

That is a matching top/bottom pair bounding the I.D.A. ad -- exactly the
box `detect_boxes` could not build because Tesseract never reported those
rules.

CAVEAT, stated plainly: this crude version finds FEWER rules overall than
Tesseract (4 vs 12 horizontal on p5, 23 vs 38 on p6) at these parameters.
It is a promising prototype, not a tuned detector, and it has not been
compared against Tesseract's rules page by page. Treat the I.D.A. result
as evidence the approach is worth building, not as a finished answer.

CONCLUSION
----------
The cheap classical route is not exhausted, so an LLM escalation is not
yet justified for this. Order of work if resumed: build the pixel rule
detector properly, measure it against Tesseract's separators corpus-wide,
and only escalate boxes that neither source supports.

Usage::

    python3 -m transcribe.scaled.experiments.rule_detection_sources
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# Downsample before looking for rules. A printed rule survives it and the
# work drops ~16x; at 300dpi/4 = 75dpi a hairline is still 1-2px.
SCALE = 4
# A rule must have no dark neighbour this far above AND below it. This is
# what separates a rule from a photo or a headline, both of which produce
# long dark runs too.
THIN_PX = 4
# Minimum run length as a fraction of page width/height.
MIN_FRAC = 0.05
DARK = 128


def find_rules(path: str, scale: int = SCALE, thin_px: int = THIN_PX,
               min_frac: float = MIN_FRAC):
    """(horizontal, vertical) rule segments in downsampled pixel coords."""
    im = Image.open(path).convert("L")
    im = im.resize((im.width // scale, im.height // scale), Image.LANCZOS)
    a = np.array(im)
    H, W = a.shape
    dark = a < DARK

    hthin = dark & ~np.roll(dark, thin_px, 0) & ~np.roll(dark, -thin_px, 0)
    vthin = dark & ~np.roll(dark, thin_px, 1) & ~np.roll(dark, -thin_px, 1)

    def long_runs(mask, axis, minlen):
        segs = []
        n = mask.shape[0] if axis == 0 else mask.shape[1]
        for i in range(n):
            line = mask[i, :] if axis == 0 else mask[:, i]
            idx = np.flatnonzero(
                np.diff(np.concatenate(([0], line.view(np.int8), [0]))))
            for s, e in zip(idx[::2], idx[1::2]):
                if e - s >= minlen:
                    segs.append((i, s, e))
        return segs

    return (long_runs(hthin, 0, int(W * min_frac)),
            long_runs(vthin, 1, int(H * min_frac)), W, H)


def merge(segs, tol=3, gap=8):
    """Collapse adjacent scanlines of one printed rule into one segment."""
    out = []
    for i, s, e in sorted(segs):
        hit = None
        for o in out:
            if abs(o[0] - i) <= tol and not (e < o[1] - gap or s > o[2] + gap):
                hit = o
                break
        if hit:
            hit[0], hit[1], hit[2] = i, min(hit[1], s), max(hit[2], e)
        else:
            out.append([i, s, e])
    return out


def main():
    for pg in (5, 6):
        path = f"transcribe/work/ocr_llm/1980-04-06/p{pg}/page_full.png"
        hs, vs, W, H = find_rules(path)
        mh, mv = merge(hs), merge(vs)
        print(f"p{pg}: {len(mh)} horizontal, {len(mv)} vertical pixel rules")
        if pg == 6:
            print("   candidates bounding the I.D.A. ad (Tesseract has none):")
            for i, s, e in sorted(mh):
                y, x0, x1 = i / H * 100, s / W * 100, e / W * 100
                if 50 < y < 96 and x0 > 45 and x1 > 80:
                    print(f"     y={y:5.1f}%  x {x0:5.1f}-{x1:5.1f}%")


if __name__ == "__main__":
    raise SystemExit(main())
