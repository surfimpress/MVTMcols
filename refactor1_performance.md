# Refactor 1 — Performance audit (report only)

**Status:** report only. No code changes proposed in this commit.
**Companion docs:** `refactor1_baseline.md`, `refactor1_recommendations.md`,
`refactor1_part2_cli_design.md`. A separate security audit will follow.

---

## 1. Methodology

The pipeline was profiled end-to-end on issue **1947-11-06** — the
incumbent regression baseline (8 pages, recto/verso, ad-heavy).

- **Run:** `process_issue(1947, 11, 6)` from a single Python process,
  PDFs already cached on disk in `/tmp/issue_1947-11-06/` (so the
  download time recorded below is curl-against-Drive but no actual
  byte transfer).
- **Tool:** `cProfile` capturing every Python call, dumped to
  `/tmp/perf_audit.prof`. Wall time captured in parallel via
  `time.perf_counter`.
- **Verification:** post-run row counts on `(detected_ads,
  page_layouts, page_geometry)` for the issue = (32, 8, 8) — matches
  the regression baseline exactly. Pitch = 10.64% from 8 columns,
  matches baseline.
- **No code was changed for the measurement.** cProfile is non-invasive.

The profile data is the source of truth for every "saves N seconds"
claim in the ranking below. Where I had to estimate (e.g. parallel
speedup), I say so explicitly.

---

## 2. Baseline timing breakdown

**Total wall time: 76.91s** for one issue (8 pages).

### 2.1 Cumulative time by stage

| Stage | Cumtime (s) | % of total | Calls | Per-page |
|---|---:|---:|---:|---:|
| Pixmap rendering (all calls combined) | 42.86 | **55.7%** | 223 | ~28 |
| `download_issue` (curl waitpid) | 12.77 | 16.6% | 8 | 1.6s |
| `detect_strips` (column_pipeline) | 15.68 | 20.4% | 8 | 1.96s |
| `extract_columns` (split_page) | 15.16 | 19.7% | 8 | 1.90s |
| `profile_page` (page_profile) | 8.74 | 11.4% | **16** ⚠ | 1.09s |
| `extract_ad_images` (detect_ads) | 5.94 | 7.7% | 9 | 0.74s |
| `detect_body_text` | 5.21 | 6.8% | 8 | 0.65s |
| `detect_headlines` | 2.08 | 2.7% | 8 | 0.26s |
| `detect_ads` | 1.96 | 2.5% | 8 | 0.25s |
| `detect_single_col_ads` | 1.90 | 2.5% | 8 | 0.24s |
| `validate_edge_columns` | 1.57 | 2.0% | 8 | 0.20s |
| `_update_viewer_data` | 0.75 | 1.0% | 1 | — |
| `place_columns` | 0.012 | <0.1% | 8 | — |
| `cluster_boundaries` | 0.011 | <0.1% | 8 | — |
| `store_ads` | 0.011 | <0.1% | 9 | — |
| All `sqlite3.connect` (78 calls) | 0.010 | <0.1% | 78 | — |
| `open_clean_pdf` (145 calls) | 0.13 | 0.2% | 145 | ~18 |

(Stage cumtimes overlap because each includes its children — the
true non-overlapping cost is in the categories below.)

### 2.2 Where the wall time actually goes

The bottom-of-stack truth: **52.7% of wall time (40.52s) is spent
inside MuPDF's `fz_run_display_list`** — i.e. raw PDF rasterisation.
Anything that reduces the number of pixmap calls saves proportional
time. The remaining 47% splits roughly:

- ~17% subprocess (curl) wait
- ~11% numpy/Python boundary detection
- ~6% PNG encoding (PIL `_save` 4.4s, 64 calls)
- ~5% MuPDF image decoding (32 calls of
  `ll_fz_get_pixmap_from_image_outparams_fn`, 2.47s — used inside
  `extract_ad_images`)
- ~8% remainder: JSON dump (2.44s for 17 dumps), body-text Python
  work, headlines Python work

### 2.3 Render-call breakdown

| Source | Calls | DPI | Cumtime |
|---|---:|---:|---:|
| `find_column_boundaries` (detect_strips darkness pass) | 56 | 450 | 12.56s |
| `find_column_boundaries_morph` (detect_strips morph pass) | 16 | 450 | 3.09s |
| `extract_columns` per-column rasters | ~56 | 450 | ~10s of 15.2s |
| `extract_ad_images` | 9 ads × ~3 = ~27 | 450 | ~3.5s of 5.94s |
| `validate_edge_columns` | 8 | 75 | 1.57s |
| `detect_ads` (Pass 1 + adaptive Pass 2 if invoked) | 8–16 | 150 | 1.96s |
| `detect_single_col_ads` | 8 | 150 | 1.90s |
| `detect_headlines` | 8 | 150 | 2.08s |
| `detect_body_text` | 8 | 300 | 5.21s |
| `profile_page` | 16 | 150 | 8.74s |

**Total: ~225 pixmap calls per issue (~28/page) producing
40.5s of pure rasterisation.**

### 2.4 What the reconnaissance got wrong

Two findings flagged as hotspots in the recon turn out to be
non-issues:

- **`open_clean_pdf` overlay-strip churn:** estimated as 11–13×/page;
  measured as **18×/page** (145 total). But each call costs ~0.9ms —
  total cost is **0.13s** (0.17% of wall time). The xref scan is
  cheap; only the pixmap that follows is expensive. **P5 is dropped
  from the ranking.**
- **DB connection lifecycle:** 78 connections measured, total cost
  **0.010s.** SQLite is local and fast. **P6 is dropped from the
  ranking.**

A finding the recon flagged but I want to keep visible:
- **`profile_page` runs twice per page (16 calls for 8 pages)** —
  confirmed empirically. 8.74s total; deduplication saves 4.37s.

---

## 3. Ranked opportunities

Each entry: ID, title, **measured current cost**, proposed change,
**expected saving** (with method), quality-preservation check, risk.

### Tier 1 — Pixmap reduction (the big wins)

#### P1. Per-page parallelism with `multiprocessing`

**Where:** `process_issue.py:264, 317, 419` — three sequential
per-page loops covering ad detection, boundary detection, and
placement+extraction.

**Current cost:** 76.91s wall; ~64s page-bound (excluding 12.77s
download). All single-process.

**Proposed change:** wrap each per-page loop in
`multiprocessing.Pool(processes=N).imap`. Pages are independent
within each phase. The pitch-establishment block between Pass 1
and Pass 2 is a synchronisation barrier — Pool finishes Pass 1
across all pages, main process computes the issue pitch from the
results, then Pool starts Pass 2.

**Expected saving:** with 8 pages and 8 cores, Pass 1 + Pass 2 +
ad detection collapse close to single-page time. Estimating:
- Single-page wall ≈ (76.9 − 12.77 download) / 8 ≈ 8s
- Plus pitch-aggregation overhead: ~0.5s
- Plus download: 12.77s (already-cached short-circuit could remove
  most of this; see P9)
- **Projected total: ~22s** (vs 76.9s baseline). Saving ≈ **55s
  (71%)**, capped by the slowest individual page.

(Estimated, not measured. Speedup will be slightly less than 8× due
to fork overhead and shared MuPDF state, but this is by far the
largest available win.)

**Quality-preservation check:** byte-equal `page_meta.json` and
`page_analysis.json` across runs (serial vs parallel) for one
issue. Hash-compare column PNGs. Compare `detected_ads` row contents
(not just count) for one issue.

**Risk:** **Medium.** Three concrete risks to surface during
implementation:
1. MuPDF / PyMuPDF objects may not pickle cleanly — workers must
   open the PDF themselves rather than receive a `fitz.Document`.
2. The `LayoutDB` SQLite handle in `page_context.build_context`
   needs per-worker connections (SQLite handles aren't fork-safe).
3. The `print(...)` lines that interleave per-page progress will
   reorder — minor cosmetic regression in stdout, but if any
   downstream tooling parses stdout, it breaks.

#### P2. detect_strips: render the page once, slice strips in numpy

**Where:** `column_pipeline.py:42` (`detect_strips`) →
`find_columns.py:34` (`find_column_boundaries`) and
`find_columns.py:226` (`find_column_boundaries_morph`).

**Current cost:** 15.68s = 20.4% of wall time. Of which:
- 12.56s in `find_column_boundaries` × 56 (7 darkness strips × 8 pages)
- 3.09s in `find_column_boundaries_morph` × 16 (2 morph strips × 8 pages)

Each strip call does its own `open_clean_pdf` → `get_pixmap` for the
strip's vertical clip rectangle at 450 DPI.

**Proposed change:** at the top of `detect_strips`, render the full
page once at 450 DPI. For each strip, slice the corresponding
rows out of the full-page numpy array and pass those into
`find_column_boundaries(...)` as an already-rendered image (new
overload: `find_column_boundaries(image=..., x_pct=..., y_pct=..., ...)`).
Same for the morph variant.

**Expected saving:** 9 strip pixmaps per page → 1 page pixmap per
page. The pixmap-generation cost is roughly linear in pixels; full
page is ~10× the area of one strip but rendered once instead of 9
times. Per-page rendering work in detect_strips drops from ~9× strip
cost to 1× page cost. Conservative estimate: **8–10s saving** (over
8 pages, after subtracting the one full-page render's cost from the
9 strip renders' cost).

**Quality-preservation check:** for one page, render via current
path AND via the new path; assert `np.array_equal` on the strip
arrays. Strip-clip pixel boundaries must align with full-page pixel
rows — test that y_pct → pixel row arithmetic is identical (it
should be, since both use the same DPI).

**Risk:** **Low.** Same-DPI same-pixel slicing is byte-equivalent
in MuPDF if both clips align to integer pixel rows. Only risk is
sub-pixel rounding if the full-page render uses
`fitz.Matrix(zoom, zoom)` and the per-strip clip uses a `fitz.Rect`
clip — verify pixel alignment in the equivalence test before
shipping.

#### P3. extract_columns: render the page once, slice columns in numpy

**Where:** `split_page.py:83` (`extract_columns`).

**Current cost:** 15.16s = 19.7% of wall time. Of which:
- ~10s pixmap rendering (~7 columns × 8 pages × ~180ms per column raster)
- ~4.4s PIL PNG encoding (`_encode_tile` 4.4s, 64 calls — reasonable)
- ~0.7s remainder

Currently `extract_columns` does `page.get_pixmap(clip=col_rect, dpi=450)`
once per column, which is ~6–7 separate pixmap calls per page.

**Proposed change:** render the full page once at 450 DPI inside
`extract_columns`, then numpy-slice each column's pixel range. PIL
save still happens N times (one PNG per column), so encoding cost is
unchanged.

**Expected saving:** ~7 page pixmaps → 1 page pixmap per page. Saves
~80% of the rendering portion of extract_columns ≈ **8s saving**
(over 8 pages).

**Quality-preservation check:** hash-compare `*_col*.png` files
across baseline vs new run. Numpy slicing must produce pixel-equal
output to the current per-column clip render.

**Risk:** **Low.** The per-column ad-cutout (RGBA hole-punch) logic
operates on the numpy array, so it's actually simpler with a
pre-rendered full-page array — fewer allocation churns. One thing to
preserve: when `ads_with_ids` is supplied, columns are saved as RGBA
with ad regions punched transparent. Numpy slicing followed by
masking is equivalent.

### Tier 2 — Eliminate duplicated work (smaller, easy)

#### P4. Deduplicate `profile_page` calls

**Where:** `process_issue.py:265` and `process_issue.py:318`.

**Current cost:** 8.74s for 16 calls = 1.09s/call. The function is
deterministic given `(pdf_path, page_number)`. Both call sites pass
the same args.

**Proposed change:** in the Pass 1 boundary loop (line 317), reuse
the `prof` already computed in the ad-detection loop (line 265).
Either by hoisting profile computation into a single per-page loop
that happens first, or by caching results in `page_profiles[page_num]`
during the ad loop and looking them up in the boundary loop.

**Expected saving:** **4.37s** (50% of profile_page total).

**Quality-preservation check:** dict equality of profile output
across two calls on the same args — already true by construction,
but assert in a one-shot test.

**Risk:** **Trivial.** Pure deduplication.

### Tier 3 — Render-share for downstream layers (medium)

#### P8. Reuse cached 450-DPI render for headlines / body-text / validate

**Where:**
- `validate_columns.py:60` — renders at 75 DPI (1.57s, 8 calls)
- `detect_headlines.py:25` — renders at 150 DPI (2.08s, 8 calls)
- `detect_body_text.py:18` — renders at 300 DPI (5.21s, 8 calls)
- `detect_ads.py:201` (`detect_ads`) — renders at 150 DPI (1.96s, 8 calls)
- `detect_single_col_ads.py` (in detect_ads.py:357) — 150 DPI (1.90s, 8 calls)

**Current cost:** combined ~12.7s rendering at lower DPIs.

**Proposed change:** if P2 and P3 land, a 450-DPI full-page array
is already in memory once per page. Downsample with `cv2.resize`
(or numpy stride tricks) to feed each downstream stage at its
preferred DPI, instead of re-rendering from PDF. `cv2.resize` is
~10ms for a 4000×6000 → 1000×1500 downsample; cheaper than a fresh
MuPDF render by 100×.

**Expected saving:** **5–7s** (most of the 12.7s combined, minus
downsample cost).

**Quality-preservation check:** **This one is NOT pixel-equivalent.**
A 300-DPI MuPDF render is not pixel-identical to a downsampled
450-DPI render — MuPDF re-runs the display list at the target
resolution, which can yield different anti-aliasing, hinting, and
sub-pixel rule positions. The check has to be **behavioural**:
`detect_body_text` finds the same regions; `detect_headlines` finds
the same headlines; `validate_edge_columns` makes the same drop
decisions. Run both ways on a sample of issues, diff the JSON
outputs.

**Risk:** **Medium.** This is the only opportunity in the list
where the verification path is fuzzy. If a downsampled 450 produces
different `detect_body_text` periodicity outcomes, the saving is
either capped or unsafe. Recommendation: land P2/P3/P4/P1 first;
ship P8 separately with its own multi-issue regression run.

### Tier 4 — Conditional / edge

#### P9. Skip download when local PDF exists and validates

**Where:** `process_issue.py:62` — `subprocess.run(["curl", …])` is
unconditional even when a valid PDF already exists at `pdf_path`.

**Current cost:** 12.77s for 8 pages. (In production, presumably
runs once per issue, so this is mostly a re-run/dev-loop saving.)

**Proposed change:** before invoking curl, check if `pdf_path`
exists and starts with `%PDF-`. If so, skip download.

**Expected saving:** **~12s on every re-run of an already-downloaded
issue.** Zero saving on first run.

**Quality-preservation check:** byte-equal post-pipeline outputs vs
baseline.

**Risk:** **Low.** Only risk is staleness — if the PDF on Drive
has changed, we don't re-fetch. Mitigation: add a `--force-download`
flag for explicit re-fetch.

---

## 4. What to leave alone (and why)

### P5. `open_clean_pdf` LRU cache — DROPPED

Recon estimated 11–13×/page; actual is 18×/page but each call costs
**0.9ms** — total 0.13s. Caching wins 0.13s at the cost of memory
churn from holding `fitz.Document` objects. Not worth the complexity.

### P6. DB connection reuse — DROPPED

Recon estimated 5–6 connect/close per page would be a hotspot;
actual is 78 connections total at 0.13ms each = **0.010s**. SQLite
is local and trivial. Leave the current `with closing(...)`
pattern — it's correct and not slow.

### P7. Pass 3 outlier re-detection — DEFERRED

No outliers triggered on 1947-11-06, so no measurement available.
If P1 (parallelism) lands, Pass 3 cost becomes amortised over the
pool and is unlikely to ever be a hotspot. Revisit only if a
different issue surfaces it.

### DPI uniformity — explicitly out of scope

Per `refactor1_recommendations.md` B3: per-stage DPIs are
intentionally different (column 450, body 300, ad 150, validate 75).
Don't unify — only let P8 share a *cached* render via downsample.

### `_update_viewer_data` decomposition — separate concern

`_update_viewer_data` is 0.75s — not a perf issue. C2-remainder
(connection-context-manager rewrap) is correctness, not perf.

---

## 5. Combined potential

If Tier 1 + Tier 2 + Tier 3 all land, projected wall time on
1947-11-06:

| Phase | Baseline (s) | After Tier 1 (P1, P2, P3) | After +Tier 2 (P4) | After +Tier 3 (P8) | After +P9 (re-run) |
|---|---:|---:|---:|---:|---:|
| Total | 76.9 | ~22 | ~20 | ~16 | ~3.5 |
| Saving vs baseline | — | 71% | 74% | 79% | 95% |

(P1's projected number is the dominant lever and is estimated, not
measured. The Tier 2/3 numbers compose multiplicatively with parallelism
because each per-page worker is shorter.)

**The 79% figure assumes parallelism × shared-render × profile dedup
× downsample-cache all compose without surprise. In practice expect
2–3× of the projected to show up reliably; the rest is conditional
on the integration not introducing serialisation bottlenecks (e.g.
Python GIL on numpy slicing, fork overhead, etc.).**

---

## 6. Suggested implementation order

Each step is a small commit on its own branch with a regression run
on 1947-11-06 (and one different-era issue, e.g. 1898-10-07 or
1920-01-02) before merging. Same defensive-preservation pattern as
prior refactor work: backup DB, change, verify row counts unchanged,
push.

1. **P4 — Deduplicate `profile_page`.**
   Smallest blast radius (one cache dict in `process_issue`).
   Saves ~4.4s alone. Lands first as a confidence-builder.

2. **P9 — Skip download when local PDF valid.**
   One conditional. Useful immediately for dev iteration loops.

3. **P3 — Render-once-slice-many in `extract_columns`.**
   Self-contained; only affects column PNG output. Hash-equivalence
   test is straightforward.

4. **P2 — Render-once-slice-many in `detect_strips`.**
   Slightly riskier than P3 because `find_column_boundaries` would
   need a new "image already rendered" entry path. Still local to
   the column-detection chain.

5. **P1 — Per-page parallelism.**
   Biggest saving but biggest blast radius. Land on top of P2/P3/P4
   so each worker is already lean. Extensive regression test
   required (multiple issues, byte-compare outputs).

6. **P8 — Downsample cached render for downstream layers.**
   Last. Quality-preservation check is fuzzy (behavioural diff, not
   pixel diff). Multi-issue regression mandatory before landing.

P5, P6 stay dropped. P7 is a watch-item.

---

## 7. Notes on the audit itself

- All measurements are from a **single** cProfile run on a single
  issue. Real-world variance per page: the largest contributor
  (page rendering at 450 DPI) scales with PDF complexity; an
  ad-heavy page like 1947-11-06 P8 may render slower than a
  text-only page. The ranking is robust to this — relative ordering
  doesn't change — but absolute saving figures will move ±20% on
  other issues.
- cProfile itself adds ~5–10% overhead. The 76.9s wall time is
  *with* profiling; un-profiled wall time is closer to ~70s.
- The comparison to historical timing in `process_issue.py`'s own
  print line ("Completed in 76.9s") matches the cProfile-internal
  total — both account for profiling overhead.
- I did not measure Pass 3 outlier handling because the issue had
  zero outliers. Add a stress-test issue if P7 ever needs ranking.
