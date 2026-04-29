"""
Processing context for the Almonte Gazette pipeline.

Assembles everything we know about a page BEFORE processing begins:
page type, era knowledge, profile data, ad zones, and derived values.
Built once per page, passed to every downstream function.

No function should query the database, derive recto/verso, or compute
pitch independently — it's all in the context.

Usage:
    from page_context import PageContext, build_context

    ctx = build_context(
        gazette_page=3, year=1937,
        db_path="data/mvtm.db",
        profile=prof,
        ads=detected_ads,
        issue_pitch=11.9,
        issue_columns=7,
    )
    # ctx.clean_side, ctx.era_pitch, ctx.expected_boundaries, etc.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class PageContext:
    """Everything known about a page before column detection begins."""

    # ── From page number (deterministic) ─────────────────────────────
    gazette_page: int
    page_type: str                  # "recto" or "verso"
    clean_side: str                 # "left" or "right"
    binding_side: str               # "left" or "right"
    is_page_2: bool

    # ── From intelligence layer (era knowledge) ──────────────────────
    era_pitch: Optional[float]      # median pitch for this era
    era_columns: Optional[int]      # expected column count for this era
    page_2_template: Optional[Dict] # wide-column pattern if applicable

    # ── From issue-level processing ──────────────────────────────────
    issue_pitch: Optional[float]    # pitch established from this issue
    issue_columns: Optional[int]    # column count from this issue

    # ── From page profile (this specific page) ───────────────────────
    r3_left: float                  # newspaper page boundary (% of page)
    r3_right: float
    text_area_left: float           # text content boundary (% of page)
    text_area_right: float
    paper_baseline: float
    dynamic_range: float
    column_darkness_threshold: float
    row_std_threshold: float

    # ── From ad detection (this specific page) ───────────────────────
    ad_zones: List[tuple] = field(default_factory=list)

    # ── Derived (computed once from the above) ───────────────────────
    pitch: float = 0.0              # best available pitch (issue > era > 11.0)
    num_columns: int = 7            # best available column count
    wide_pitch: float = 0.0        # 1.5 × pitch (for page 2)
    expected_boundaries: List[float] = field(default_factory=list)


def build_context(gazette_page, year, db_path="data/mvtm.db",
                  profile=None, ads=None,
                  issue_pitch=None, issue_columns=None):
    """
    Assemble the processing context for a page.

    Args:
        gazette_page:   Page number from the gazette filename (1-indexed)
        year:           Issue year
        db_path:        SQLite database path
        profile:        Dict from profile_page() — text_area, thresholds, etc.
        ads:            List of ad dicts from detect_ads()
        issue_pitch:    Pitch established from this issue (if known)
        issue_columns:  Column count from this issue (if known)

    Returns:
        PageContext with all fields populated.
    """
    from detect_ads import get_ad_exclusion_zones

    # ── Page type from page number ───────────────────────────────────
    page_type = "recto" if gazette_page % 2 == 1 else "verso"
    clean_side = "right" if page_type == "recto" else "left"
    binding_side = "left" if page_type == "recto" else "right"
    is_page_2 = (gazette_page == 2)

    # ── Era knowledge from intelligence layer ────────────────────────
    era_pitch = None
    era_columns = None
    page_2_template = None

    try:
        from layout_intelligence import LayoutDB
        db = LayoutDB(db_path)

        # Era pitch and columns
        prior = db.get_prior(year)
        if prior:
            era_columns = prior.get("expected_columns")
            # The prior returns typical_widths — median is the pitch
            if prior.get("typical_widths"):
                import numpy as np
                era_pitch = round(float(np.median(prior["typical_widths"])), 1)
            else:
                era_pitch = None  # no width data available

        # Page 2 template
        if is_page_2:
            tmpl = db.get_template("page2_editorial_wide", 2, year)
            if tmpl:
                page_2_template = tmpl.get("pattern")
    except Exception:
        pass

    # ── Profile data ─────────────────────────────────────────────────
    r3 = profile.get("r3", {}) if profile else {}
    r3_left = r3.get("left", 5.0)
    r3_right = r3.get("right", 95.0)
    ta = profile.get("text_area", {}) if profile else {}
    text_area_left = ta.get("left", 5.0)
    text_area_right = ta.get("right", 95.0)
    paper_baseline = profile.get("paper_mean", 0) if profile else 0
    dynamic_range = profile.get("dynamic_range", 200) if profile else 200
    col_thresh = profile.get("column_darkness_threshold", 60) if profile else 60
    row_thresh = profile.get("row_std_threshold", 45) if profile else 45

    # ── Ad zones ─────────────────────────────────────────────────────
    ad_zones = get_ad_exclusion_zones(ads or [])

    # ── Derive best available pitch and column count ─────────────────
    # Priority: issue (from this run) > era (from database) > default
    pitch = issue_pitch or era_pitch or 11.0
    num_columns = issue_columns or era_columns or 7
    wide_pitch = round(pitch * 1.5, 2)

    # ── Compute expected boundary positions ──────────────────────────
    # These are where columns SHOULD be if the grid is perfect.
    # Used as a reference for scoring detected boundaries.
    expected = []
    if is_page_2 and page_2_template:
        # Page 2 editorial: 2 wide + N regular from clean-side edge
        n_wide = page_2_template.get("wide_columns", 2)
        n_regular = page_2_template.get("regular_columns", 4)
        start = text_area_left if clean_side == "left" else text_area_right
        if clean_side == "left":
            x = start
            expected.append(round(x, 2))
            for _ in range(n_wide):
                x += wide_pitch
                expected.append(round(x, 2))
            for _ in range(n_regular):
                x += pitch
                expected.append(round(x, 2))
        else:
            x = start
            expected.append(round(x, 2))
            for _ in range(n_wide):
                x -= wide_pitch
                expected.insert(0, round(x, 2))
            for _ in range(n_regular):
                x -= pitch
                expected.insert(0, round(x, 2))
    else:
        # Standard: N columns from clean-side edge
        start = text_area_left if clean_side == "left" else text_area_right
        if clean_side == "left":
            expected = [round(start + i * pitch, 2) for i in range(num_columns + 1)]
        else:
            expected = [round(start - i * pitch, 2) for i in range(num_columns + 1)]
            expected.sort()

    # Constrain to R3 bounds — the grid cannot extend outside
    # the newspaper page boundary. All coordinates in PDF page space.
    # Use <= so a boundary that lands exactly on the R3 edge is kept
    # (happens when text_area == r3 after the page_cv R3 clamp).
    expected = [b for b in expected if r3_left <= b <= r3_right]

    return PageContext(
        gazette_page=gazette_page,
        page_type=page_type,
        clean_side=clean_side,
        binding_side=binding_side,
        is_page_2=is_page_2,
        era_pitch=era_pitch,
        era_columns=era_columns,
        page_2_template=page_2_template,
        issue_pitch=issue_pitch,
        issue_columns=issue_columns,
        r3_left=r3_left,
        r3_right=r3_right,
        text_area_left=text_area_left,
        text_area_right=text_area_right,
        paper_baseline=paper_baseline,
        dynamic_range=dynamic_range,
        column_darkness_threshold=col_thresh,
        row_std_threshold=row_thresh,
        ad_zones=ad_zones,
        pitch=pitch,
        num_columns=num_columns,
        wide_pitch=wide_pitch,
        expected_boundaries=expected,
    )


def print_context(ctx):
    """Human-readable summary."""
    print(f"Page {ctx.gazette_page} ({ctx.page_type})")
    print(f"  Clean: {ctx.clean_side}, Binding: {ctx.binding_side}")
    print(f"  Pitch: {ctx.pitch}% ({ctx.num_columns} cols)")
    if ctx.is_page_2 and ctx.page_2_template:
        print(f"  Page 2 template: {ctx.page_2_template}")
    print(f"  Text area: {ctx.text_area_left:.1f}%-{ctx.text_area_right:.1f}%")
    print(f"  Ad zones: {len(ctx.ad_zones)}")
    if ctx.expected_boundaries:
        bounds = " ".join(f"{b:.0f}" for b in ctx.expected_boundaries)
        print(f"  Expected boundaries: [{bounds}]")
