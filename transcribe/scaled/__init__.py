"""Scaled pipeline experiment (started 2026-08-15).

An isolated, parallel track that tries to derive newspaper structure
from *classical* signal -- Tesseract's own hOCR layout output and plain
geometry -- instead of paying an LLM to look at the page image.

Why it exists: the OCR+LLM route measures at 77-104k tokens/page and
93s/page. Across the 70,063-page corpus that extrapolates to 5.4-7.3
billion tokens and ~76 days of continuous running. Item segmentation
alone is 72% of that token cost.

Nothing in this package modifies the working OCR+LLM route. It writes
only to schema-v15 tables (`page_hocr_lines`, `page_hocr_regions`,
`page_columns`) and two additive columns on existing tables. Delete this
package and the production pipeline is unaffected.

Stages:
  hocr_parse      -- recover the layout signal ocr_llm.parse_hocr() drops
  detect_columns  -- column boundaries from hOCR geometry, no pixels/LLM
  build_iiif      -- IIIF 3.0 manifests + per-stage annotation layers

See instructions/scaled_pipeline.md for the full design record,
measured evidence, and how to run each stage.
"""
