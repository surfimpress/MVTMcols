# Rasterisation pipeline — what gets rendered, how, by whom

The corpus PDFs embed JBIG2 1-bit bitmaps; older work re-rendered
them as RGB at multiple DPIs, throwing away the bilevel nature and
inflating both memory and disk. This document maps the pipeline as it
stands now: who renders, who reads from disk, what DPI each stage
uses, and what the embedded-bitmap fast path bypasses.

Cross-link: per-stage DPI rationale lives in `dpi_constants.py` —
that file is the source of truth for *why* each stage uses the DPI it
does. This document is the *where* and *how*.

## Source format

Empirically observed in the corpus:

- 1946 / 1948 / 1902: single 1-bit JBIG2-encoded page image, ~510 PPI
  native (e.g. 1902-01-17: 4290×5979).
- 1923-06-22: single 1-bit JBIG2, 4287×5996 native (~520 PPI).
- 1922-06-30: same shape (single 1-bit image).
- Later eras (post-1948): unverified; the auto-gate falls back to the
  legacy fitz path automatically when the page isn't single-image
  bilevel.

The fast path's gate condition is therefore: `len(page.get_images(full=True)) == 1`
AND `doc.extract_image(xref)['bpc'] == 1`. JBIG2-specifically isn't
required — any 1-bit single-image page qualifies.

## Producers — what gets written to disk

All disk artefacts live under `columns/<YYYY-MM-DD>/p<N>/` (per-page)
plus `columns/<YYYY-MM-DD>/ads/p<N>/` (per-ad crops).

| Artefact | Producer | Library | DPI | Mode (legacy) | Mode (bitmap path) |
|---|---|---|---|---|---|
| `page_raw.png` | `process_issue.py:954–998` | PyMuPDF + PIL | 150 (legacy) / native ~510 (bitmap) | RGB | mode='1' at native PPI |
| `page_display.avif` | `process_issue.py:954–998` (alongside `page_raw.png`) | PIL (LANCZOS + AVIF q=70) | 150 | mode='L' from RGB render | mode='L' from native bitmap |
| `*_col<N>.png` (no ads) | `split_page.py:181–186` | PIL | 450 (legacy) / native (bitmap) | RGB | mode='1' at native PPI |
| `*_col<N>.png` (ads-present) | `split_page.py:187+` | PIL | 450 | RGBA (alpha holes for ad rectangles) | unchanged — bitmap path declines this case (see below) |
| `ads/p<N>/*.png` | `detect_ads.py:1255–1273` | PIL | 450 (legacy) / native (bitmap) | RGB | mode='1' at native PPI |
| `body_blur.png` | `process_issue.py:792` | derived from in-memory blur output | 150 | mode='L' (8-bit grey) | unchanged — see below |
| `page_meta.json` | `process_issue.py` (per-page metadata) | json | n/a | n/a | n/a |
| `page_analysis.json`, `page_cv.{json,npz}` | `process_issue.py` | json / npz | n/a | n/a | n/a |

### Why a separate display artefact

`page_raw.png` is the archival/processing output — kept high-fidelity
(mode='1' at native ~510 PPI for the bitmap path) so downstream tools
can re-derive cuts, do further analysis, etc. It is **not** safe to
load in a browser: a 6388×9034 mode='1' PNG decodes to ~220 MB RGBA
in the renderer, which trips Safari/Chromium's "A problem repeatedly
occurred" guard.

`page_display.avif` is the browser-display variant: 150-DPI mode='L'
LANCZOS-downsampled greyscale, AVIF quality=70, ~470 KB on disk and
~9 MB RGBA in the browser. Visually preferred (compared mode='1' /
mode='L', PNG / AVIF, native-downsampled / fitz-rerender,
2026-05-02 — `display_trial/`).

`viewer.html` and `page_viewer.html` reference `page_display.avif`.
On 404 they fall back to `page_raw.png` — safe for pre-un-gate issues
(RGB at ~150 DPI, ~24 MB browser RAM) but unsafe for un-gate-era
issues, which therefore require `page_display.avif` on disk
(backfilled by `scripts/backfill_page_display.py`).

### The bitmap fast path

`pdf_utils._build_page_shaped_bitmap(doc, page)` is the single entry
point. It pastes the embedded 1-bit image into a page-shaped white
mode='1' canvas at native PPI, positioned by `page.get_image_bbox`.
Within the bbox the bitmap is byte-identical to the source — no
resize, no resample. Outside the bbox the canvas is white at native
PPI.

Two consumers route through it:

- `_try_embedded_bitmap_render` (in-memory render path used by every
  detector via `render_grey` / `render_grey_uint8` / `get_clip_pixmap`).
- `try_embedded_bitmap_pil` (writers in `process_issue`, `split_page`,
  `detect_ads` — they receive the page-shaped PIL Image and crop /
  save without re-rendering).

Why **page-shaped** rather than bbox-shaped: detector outputs are
expressed as page-rect percentages. A bbox-shaped writer artefact
would scale-mismatch the overlay coordinate system; this was the
proximate cause of the 1902-01-17 column-overlay misalignment fixed
in `eb1d4ae`.

The path is auto-enabled per page; opt-out via env var
`MVTM_USE_EMBEDDED_BITMAP=0`.

### Caveats

- **Ads-present column thumbnails** still go through the legacy RGBA
  path (`split_page.py:187+`). That branch needs an alpha plane to
  hide ad rectangles, which mode='1' can't carry. Mode='LA' would
  work but isn't wired up; the ads-present case stays on the legacy
  pixmap-then-PIL conversion.
- **`body_blur.png` stays as derived greyscale.** It's produced from
  `cv2.GaussianBlur` output of the body-text detection pipeline —
  the artefact is a smooth continuum of greys *by design* (it's the
  blur, not the source). Forcing it to mode='1' would destroy the
  signal it carries.
- **Off-by-one width rounding.** `_try_embedded_bitmap_render`
  resamples to `int(round(page.rect.width * dpi / 72.0))` to match
  MuPDF's full-page render dimensions. Edge-case differences are
  possible on non-canonical DPIs; verified against MuPDF's output at
  the canonical DPIs in actual use.
- **Browser memory on `page_raw.png`.** The native-PPI mode='1'
  artefact is ~6388×9034 pixels — ~220 MB RGBA in a browser, which
  used to trip the renderer for un-gate-era issues. Resolved by
  introducing the separate `page_display.avif` artefact (see "Why a
  separate display artefact" above). `page_raw.png` is no longer
  consumed by the viewer.

## Consumers — who reads what

### Detectors NEVER read from disk

Verified across the codebase: every detector reads pixels via
`pdf_utils.render_grey` / `render_grey_uint8` / `get_clip_pixmap`,
backed by the in-memory render cache in `pdf_utils`. The on-disk
artefacts above are *display only* (plus one diagnostic). This is the
load-bearing constraint for any future change: the bitmap fast path
benefits writers and the in-memory cache simultaneously, but the
detector contract is "you get pixels via pdf_utils", not "you read
files".

| Detector | DPI | Render call |
|---|---|---|
| `detect_ads.detect_ads` | 150 | `render_grey_uint8` |
| `page_cv` | 150 | `render_grey_uint8` |
| `detect_headlines` | 150 | `render_grey` |
| `detect_body_text` | 300 | `render_grey` |
| `find_columns`, `column_pipeline.detect_strips` | 450 | `render_grey` |
| `detect_sliver` | 150 | `render_grey` |
| `page_profile` | 150 | `render_grey` |
| `split_page.extract_columns` | 450 | `get_clip_pixmap` (or bitmap PIL crop in fast path) |
| `detect_ads.extract_ad_images` | 450 | `get_clip_pixmap` (or bitmap PIL crop in fast path) |

### Display-only consumers

- `viewer.html:191` — `page_display.avif` thumbnail in the issue list
  (with `retryImg`-driven fallback to `page_raw.png` for pre-un-gate
  issues that don't have an AVIF on disk).
- `page_viewer.html:398–404` — `page_display.avif` as the `<img>`
  source for the page view; `onerror` falls back to `page_raw.png`.
  `:608` — `body_blur.png` for the body-text overlay.
- `ads.html` — references to `ads/p<N>/*.png` thumbnails.

The only on-disk re-read by Python is `explore_pipeline.py:308`
(`Image.open(page_raw_path).convert("RGB")`) — a stand-alone
diagnostic harness, not part of `process_issue`.

## DPI constants — point of truth

`dpi_constants.py` documents what each stage uses and why. Quick
summary:

| Constant | Value | Used for |
|---|---|---|
| `COLUMN_DETECTION_DPI` | 450 | column boundary peak detection (rules are thin) |
| `COLUMN_EXTRACTION_DPI` | 450 | column / ad PNG extraction |
| `AD_DETECTION_DPI` | 150 | adaptive threshold + contour for ad bboxes |
| `PROFILE_DPI` | 150 | page-level R2/R3/text-area profiling |
| `SLIVER_DPI` | 150 | binding edge / margin detection |
| `HEADLINE_DPI` | 150 | gutter-darkness sums |
| `BODY_TEXT_DPI` | 300 | line-period analysis (needs to resolve adjacent lines) |
| `VALIDATION_DPI` | 75 | empty-edge-column check (coarse, fast) |

The constants file is documentation; call sites still hard-code the
values. Wiring it through is a separate refactor (out of scope for
the rasterisation cleanup).

## Cache contract

`pdf_utils._RENDER_CACHE` keys on `(pdf_path, mtime, page_number, dpi)`.
Sized at 12 (one issue's worth of pages). Slim path stores only
`grey_u8`; RGB / fitz.Pixmap built lazily if a consumer demands it.
For 1-bit-source issues the RGB triple-stack is now never
materialised in normal detection runs.

When a non-canonical DPI is requested, the canonical entry
(`CANONICAL_DPI = 450`) is rendered first; the lower-DPI entry is
derived by `cv2.INTER_AREA` downsampling. This keeps the source
render once-per-page even when stages disagree on DPI.

`clear_render_cache()` drops everything; called between issues in
batch processing.

## Update history

- **2026-05-02 — `page_display.avif` artefact added.** Resolves the
  open browser-memory issue. Writer in `process_issue.py` produces a
  150-DPI mode='L' AVIF q=70 alongside `page_raw.png`. Viewers
  (`viewer.html`, `page_viewer.html`) point at the AVIF with
  fallback to `page_raw.png`. Backfill script
  `scripts/backfill_page_display.py` filled in the 437 un-gate-era
  pages (mode='1' page_raw.png) across the corpus; pre-un-gate
  RGB pages are skipped — they're already browser-safe at ~150 DPI.
- **2026-05-02 — Initial draft.** Steps 0–6 of the "cautious removal
  of pointless rendering" plan landed: slim cache, mode='1' writers
  for `page_raw.png` / `*_col*.png` (no-ads) / ad crops, page-shaped
  canvas via `_build_page_shaped_bitmap`, un-gate of the bitmap path
  (`a04d07a`), Plan A coordinate alignment fix (`eb1d4ae`), DB
  snapshot-then-delete safety (`2de9660`), per-page artefact
  move-aside (`017fd64`). Open: page_raw.png browser memory cost.
