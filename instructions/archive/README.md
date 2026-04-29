# instructions/archive/

Historical documents and superseded code, kept for context. **Do not
update these files** — if a document here is still useful, copy what's
relevant into `instructions/detection_methods_review.md` or
`instructions/layout_observations.md` and cite the archived source.

## Contents

### Pipeline planning (pre-refactor)
- `newspaper_column_analysis_pipeline.md` — original pipeline design.
- `plan_archive_three_rectangles.md` — delivered plan for the
  R1/R2/R3 page-rectangle work (now part of `page_profile.py`).

### Refactor-1 planning (split_page → column_pipeline + LLM CLI)
- `refactor1_baseline.md` — pre-refactor architecture snapshot.
- `refactor1_recommendations.md` — recommendations going into Part 1.
- `refactor1_part2_cli_design.md` — Part 2 CLI design doc.
- `refactor1_part2_cli_llm_view.md` — Part 2 LLM-operations view.

### Superseded code
- `viewer.py` — obsolete static-HTML generator. The live viewer is now
  the dynamic SPA at `viewer.html` (root) + `viewer_data.json` (under
  `columns/`, written by `process_issue.py`). **Never run this script**
  — it overwrites `viewer.html` with a 10-issue stub and breaks the
  published viewer.

### Working notes
- `working_notes_stage0_bench.md` — Stage 0 hand-labelling bench
  working notes (issues 1947-02-27, 1947-11-06). The bench labels
  themselves live in `data/mvtm.db` as `detected_ads.hand_edited=1`
  rows; this file captured the in-flight notes during labelling.
