"""Per-stage DPI constants — documentation of intent.

Different pipeline stages use different DPIs deliberately. The values
are calibrated empirically against the heritage scan corpus and the
target output quality. Centralising them as named constants makes
the rationale explicit and prevents accidental homogenisation.

This file is documentation; it is not (yet) imported by the call
sites. The function signatures still hard-code their defaults
(`def f(..., dpi=150):`). Wiring this module into those signatures
is a separate, larger change — see refactor1_recommendations.md
B3 "full" for the wider option.
"""

# ── Column work — high-resolution ────────────────────────────────────

COLUMN_DETECTION_DPI = 450
"""Used by:
   - find_columns.find_column_boundaries / .._morph
   - column_pipeline.detect_strips
   - split_page.DEFAULT_DPI
   - process_issue.process_issue (top-level entry)
   - crop_pdf.crop_pdf
Why high: column rules are thin and faint on heritage scans; the
boundary peak detection needs the resolution to separate a 1-px rule
from background noise.
"""

COLUMN_EXTRACTION_DPI = 450
"""Used by:
   - detect_ads.extract_ad_images
   - split_page.extract_columns (via DEFAULT_DPI)
Why high: the extracted PNGs are downstream artefacts (the SVG
viewer, future article-level work). Re-rendering at lower DPI later
is fine; rendering originally at low DPI loses information forever.
"""


# ── Detection work — medium resolution ───────────────────────────────

AD_DETECTION_DPI = 150
"""Used by:
   - detect_ads.detect_ads
   - detect_ads.detect_single_col_ads
Why medium: ad bounding boxes are coarse (several columns wide).
150 DPI is enough to find the rectangular border via adaptive
threshold + contour analysis, and the lower resolution makes the
contour pass tractable on a full corpus.
"""

PROFILE_DPI = 150
"""Used by:
   - page_profile.profile_page
Why medium: page-level R2/R3/text-area profiling works on smoothed
column-darkness profiles. Higher DPI would not improve the
detection of wide whitespace troughs.
"""

SLIVER_DPI = 150
"""Used by:
   - detect_sliver.find_binding_edge
Why medium: looks for a wide white print margin and the dark
binding shadow — both are tens-of-pixels-wide features at 150 DPI.
"""

HEADLINE_DPI = 150
"""Used by:
   - detect_headlines.detect_headlines
Why medium: gutter-fill detection works on darkness ratios across
column boundaries. Headline type is large; resolution headroom
isn't useful.
"""


# ── Body text — finer resolution ─────────────────────────────────────

BODY_TEXT_DPI = 300
"""Used by:
   - detect_body_text.detect_body_text
Why higher than other detection: body-text periodicity analysis
relies on resolving individual lines of small type. 150 DPI blurs
adjacent lines; 300 DPI keeps them separable.
"""


# ── Validation — coarse resolution ───────────────────────────────────

VALIDATION_DPI = 75
"""Used by:
   - validate_columns.validate_edge_columns
Why low: the check is a coarse ink-content ratio per column to
identify empty edge columns. 75 DPI averages out per-character
noise and runs fast.
"""
