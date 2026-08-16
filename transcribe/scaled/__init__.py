"""Scaled pipeline experiment (started 2026-08-15).

An isolated, parallel track that tries to derive newspaper structure
from *classical* signal -- Tesseract's own hOCR layout output and plain
geometry -- instead of paying an LLM to look at the page image.

Why it exists: the OCR+LLM route measures at 77-104k tokens/page and
93s/page. Across the 70,063-page corpus that extrapolates to 5.4-7.3
billion tokens and ~76 days of continuous running. Item segmentation
alone is 72% of that token cost.

Nothing in this package modifies the working OCR+LLM route. It writes
only to additive tables (`page_hocr_lines`, `page_hocr_regions`,
`page_columns` at v15; `page_hlines` v17, `page_photo_captions` v19,
`page_zones` v20) and additive columns on existing tables. Delete this
package and the production pipeline is unaffected.

Stages, in the order they run:
  hocr_parse           -- recover the layout signal ocr_llm.parse_hocr() drops
  detect_content_area  -- 1c: where the type starts and stops. BEFORE columns
  detect_grid          -- 2:  the column lattice, fitted as four numbers
  detect_zones         -- 2b: boxed zones, from rule corners
  detect_captions      -- 2c: photos paired with their captions
  detect_hlines        -- 3:  horizontal alignments, with a column span
  build_iiif           -- IIIF 3.0 manifests + per-stage annotation layers

`detect_columns` is ARCHIVED (archive/detect_columns.py) -- it was the
confidence-scoring generation, superseded by detect_grid. It was still
listed here long after that, which is the first thing a cold session
reads.

See instructions/scaled_pipeline.md for the full design record,
measured evidence, and how to run each stage.
"""
