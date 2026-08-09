---
name: ocr-transcribe-issue
description: Run the OCR+LLM route for one issue (YYYY-MM-DD) -- 1980s+ issues that resist column detection. Renders and OCRs every page, dispatches the geometry-first cleanup+item segmentation passes via Workflow, ingests the results, and checks block coverage. Entity/term extraction is a separate, later, independent step -- see "Entity extraction" below, not part of this skill's steps.
---

# /ocr-transcribe-issue YYYY-MM-DD

Run the OCR+LLM route for one issue of the Almonte Gazette -- the
alternative to `transcribe-issue`'s column-cut pipeline, for issues
where column detection doesn't work (see `transcribe/routing.py` for
the routing rule: 1980+ by default).

This skill is the orchestrator's procedure. The deterministic work
(render, Tesseract OCR, hOCR parsing, DB writes, ticket/prompt
construction) is done by `transcribe/ocr_llm.py`, which never calls an
LLM. The two LLM passes are done by dedicated agent types
(`.claude/agents/ocr-cleanup.md`, `.claude/agents/ocr-items.md`),
dispatched via the `Workflow` tool for concurrency. The orchestrator's
job is to glue these together and verify the result.

`ocr-items` only segments the page (label/type/bbox/block_ids) --
it does not extract entities/terms. That's a separate, later,
independent step (`transcribe/extract_terms.py` +
`.claude/agents/term-extractor.md`, dispatched via
`transcribe/workflows/extract_terms.js`), decoupled in time and code
from this skill, same shape as `classify_terms.py`. Run it whenever,
over whatever's accumulated -- it isn't part of this skill's steps and
doesn't need to run right after ingest.

## Steps

1. **Check the route.** This skill assumes `transcribe.routing.
   route_for_date(year)` returns `'ocr_llm'` for the target date (the
   default cutoff is 1980). If it doesn't, stop and use
   `/transcribe-issue` instead -- don't override the routing function
   for a one-off without a specific reason (see `routing.py`'s
   docstring for what would justify an override).

2. **Render and OCR every page.** Run:
   ```
   python3 -m transcribe.ocr_llm render-issue YYYY-MM-DD
   ```
   This enumerates every page with a source PDF in `mvtm.files`
   (don't assume a page count from a prior run or from the issue's
   apparent length -- this command found 2 extra pages beyond an
   assumed 10 the first time it ran, on 2001-01-03). For each page:
   downloads the PDF (cached), renders at 300dpi, runs Tesseract with
   Sauvola thresholding + `tessdata_best`, writes `pages` +
   `page_ocr_blocks` rows, and builds both LLM tickets. Idempotent --
   already-rendered pages are skipped and their existing tickets
   reused. Writes a consolidated args file to
   `transcribe/work/ocr_llm/YYYY-MM-DD/workflow_args.json`: one
   `{page_id, page, cleanup_prompt, items_prompt}` object per page.

3. **Dispatch via Workflow.** Read the args file's contents and
   invoke:
   ```
   Workflow({
     scriptPath: "transcribe/workflows/ocr_llm_issue.js",
     args: <the args file's JSON array, as a real array>
   })
   ```
   Pass the array as an actual JSON value in the tool call, not a
   stringified blob -- a stringified `args` has crashed
   `pipeline()` before any agent ran at least once this project's
   history (the script defends against it internally, but don't rely
   on that when you control the call site). The script pipelines
   cleanup -> items per page (not a barrier across pages), so page 2's
   items pass can start while page 5's cleanup is still running.

   For large issues, consider chunking pages across a few `Workflow`
   calls rather than one huge call -- no hard limit has been hit yet,
   but this hasn't been tested past ~12 pages in one run.

4. **Save the result and ingest.** When the workflow completes, save
   its `result` array to a file (e.g.
   `transcribe/work/ocr_llm/YYYY-MM-DD/workflow_result.json`) and run:
   ```
   python3 -m transcribe.ocr_llm ingest-workflow-result <path> \
     --run-dir <the run's transcript dir, from the Workflow tool's own output> \
     --date YYYY-MM-DD --total-tokens <N> --agent-count <N> --duration-ms <N>
   ```
   The last four come straight from the completion notification's own
   `<usage>` block -- pass them through, don't recompute them (the
   token/duration total there is the harness's own trusted figure; a
   per-page reconstruction from the transcripts runs ~70-80% of it, for
   reasons not fully understood -- see `page_llm_calls` in `schema.sql`).
   `--run-dir`/`--date`/`--total-tokens`/`--agent-count`/`--duration-ms`
   are all optional as a group -- omit them to ingest results without
   usage telemetry. Idempotent either way -- pages that already have
   items ingested are skipped and reported separately, so re-running
   against a partially-ingested batch (e.g. after ingesting some pages
   by hand mid-session) is safe.

5. **Verify block coverage.** Run:
   ```
   python3 -m transcribe.ocr_llm verify-coverage YYYY-MM-DD
   ```
   An item-markup pass can legitimately drop blocks even when told not
   to (confirmed: 4/188 blocks on 2001-01-03 page 9 were never
   assigned to any item, despite the prompt's explicit "every block id
   should end up inside exactly one item" instruction). This command
   reports any gap by page and block index -- it does not auto-fix.
   For a small gap, add an honest catch-all item bundling the orphaned
   blocks' raw text (see the page 9 precedent in `CLAUDE.md`'s history
   -- don't fabricate a confident label for content you can't actually
   classify). For a large gap, re-run that page's items pass instead.

6. **Summarise.** How many pages, items; any coverage gaps found and
   how they were resolved; anything that needed `--force` on
   `render-issue` (routing override) and why. Entity/term counts don't
   belong here -- that's a separate step's summary, see below.

## Entity extraction (separate step, run whenever)

Once items exist (step 4 above), run term extraction independently --
not tied to this skill's completion, can run against this issue's
items alongside any other already-segmented issue's pending items:

```
python3 -m transcribe.extract_terms build
```
Then dispatch `Workflow({ scriptPath: "transcribe/workflows/extract_terms.js",
args: <workflow_args.json's contents> })`, save the result, and run:
```
python3 -m transcribe.extract_terms ingest-workflow-result <path>
```
`items.terms_extracted_at` is the readiness signal -- `build` only
picks up items that don't have it set yet, so re-running `build` later
(after more issues are ingested) only processes what's actually new.

## Not yet built (unlike `transcribe-issue`)

- No content-filter-block escalation ladder. If an `ocr-cleanup`,
  `ocr-items`, or `term-extractor` agent call is blocked by
  Anthropic's safety classifier, there is no Tier 2/3/4 retry path
  here yet -- surface it and decide case by case. Worth building if it
  recurs; not built preemptively.
- No Haiku/Sonnet comparison mode.
- No download cleanup step (`transcribe-issue`'s step 5) -- rendered
  pages and their tickets stay under `transcribe/work/ocr_llm/` and
  aren't disk-managed yet.

## Monitor

`transcribe/monitor.html` shows live pages/items/entity counts
and per-issue block-coverage gaps. It reads
`transcribe/monitor.json` only -- it never queries the database,
and the JSON is kept fresh by a LaunchAgent
(`com.mvtm.ocr_llm_stats`, installed from `tools/`) on its own 60s
loop, independent of whether a Workflow is running. No manual refresh
needed; just open the page.
