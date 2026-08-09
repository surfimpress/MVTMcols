"""Which pipeline handles a given issue: column-cut or OCR+LLM.

Pre-1980 issues have already been through column cutting (sunk cost,
QA'd). 1980s+ issues resisted column detection -- per
instructions/layout_observations.md and this project's own status
notes, that cutting/QA was never signed off and the columns that do
exist for a handful of 1980s issues are explicitly flagged as not to
be built on. That reality, not a stylistic preference, is why the
cutover is a hard year boundary rather than a per-issue heuristic:
"has this issue's column-cut been completed and QA'd" and "is this
issue 1980 or later" are, as of this writing, the same question.

If a specific issue is later found to need the opposite route (a
pre-1980 issue whose columns were never cut, or a 1980s+ issue that
did get a clean column-cut), override it explicitly at the call site
rather than changing the cutoff -- the cutoff is a corpus-wide default,
not a guarantee about any one issue.
"""

from __future__ import annotations

COLUMN_CUT_CUTOFF_YEAR = 1980  # last year handled by the column-cut route


def route_for_date(year: int) -> str:
    """'column_cut' or 'ocr_llm'."""
    return "column_cut" if year < COLUMN_CUT_CUTOFF_YEAR else "ocr_llm"


def layout_class_for_date(year: int) -> str:
    """'column_grid' or 'modular' -- the pages.layout_class value for
    the OCR+LLM route's own record-keeping. Only meaningful for issues
    actually routed to ocr_llm; column_cut issues don't populate this
    field at all (no pages/page_ocr_blocks rows are written for them)."""
    return "column_grid" if year < COLUMN_CUT_CUTOFF_YEAR else "modular"
