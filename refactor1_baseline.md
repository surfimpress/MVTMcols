# Refactor 1 — Baseline audit

Snapshot of the MVTM codebase as of branch `refactor-1` (off `main` at `8b476e5`). This is a read-only audit; no `.py` / `.html` / schema changes are made in this commit.

## 1. Repository at a glance

- 18 Python files at repo root (no Python in subdirs)
- 7 SQLite tables in `data/mvtm.db`
- 2 HTML viewers (`viewer.html`, `page_viewer.html`) + 1 generator (`viewer.py`)
- 4 JSON output formats (`page_meta.json`, `page_analysis.json`, `issue_summary.json`, `viewer_data.json`)

## 2. Python scripts — function inventory

For each file: purpose (1 line), top-level functions, who imports it, who it imports, pipeline role.

### Orchestrator

**`process_issue.py`** — Issue-level orchestrator. Downloads pages, profiles each, detects ads, runs two-pass column detection, validates, extracts column PNGs, writes viewer JSON.
- `download_issue(year, month, day, db_path, download_dir)` — query `files` table, curl PDFs from Drive, verify magic bytes
- `_score_regularity(result)` — coefficient of variation of column widths
- `_clean_side_pitch(result, profile)` — pitch from clean (non-binding) side only
- `_establish_pitch(pass1_results)` — aggregate clean-side pitches recto/verso → issue pitch + column count
- `process_issue(year, month, day, output_dir, db_path, download_dir, dpi)` — main entry
- `_update_viewer_data(db_path, columns_dir)` — emit `viewer_data.json`
- Imports from: `split_page`, `page_profile`, `detect_ads`, `page_context`, `column_pipeline`, `layout_intelligence` (dynamic), `validate_columns` (dynamic), `detect_headlines` (dynamic, line 532), `detect_body_text` (dynamic, line 552)
- Imported by: nothing (entry point)

### Column-detection chain

**`split_page.py`** — Stage 1: column boundary detection per PDF page. Multi-strip consensus, edge projection, narrow-column merge, PNG extraction.
- `_detect_consensus(pdf_path, page_number, dpi, page_prof, expected_columns, ad_exclusion_zones)` — run boundary detection on each strip, keep boundaries appearing in ≥40% of strips
- `_project_grid_edges(boundaries, tolerance, text_left_pct, …)` — extend interior boundaries to text-area edges
- `_remove_narrow_columns(boundaries, min_width_pct)` — drop too-narrow columns
- `_select_best_grid(boundaries, max_n)` — pick most plausible candidate set
- `_validate(boundaries)` — order/overlap sanity
- `extract_columns(pdf_path, boundaries, page_number, dpi, output_dir, buffer_vw, ads_with_ids)` — render + crop + save PNG (RGBA when `ads_with_ids` given)
- `split_page(...)` — main entry
- `_log_to_db(result, db_path)` — INSERT into `page_splits`
- `_save_metadata(result, path)` — write `page_meta.json`
- Imports from: `find_columns`, `page_profile`, `detect_sliver`
- Imported by: `process_issue`

**`column_pipeline.py`** — Decomposed column detection in three stages: `detect_strips` → `cluster_boundaries` → `place_columns`. Pure functions, no side effects.
- `detect_strips(pdf_path, ctx, dpi)`
- `cluster_boundaries(detections, merge_distance, strip_profiles, ad_zones)`
- `place_columns(boundaries, ctx)` — dispatcher
- `place_standard(boundaries, ctx)` — regular pages
- `place_page2_editorial(boundaries, ctx)` — special template
- `_boundaries_from_positions(positions)`
- `_merge_narrow(boundaries, min_width)`
- Imports from: `find_columns`
- Imported by: `process_issue`

**`find_columns.py`** — Lowest-level column boundary detector. Two strategies: darkness-peak and morphological vertical-rule.
- `_open_clean(pdf_path)` — strip red overlay lines from PDF
- `find_column_boundaries(pdf_path, x, y, w, h, page_number, dpi, …)` — peak-based
- `find_column_boundaries_morph(pdf_path, x, y, w, h, …)` — kernel-based
- `print_results(results)`
- Imports from: nothing
- Imported by: `column_pipeline`, `split_page`

**`validate_columns.py`** — Post-placement validation. Drops empty edge "columns" (slivers / margin strips) by ink-content ratio.
- `_render_grey(pdf_path, page_number, dpi)`
- `_column_ink_means(grey, boundaries)`
- `validate_edge_columns(boundaries, pdf_path, page_number, dpi)` — drop first/last if ink < 35% of median interior
- Imports from: nothing
- Imported by: `process_issue` (dynamic)

### Profilers / context

**`page_profile.py`** — Per-page bounding boxes (R1/R2/R3), paper baseline, ink stats, binding shadow, adaptive thresholds. Run before any detection.
- `_open_clean(pdf_path)` — duplicates other copies (see recommendations)
- `find_rectangles(inv, h, w, gazette_page, pdf_image_rect)`
- `_extract_gazette_page(pdf_path)`
- `profile_page(pdf_path, page_number, profile_dpi, gazette_page)` — main entry
- `print_profile(prof)`
- Imports from: nothing
- Imported by: `split_page`, `process_issue`

**`page_context.py`** — Dataclass + factory holding all page-level state passed to downstream functions: page type (recto/verso), binding side, era pitch, expected columns, R3/text-area boundaries, ad zones.
- `build_context(gazette_page, year, db_path, profile, ads, issue_pitch, issue_columns)`
- `print_context(ctx)`
- Imports from: nothing top-level (`LayoutDB` imported dynamically)
- Imported by: `process_issue`

**`layout_intelligence.py`** — Era-level layout DB. Records successful detections, builds priors per decade.
- `LayoutDB.__init__(db_path)`
- `LayoutDB._conn()`
- `LayoutDB._init_tables()`
- `LayoutDB.record_layout(year, month, day, page, num_columns, boundary_positions, …)`
- `LayoutDB.get_prior(year)`
- `LayoutDB.get_template(name, page, year)`
- `print_prior(prior)`
- Imports from: nothing
- Imported by: `process_issue` (dynamic), `page_context` (dynamic)

### Detectors (display content)

**`detect_ads.py`** — Bordered display-ad detection. Adaptive threshold + contour analysis. Recently extended with Tier 1 multi-pass adaptive thresholds for low-contrast pages.
- `_open_clean(pdf_path)` — DUPLICATE of same name in 4 other files
- `_detect_ads_pass(grey, h, w, block_size, C, kernel_size, iterations, …)` — single threshold-and-contour pass
- `detect_ads(pdf_path, page_number, render_dpi, …, page_profile)` — main entry, runs Pass 1 always + Pass 2 conditional on contrast
- `detect_single_col_ads(pdf_path, multi_col_ads, …)` — narrower-width sibling
- `get_ad_exclusion_zones(ads, min_confidence)` — convert to (x1,x2,y1,y2) tuples
- `print_ads(ads)`
- `extract_ad_images(pdf_path, ads, output_dir, page_number, dpi, name_prefix)`
- `init_ads_table(db_path)`
- `store_ads(db_path, year, month, day, page, ads_with_images)` — returns inserted id list
- Imports from: nothing
- Imported by: `process_issue`

**`detect_sliver.py`** — Facing-page sliver detection on the binding side.
- `_open_clean(pdf_path)` — DUPLICATE
- `find_binding_edge(pdf_path, page_number, binding_side, pitch, last_grid_boundary, render_dpi)`
- Imports from: nothing
- Imported by: `split_page`

**`detect_headlines.py`** — Multi-column headlines (gutter-fill detection).
- `detect_headlines(pdf_path, column_boundaries, …)`
- `assemble_headlines_from_charts(body_text_charts, columns_meta, …)`
- Imports from: nothing
- Imported by: `process_issue` (dynamic, line 532), `detect_body_text`

**`detect_body_text.py`** — Per-column body-text region detection via vertical-stripe periodicity analysis.
- `detect_body_text(pdf_path, columns, page_number, dpi, …)`
- Imports from: `coordinates`, `detect_headlines`
- Imported by: `process_issue` (dynamic, line 552)

### Stage 2 / future / standalone

The following are present but NOT called from `process_issue`. Some are future-stage utilities, others are diagnostic-only.

- **`find_splits.py`** — Item segmentation within a column (calibrate, find features, group into article boundaries, extract item PNGs). Designed for Stage 2 article-level work. Not currently called.
- **`classify_segments.py`** — LLM (Claude) classification of segment images, with cross-column continuation tracking. Not currently called.
- **`four_probe_v5.py`** — Diagnostic body-text classifier with autocorrelation + comb matching. Generates a 5-panel chart. Standalone CLI tool.
- **`crop_pdf.py`** — Generic PDF cropping utility (grid/percent/pixel units). Standalone.
- **`coordinates.py`** — Coordinate conversions (pct ↔ px ↔ frac). Used only by `detect_body_text`.
- **`viewer.py`** — Static HTML generator producing `viewer.html` from per-issue summaries. Manual run.

## 3. Orchestration spine

```
process_issue(year, month, day)
  ├── download_issue           (SELECT files; curl PDFs)
  ├── per page:
  │     ├── profile_page       (R1/R2/R3, paper, ink, binding)
  │     ├── detect_ads          (Pass 1 + optional Pass 2 [Tier 1])
  │     ├── detect_single_col_ads
  │     ├── extract_ad_images   (renders ad PNGs)
  │     └── store_ads           (INSERT detected_ads, returns ids)
  ├── per page (Pass 1 columns):
  │     ├── build_context       (page_type, binding, era pitch, ad zones)
  │     ├── detect_strips       (strip-by-strip find_column_boundaries)
  │     └── cluster_boundaries
  ├── _establish_pitch          (recto/verso aggregate)
  ├── per page (Pass 2 columns, with issue pitch):
  │     ├── place_columns
  │     ├── validate_edge_columns
  │     └── extract_columns     (RGBA PNGs with ad cutouts + ids)
  ├── per page (optional layers):
  │     ├── detect_headlines    (dynamic import)
  │     └── detect_body_text    (dynamic import)
  └── _update_viewer_data       (emit viewer_data.json from DB)
```

## 4. Database — table inventory

`/Users/peter/Projects/MVTM/data/mvtm.db`

| Table | Rows | Purpose | Populated by | Read by |
|---|---|---|---|---|
| `files` | 142,846 | Drive metadata for every Almonte Gazette PDF | externally / pre-existing | `process_issue.download_issue` |
| `page_splits` | 21 | Diagnostic log from `split_page.split_page()` | `split_page._log_to_db` | `split_page` (status) |
| `page_layouts` | 281 | Final per-page column layouts | `LayoutDB.record_layout` | `process_issue` (status), `LayoutDB.get_prior` |
| `era_patterns` | 13 | Per-decade dominant column count + widths | `LayoutDB` (pattern learning) | `LayoutDB.get_prior` |
| `detected_ads` | 3,391 | All detected display ads + image filenames | `detect_ads.store_ads` | `process_issue._update_viewer_data`, `process_issue` (counts) |
| `page_geometry` | 281 | Per-page R2/R3/text-area + binding side | `LayoutDB.record_geometry` | `LayoutDB` (lookup) |
| `layout_templates` | 1 | Named recurring patterns (e.g. `page2_editorial_wide`) | `LayoutDB.record_template` | `LayoutDB.get_template` |

### Suspect fields (no judgement, just observation)

- `files.incorrect_date`, `files.likely_date` — declared but never SELECTed in any `.py`
- `detected_ads.aspect` — alongside `rect_ratio`; possible duplicate (would need code-side check)
- `detected_ads.cols` vs `page_layouts.num_columns` — different terminology for similar concept
- `page_splits` very low row count (21) — most pipeline state is in-memory, not persisted here

### Indexes

- `idx_detected_ads_issue` on `(year, month, day)`
- `idx_page_layouts_year`, `idx_page_layouts_issue`
- `idx_page_geometry_year`, `idx_page_geometry_issue`

No foreign keys. Tables are joined ad-hoc on `(year, month, day, page)` tuples.

## 5. Viewers and JSON outputs

### HTML viewers

- **`page_viewer.html`** — interactive per-page SVG layer viewer; consumes `viewer_data.json` + per-page `page_analysis.json`. Defines a 12-entry `LAYERS` array (R2, R3, text area, multi/single-col ads, boundaries, columns, body text, headlines, horizontal rules, content-by-column). URL params: `?issue=<date>&page=<num>`. Hardcoded `BASE = '/MVTM/columns'`.
- **`viewer.html`** — static issue-grid landing page; consumes `viewer_data.json`. Generated by `viewer.py`.
- **`viewer.py`** — generator for `viewer.html` from `issue_summary.json` files in `columns/`. Manual run, not part of `process_issue`.

### JSON outputs

| File | Per | Producer | Consumer |
|---|---|---|---|
| `page_meta.json` | page | `split_page._save_metadata` | `page_viewer.html` (indirectly) |
| `page_analysis.json` | page | `process_issue` (consolidates detectors) | `page_viewer.html` |
| `issue_summary.json` | issue | `process_issue` final block | `viewer.py` (when regenerating `viewer.html`) |
| `viewer_data.json` | global | `process_issue._update_viewer_data` | `viewer.html` + `page_viewer.html` |

### Dev / output dirs

- `columns/<date>/p<n>/` — per-page output (PNGs, JSONs)
- `screenshots/` — untracked, dev-only
- `working_notes_profile_review.md` — untracked dev note
- `instructions/` — present but contents not enumerated in this audit

---

## Notes on the audit itself

- Two of the explore agents reported that `detect_headlines.py` and `detect_body_text.py` are "imported by nothing". Verified manually that they are imported dynamically from `process_issue.py` at lines 532 and 552 respectively. The audit above corrects this. Flagged because dynamic imports are a recurring blind spot in static scans.
- The audit was assembled from three parallel read-only explorations + a small number of direct verifications. Not every line of every file was read; the function-list completeness should be confirmed before treating it as canonical.
