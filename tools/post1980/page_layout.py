"""Page-level layout: masthead-band detection + page classification.

Masthead band: the strip at the top of the page that's furniture, not
content. On a front page this is the gazette title + date strip + any
teaser strip above. On an interior page it's the section banner
("ALMONTE AND DISTRICT", "DISTRICT NEWS", etc.). The band's bottom y
is the "where do articles start" line.

Masthead detection has two strategies:
 1. **Rule-based** — render the page in greyscale, find the
    bottom-most thick horizontal rule that spans most of the page
    width in the top 45%. Newspapers conventionally print such a
    rule below the masthead / date strip. Primary strategy.
 2. **Text-based** — find the largest text element in the top 30%
    and use its bottom edge plus a small slack for date-strip
    furniture. Fallback when no rule is found (e.g. some interior
    pages with only a text banner).

Page classification: which cutter should process this page. See the
plan for the full taxonomy; this MVP returns one of:

    'modular'          — modular broadsheet (the typical case)
    'classifieds'      — dense narrow-column listings
    'image_only'       — mostly graphic, very little text
    'looks_classical'  — uniform page-wide column grid (unlikely in
                         post-1980 but kept as an escape hatch)
"""
import fitz
import numpy as np

from .spans import body_font_size


# Rule-detection knobs, chosen from empirical probing of 2000/2007/1995/1985 fronts:
RULE_DPI = 150            # render DPI for the probe; 150 catches 2pt+ rules clearly
RULE_DARK_THR = 130       # greyscale value < this counts as "ink"
# Per-page-type fill thresholds. Front-page masthead rules span the
# full page width by convention; interior section-banner rules can
# be interrupted by a corner ad (as on 2007-02-13 p3 where the
# Gazette ad in the top-right cuts the rule to ~60% of page width).
RULE_FILL_FRAC_FRONT    = 0.85   # page 1: rule must be close to full-width
RULE_FILL_FRAC_INTERIOR = 0.55   # page 2+: rule can be ~60% wide;
                                 # 2007-02-13 p3's DISTRICT NEWS rule
                                 # under the Gazette ad in the top-right
                                 # measures 0.57–0.59 fill at its peak.

# Minimum and maximum rule thickness as fraction of *page height*.
# Page-height percentage scales naturally between A4 (1985 TCPDF
# re-wraps, ~842pt) and broadsheet (1990–2007 Adobe Paper Capture,
# ~2120pt). Real rules in the corpus span 0.07% (2007 p3's thin rule
# under DISTRICT NEWS) up to 0.68% (1990 p1's decorative double-rule).
# Photo regions register at 2.4%+ — well above any real rule.
# With the peakiness check below acting as the noise filter, we can
# afford a loose min — 0.05% (clamped to ≥ 2px in practice).
RULE_MIN_THICKNESS_FRAC = 0.0005   # ~0.05%; clamped to 2px minimum
RULE_MAX_THICKNESS_FRAC = 0.0140   # ~1.4%

# Search zone differs by page number. Front pages: full masthead +
# teaser strip + date row. Interior pages: just a section banner.
RULE_SEARCH_TOP_FRAC_FRONT    = 0.30  # page 1
RULE_SEARCH_TOP_FRAC_INTERIOR = 0.15  # page 2+

# Ignore the topmost ~30pt — that's the page-edge frame line which
# always passes the rule test but isn't a useful masthead boundary.
RULE_MIN_Y_PT = 30.0

# Peakiness threshold: a real rule's row-fill must be much higher
# than the average fill of the surrounding 20px. Real rules in the
# corpus measure 4.7×–29.5×; ad/photo-content artefacts measure
# 1.0×–1.6× because they sit inside continuous dense content.
RULE_PEAKINESS_THR = 2.5    # ad-content artefacts measure ~1.5×;
                            # real rules ≥ 2.88× in the corpus.
RULE_PEAKINESS_SURROUND_PX = 20


def _longest_run(row):
    """Longest contiguous True run in a 1-D boolean array. Vectorised."""
    if not row.any():
        return 0
    # Sentinel False at both ends so transitions catch leading/trailing runs.
    diffs = np.diff(np.concatenate(([False], row, [False])).astype(np.int8))
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]
    if len(starts) == 0:
        return 0
    return int((ends - starts).max())


def find_rule_based_masthead_bottom(page, top_ads=None, is_interior=False,
                                     dark_thr=None):
    """Return y (in PDF points) of the bottom edge of the bottom-most
    "real" thick horizontal rule in the top of the page, or None if
    no such rule exists.

    Detection runs on the full page width. A row counts as "ruled"
    if at least RULE_FILL_FRAC_FRONT (page 1) or
    RULE_FILL_FRAC_INTERIOR (interior pages) of its width is ink.
    Front-page masthead rules conventionally span the full width;
    interior section-banner rules can be cut to ~60% by a corner ad
    (e.g. 2007-02-13 p3's Gazette ad in top-right truncating the
    rule under "DISTRICT NEWS").

    A run of ruled rows whose thickness is between
    RULE_MIN_THICKNESS_FRAC and RULE_MAX_THICKNESS_FRAC of page
    height counts as a candidate.

    Each candidate is tested for **peakiness** — its row-fill against
    the average fill of the surrounding RULE_PEAKINESS_SURROUND_PX
    rows above and below. Real rules sit in whitespace (high
    peakiness 4×–30×); ad/photo-content artefacts sit inside
    continuous dense content (peakiness ~1×). Threshold
    RULE_PEAKINESS_THR = 3× cleanly separates them in this corpus.

    Bottom-most passing candidate is the masthead boundary.
    """
    zoom = RULE_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    thr = dark_thr if dark_thr is not None else RULE_DARK_THR
    dark = img < thr
    H, W = dark.shape

    # Full-page-width row fill. The fill-fraction threshold below is
    # what handles the "corner ad interrupts the section banner rule"
    # case — for interior pages we accept partial-width rules.
    frac = dark.mean(axis=1)

    search_frac = (RULE_SEARCH_TOP_FRAC_INTERIOR if is_interior
                   else RULE_SEARCH_TOP_FRAC_FRONT)
    top_lim = int(H * search_frac)
    fill_thr = (RULE_FILL_FRAC_INTERIOR if is_interior
                else RULE_FILL_FRAC_FRONT)
    filled = frac[:top_lim] >= fill_thr

    min_thick_px = max(2, int(round(RULE_MIN_THICKNESS_FRAC * H)))
    max_thick_px = max(min_thick_px,
                       int(round(RULE_MAX_THICKNESS_FRAC * H)))
    min_y_px = int(round(RULE_MIN_Y_PT * zoom))

    bottom_y_pt = None
    i = 0
    while i < len(filled):
        if filled[i]:
            j = i
            while j < len(filled) and filled[j]:
                j += 1
            thickness = j - i
            if (min_thick_px <= thickness <= max_thick_px
                    and i >= min_y_px
                    and _peakiness(frac, i, j - 1) >= RULE_PEAKINESS_THR):
                bottom_y_pt = j / zoom
            i = j
        else:
            i += 1
    return bottom_y_pt


def _peakiness(frac, run_start, run_end):
    """How much darker is this run than the rows around it?

    Real rules sit in whitespace and produce peakiness 4×–30×.
    Ad/photo-content artefacts sit in continuous dense content
    (rows above and below also dark) and produce peakiness ~1×.
    """
    s = RULE_PEAKINESS_SURROUND_PX
    rule_max = float(frac[run_start:run_end + 1].max())
    above = frac[max(0, run_start - s):run_start]
    below = frac[run_end + 1:min(len(frac), run_end + 1 + s)]
    if len(above) == 0 and len(below) == 0:
        return 0.0
    surr = np.concatenate([above, below])
    surr_avg = float(surr.mean())
    return rule_max / max(surr_avg, 0.01)


def _text_based_masthead_bottom(spans, page_w, page_h):
    """Fallback used only when rule-based detection finds nothing."""
    if not spans:
        return 0.0
    top_zone = [s for s in spans if s.y0 < page_h * 0.30]
    if not top_zone:
        return 0.0
    largest = max(top_zone, key=lambda s: (s.size, -s.y0))
    body = body_font_size(spans)
    if largest.size < body * 2.5:
        return 0.0
    band_bottom = largest.y1
    for s in top_zone:
        if (s.size < largest.size * 0.4
                and s.y0 < band_bottom + 45
                and s.y0 >= band_bottom - 5):
            band_bottom = max(band_bottom, s.y1)
    return band_bottom + 8


def find_masthead_bottom(spans, page_w, page_h, page=None, top_ads=None,
                          is_interior=False, dark_thr=None):
    """Return y at which content begins (bottom of masthead/section banner).

    Primary strategy: rule-based (renders the page; needs `page`).
    `is_interior=True` tightens the search zone to the top 15% (vs
    30% for front pages) because section banners on interior pages
    are short.
    `dark_thr` is the greyscale ink threshold (default RULE_DARK_THR);
    pass an adaptive value derived from page_profile for robustness
    across paper-quality variation.
    Fallback: text-based when no rule is found or `page` is None.
    """
    if page is not None:
        try:
            y = find_rule_based_masthead_bottom(
                page, top_ads=top_ads, is_interior=is_interior,
                dark_thr=dark_thr,
            )
        except Exception:
            y = None
        if y is not None:
            return y
    return _text_based_masthead_bottom(spans, page_w, page_h)


# Horizontal whitespace band detection ----------------------------------------

# A whitespace band is a horizontal strip of the page below the masthead that
# contains essentially no ink. Strong horizontal alignment of article tops
# leaves visible white-space bands across the mid-range of the page (per the
# user's layout note 2026-05-16). These bands are direct row-divider signals:
# an article's bottom can be clipped at the top of the next band.

WHITE_FRAC = 0.015        # row counts as "white" if < 1.5% of width is ink
MIN_BAND_THICKNESS_PT = 12  # gaps shorter than this are typography (between
                            # paragraphs/headline-and-body), not row dividers


def find_whitespace_bands(page, masthead_bottom, dark_thr=None):
    """Return list of (y_top, y_bottom) tuples for horizontal whitespace
    bands below the masthead, each spanning the full page width.

    Uses the same DPI-rendered greyscale as the masthead-rule detector;
    callers could share the render to avoid redoing it, but this is fast
    enough (~100ms/page at 150 DPI) that we keep the code path simple.
    `dark_thr` defaults to RULE_DARK_THR but should be passed from the
    page profile for consistent measurement across paper quality.
    """
    zoom = RULE_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8) \
            .reshape(pix.height, pix.width)
    thr = dark_thr if dark_thr is not None else RULE_DARK_THR
    dark = img < thr
    frac = dark.mean(axis=1)

    start_px = max(0, int(round(masthead_bottom * zoom)))
    min_thick_px = max(2, int(round(MIN_BAND_THICKNESS_PT * zoom)))
    is_white = frac < WHITE_FRAC

    bands = []
    i = start_px
    while i < len(is_white):
        if is_white[i]:
            j = i
            while j < len(is_white) and is_white[j]:
                j += 1
            if (j - i) >= min_thick_px:
                bands.append((i / zoom, j / zoom))
            i = j
        else:
            i += 1
    return bands


def classify_page(spans, page, page_w, page_h):
    """One-line page-type tag. See module docstring for values."""
    body = body_font_size(spans)
    span_count = len(spans)

    # image_only: very little text and the page is mostly an image
    image_area = 0.0
    try:
        for info in page.get_images(full=True):
            xref = info[0]
            for ir in page.get_image_rects(xref):
                image_area += ir.width * ir.height
    except Exception:
        pass
    image_frac = image_area / max(page_w * page_h, 1)
    if span_count < 100 and image_frac > 0.5:
        return "image_only"

    # classifieds: very many small-text spans
    small = sum(1 for s in spans if s.size < body * 0.8)
    if span_count > 800 and small > span_count * 0.4:
        # Classifieds pages are dominated by sub-body small text.
        return "classifieds"

    # looks_classical: a uniform 6-8 column grid at this density is
    # unlikely in post-1980 but worth flagging if we see it
    # (signature: ≥6 vertical column-edge clusters with high regularity)
    # Skipped in MVP — return 'modular' by default.

    return "modular"
