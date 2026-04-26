# Refactor 1 — Recommendations

This document lists ONLY low-risk simple improvements drawn from the baseline audit. Each item has a category, a one-line rationale, and a risk note. Performance, architecture, and large refactors are deliberately out of scope.

Risk levels used below:
- **Trivial** — pure cleanup, won't change runtime behaviour, very small blast radius
- **Low** — a real change but the affected surface is a single helper or constant
- **Caution** — touches behaviour-bearing code or schema; needs verification before action

## A. Removal of code no longer used

| # | Item | Where | Risk |
|---|---|---|---|
| A1 | Suspect dev artefact at repo root: `working_notes_profile_review.md` | repo root | Trivial — confirm with user, then move to `docs/` or delete |
| A2 | `screenshots/` untracked dir at repo root | repo root | Trivial — likely dev-only; consider `.gitignore` |
| A3 | DB columns `files.incorrect_date`, `files.likely_date` never SELECTed | `data/mvtm.db` | **Caution** — schema changes touch a 142K-row table; safer to leave or document as deprecated than ALTER |
| A4 | DB column `detected_ads.aspect` may duplicate `rect_ratio` | `data/mvtm.db` | **Caution** — verify by inspecting `store_ads` in `detect_ads.py` and viewer JS before any drop |
| A5 | Possible commented-out helper `_vl()` at `process_issue.py:750` | code | Trivial — read the comment, decide |
| A6 | Stage-2 standalone scripts not called by orchestrator: `find_splits.py`, `classify_segments.py`, `four_probe_v5.py`, `crop_pdf.py` | code | **Do NOT remove** — these are stage-2 / dev tools, intentionally standalone. Listed here only so we know to leave them alone. |

## B. Centralization of variables

| # | Item | Where it appears | Risk |
|---|---|---|---|
| B1 | `_open_clean(pdf_path)` defined identically in **5 files**: `find_columns.py:33`, `detect_ads.py:26`, `detect_sliver.py:27`, `page_profile.py:26`, `crop_pdf.py:34` | scattered | Low — pure function, no state. Move to a shared `pdf_utils.py` (or add to `coordinates.py`); have all five files import. **Verify** that the red-overlay-strip behaviour really is identical character-for-character before consolidating. |
| B2 | `CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]` and `STRIP_WEIGHTS` duplicated in `column_pipeline.py:24` and `split_page.py:46` | strip detection | Low — single owner needed. Move to `column_pipeline.py`; have `split_page` import. |
| B3 | DPI defaults scattered: `dpi=450` (column_pipeline, find_columns, process_issue, crop_pdf), `dpi=300` (detect_body_text), `dpi=150` (detect_ads, page_profile, detect_headlines) | many | **Caution** — these are deliberate per-stage choices, not bugs. Don't unify; just **document** in a central `dpi.py` constants file (e.g. `COLUMN_DPI=450`, `AD_DPI=150`, `PROFILE_DPI=150`, `BODY_TEXT_DPI=300`) and have call sites import. |
| B4 | Parameter name inconsistency for the same concept: `dpi`, `render_dpi`, `profile_dpi` | several | Trivial-but-tedious — leave alone unless we touch the function for another reason. Renaming touches every call site. |
| B5 | Magic numbers `0.35` (drop threshold) appear in `detect_headlines.py:315`, `find_columns.py:201`, `detect_sliver.py:162` | several | **Caution** — *probably* coincidental rather than a shared concept; verify each meaning before centralizing. |

## C. Reuse of common functions

| # | Item | Where | Risk |
|---|---|---|---|
| C1 | The exact PDF-render-to-greyscale pattern (`fitz.open` → `get_pixmap` → `np.frombuffer.reshape` → `cv2.cvtColor`) appears in at least `detect_ads.py`, `validate_columns.py`, `find_columns.py`, `detect_sliver.py`, `detect_body_text.py` | scattered | Low — pure boilerplate; collapse into one helper `render_grey(pdf_path, page_number, dpi)`. **Verify** byte-for-byte equivalence on one issue before swapping in (different files use slightly different pixmap modes or alpha settings). |
| C2 | `sqlite3.connect(db_path)` opened directly in 4 places without context-manager pattern: `process_issue.py:42`, `detect_ads.py:891,937`, `split_page.py:1179` (only `LayoutDB` uses a context manager) | scattered | Low — wrap each in `with sqlite3.connect(...) as conn:` for connection-leak safety. Each call site is self-contained, so changes are local. |

## D. Best practices (only obvious ones)

| # | Item | Where | Risk |
|---|---|---|---|
| D1 | Bare `except:` clauses (if any) — needs a grep pass to confirm | TBD | Trivial fix, but **verify** there aren't intentional swallow-all clauses |
| D2 | `print(...)` calls in detection modules used as logging — fine for CLI use, mixes with library use | `detect_ads.py`, `detect_headlines.py`, `find_columns.py`, `classify_segments.py` | **Don't change** unless we adopt project-wide logging; switching `print` → `logging` is a larger change than scoped here |
| D3 | Mutable-default-arg risks (`def f(x=[])`) — needs a grep pass | TBD | Trivial fix |
| D4 | Unused imports — needs a `pyflakes` / `ruff` pass | TBD | Trivial fix; safe |

## Risk assessment

**Cross-cutting risks to be aware of in any refactor:**

1. **Shared PDF helpers** (`_open_clean`, render-to-grey) — five copies exist because each detector evolved independently. Before consolidating, render the same page through each version and `numpy.array_equal` the results. A subtle difference in pixmap mode (RGB vs RGBA, or `pix.n` handling) will silently change downstream contour results.

2. **DPI constants** — different stages use different DPIs intentionally (column detection at 450, ad detection at 150). Centralizing is fine but **uniforming** is not. Use named constants like `COLUMN_DPI`, `AD_DPI`, never a single `DEFAULT_DPI`.

3. **`page_layouts.confidence` and other stored derived values** — if any centralization changes how a constant is computed, persisted DB rows will be inconsistent with newly-written ones. Check the populate path before changing the input constant.

4. **Dynamic imports** in `process_issue.py` — `LayoutDB`, `validate_columns`, `detect_headlines`, `detect_body_text` are imported inside functions, not at module top. A static-analysis tool may report them as unused; don't believe it.

5. **No commit on this branch should land without a behaviour-equivalence rerun.** Re-run the same four sample issues (1898-10-07, 1920-01-02, 1937-01-14, 1947-11-06) and diff `detected_ads` and `page_layouts` row counts pre/post each cleanup commit. The plumbing for that is already proven.

## Defensive preservation — recap for any future refactor commit

For each subsequent commit in this refactor branch (e.g. B1–C2 from the table above), repeat this pattern:
- Snapshot anything non-git that's about to change (DB → `data/mvtm.db.pre-<step>.bak`).
- Make ONE small focused change. Don't batch.
- Run the four-issue verification.
- Commit + push immediately. Don't accumulate.
- If the machine dies, only the in-flight unit is at risk.

## Opportunities (low-effort signals, no deep dive)

These came up while scanning. Listing for completeness; user can choose what to investigate.

1. **`coordinates.py` is only used by `detect_body_text.py`.** Either expand its use across the other detectors (good) or fold it into the consumer (also fine). Either way, current state is asymmetric.

2. **`page_meta.json` and `page_analysis.json` overlap.** Both per-page; both contain column data. If they were merged, one fewer file per page. (Out of scope here, but flag for later.)

3. **`viewer.py` has a hardcoded `BASE = '/MVTM/columns'` (mirrored in `page_viewer.html`).** Single source for that base URL would prevent drift if hosting moves.

4. **`page_splits` table has 21 rows vs `page_layouts` 281.** This is suspicious — either we stopped logging to it or it's only populated on a debug path. Worth one query to confirm intent.

5. **`store_ads` returns ids; `record_layout` could too.** Symmetry would let viewer-data assembly drop a SELECT.

6. **`detected_ads.cols` vs `page_layouts.num_columns`** — same concept, different name. Pick one if/when either schema is touched for another reason.

7. **`instructions/` directory** — present in the working tree but not audited here. May contain useful prior decisions, may contain stale notes.

8. **Recently-added Tier 1 contrast threshold (`< 145`)** — magic number in `detect_ads.py`. Currently hard-coded. A natural candidate to lift into a constants block alongside the strict/loose param sets, with a one-line comment about empirical derivation.
