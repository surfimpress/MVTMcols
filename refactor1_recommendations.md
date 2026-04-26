# Refactor 1 — Recommendations

This document lists ONLY low-risk simple improvements drawn from the baseline audit. Each item has a category, a one-line rationale, and a risk note. Performance, architecture, and large refactors are deliberately out of scope.

Risk levels used below:
- **Trivial** — pure cleanup, won't change runtime behaviour, very small blast radius
- **Low** — a real change but the affected surface is a single helper or constant
- **Caution** — touches behaviour-bearing code or schema; needs verification before action

> **Audit-correction notice (added after the fact).**
> Sections A, B, C below contain a small number of items I asserted on the basis of a quick read rather than verification. Each affected item is left in place and followed by a `↳ Correction` paragraph that states what I got wrong, what is actually true, and what would have caught the error in one tool call. The point of leaving the wrong text visible is to make the gap between "looks plausible from a name" and "verified from the code" inspectable, so future audits start by *grepping* and not by *recognising*.

## A. Removal of code no longer used

| # | Item | Where | Risk |
|---|---|---|---|
| A1 | Suspect dev artefact at repo root: `working_notes_profile_review.md` | repo root | Trivial — confirm with user, then move to `docs/` or delete |
| A2 | `screenshots/` untracked dir at repo root | repo root | Trivial — likely dev-only; consider `.gitignore` |
| A3 | DB columns `files.incorrect_date`, `files.likely_date` never SELECTed | `data/mvtm.db` | **Caution** — schema changes touch a 142K-row table; safer to leave or document as deprecated than ALTER |
| A4 | DB column `detected_ads.aspect` may duplicate `rect_ratio` | `data/mvtm.db` | **Caution** — verify by inspecting `store_ads` in `detect_ads.py` and viewer JS before any drop |
| A5 | Possible commented-out helper `_vl()` at `process_issue.py:750` | code | Trivial — read the comment, decide |
| A6 | Stage-2 standalone scripts not called by orchestrator: `find_splits.py`, `classify_segments.py`, `four_probe_v5.py`, `crop_pdf.py` | code | **Do NOT remove** — these are stage-2 / dev tools, intentionally standalone. Listed here only so we know to leave them alone. |

↳ **Correction to A4.** `detected_ads.aspect` is **not** a duplicate of `rect_ratio`. They are independent quantities: `rect_ratio` is contour-area ÷ bounding-rect-area (a *rectangularity* score), while `aspect` is bounding-rect width ÷ height (a *shape* ratio). `aspect` has at least twelve live uses in `detect_ads.py`, including the primary admission gates `0.3 < aspect < 5.0` (line 130) and `0.2 < aspect < 8.0` (line 132), and is persisted by `store_ads` for downstream filtering. **Why I got this wrong:** the two columns sit next to each other in the schema and both are floats in roughly the same range, so they *look* duplicative when scanning a `CREATE TABLE`. **What would have caught it in one tool call:** `grep -n "aspect" detect_ads.py` would have shown the gate expressions immediately; reading a CREATE TABLE in isolation is not enough to claim a column is unused. Strike A4 from the action list.

## B. Centralization of variables

| # | Item | Where it appears | Risk |
|---|---|---|---|
| B1 | `_open_clean(pdf_path)` defined identically in **5 files**: `find_columns.py:33`, `detect_ads.py:26`, `detect_sliver.py:27`, `page_profile.py:26`, `crop_pdf.py:34` | scattered | Low — pure function, no state. Move to a shared `pdf_utils.py` (or add to `coordinates.py`); have all five files import. **Verify** that the red-overlay-strip behaviour really is identical character-for-character before consolidating. |
| B2 | `CONSENSUS_ROWS = [3, 4, 5, 6, 7, 8, 9]` and `STRIP_WEIGHTS` duplicated in `column_pipeline.py:24` and `split_page.py:46` | strip detection | Low — single owner needed. Move to `column_pipeline.py`; have `split_page` import. |
| B3 | DPI defaults scattered: `dpi=450` (column_pipeline, find_columns, process_issue, crop_pdf), `dpi=300` (detect_body_text), `dpi=150` (detect_ads, page_profile, detect_headlines) | many | **Caution** — these are deliberate per-stage choices, not bugs. Don't unify; just **document** in a central `dpi.py` constants file (e.g. `COLUMN_DPI=450`, `AD_DPI=150`, `PROFILE_DPI=150`, `BODY_TEXT_DPI=300`) and have call sites import. |
| B4 | Parameter name inconsistency for the same concept: `dpi`, `render_dpi`, `profile_dpi` | several | Trivial-but-tedious — leave alone unless we touch the function for another reason. Renaming touches every call site. |
| B5 | Magic numbers `0.35` (drop threshold) appear in `detect_headlines.py:315`, `find_columns.py:201`, `detect_sliver.py:162` | several | **Caution** — *probably* coincidental rather than a shared concept; verify each meaning before centralizing. |

↳ **Corrections to B.**
> - **B2 line numbers stale.** `STRIP_WEIGHTS` in `split_page.py` is at line **110**, not 46. `CONSENSUS_ROWS` in `split_page.py` is at line **45**, not 46. Pre-existing drift compounded by the D4/D7-D11 cleanup commits (line removals shifted everything below).
> - **B5 missing occurrence.** `validate_columns.py:28` defines `EDGE_INK_RATIO_THRESHOLD = 0.35` — a fifth place the literal appears, missed by the original scan. The scan also captured `find_splits.py:609` and `four_probe_v5.py:470, 775, 786, 798` and `detect_headlines.py:314` (not 315). The recommendation's *spirit* (these are probably coincidental, don't rush to consolidate) is unchanged, but the inventory was incomplete.
>
> **Why I got this wrong:** I treated the original audit's claimed line numbers as gospel and didn't re-grep before transcribing them into the recommendations doc. After several intermediate commits had moved lines around, every line number was suspect, and I checked none of them. **What would have caught it in one tool call:** `grep -n` on each pattern at the moment of writing the doc, not at the moment of the original audit. Treat any line number older than the most recent commit as stale until re-verified.

## C. Reuse of common functions

| # | Item | Where | Risk |
|---|---|---|---|
| C1 | The exact PDF-render-to-greyscale pattern (`fitz.open` → `get_pixmap` → `np.frombuffer.reshape` → `cv2.cvtColor`) appears in at least `detect_ads.py`, `validate_columns.py`, `find_columns.py`, `detect_sliver.py`, `detect_body_text.py` | scattered | Low — pure boilerplate; collapse into one helper `render_grey(pdf_path, page_number, dpi)`. **Verify** byte-for-byte equivalence on one issue before swapping in (different files use slightly different pixmap modes or alpha settings). |
| C2 | `sqlite3.connect(db_path)` opened directly in 4 places without context-manager pattern: `process_issue.py:42`, `detect_ads.py:891,937`, `split_page.py:1179` (only `LayoutDB` uses a context manager) | scattered | Low — wrap each in `with sqlite3.connect(...) as conn:` for connection-leak safety. Each call site is self-contained, so changes are local. |

↳ **Correction to C2.** Two errors in the same row:
> 1. **"Only `LayoutDB` uses a context manager"** is false. `LayoutDB._conn()` returns a fresh `sqlite3.connect(...)` and assigns it to a local `conn`; no `with` statement wraps it anywhere in `layout_intelligence.py`. The connections are closed manually (or relied on to be GC'd). So the *baseline* C2 claims is wrong: there is no callsite in the project that uses `with sqlite3.connect(...) as conn:`. The recommendation (wrap in `with`) is still good — but it applies to **every** callsite including all 11 inside `LayoutDB`, not just the four originally listed.
> 2. **Missed callsite + stale lines.** Current state by grep: `process_issue.py:40` and `:251`, `detect_ads.py:890,936`, `split_page.py:1176`, plus 11 inside `layout_intelligence.py`. The originally-listed lines were also drifted by 1–3 from the post-D4/D7-D11 state. The `process_issue.py:251` callsite (`_conn = __import__("sqlite3").connect(db_path)`) was missed entirely because the dynamic import obscures the pattern from a casual grep.
>
> **Why I got this wrong:** I treated `_conn()` as if its name implied context-management, and I didn't read the body of `LayoutDB` before claiming it was the well-behaved exception. The dynamic import in `process_issue.py:251` was missed because I grepped for `sqlite3.connect` literally and the module was loaded via `__import__`. **What would have caught it in one tool call:** `grep -n "sqlite3" *.py` (broader pattern) plus actually reading three lines of `layout_intelligence._conn`. The lesson is bigger than C2: when a recommendation says "only X is correct," verify that X is correct before relying on it as a safe baseline.

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
