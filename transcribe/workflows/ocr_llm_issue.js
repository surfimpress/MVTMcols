export const meta = {
  name: 'ocr-llm-issue',
  description: 'OCR+LLM route: cleanup + item segmentation for one issue\'s already-rendered pages. Entity/term extraction is a separate, later, independent pass -- see transcribe/workflows/extract_terms.js',
  phases: [
    { title: 'Cleanup' },
    { title: 'Items' },
  ],
}

// args: [{ page_id, page, cleanup_prompt, items_prompt }, ...]
// Rendering (Tesseract, hOCR parsing, DB writes for pages/page_ocr_blocks,
// ticket-building) is deterministic and already done in Python before this
// runs -- see transcribe/ocr_llm.py. This workflow only pipelines the two
// LLM-judgment passes per page, which is where wall-clock and concurrency
// actually matter (90-600s per call observed, vs seconds for render).
// Ingest into the DB happens after this workflow returns, in the caller.

const CLEANUP_SCHEMA = {
  type: 'object',
  properties: {
    blocks: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'integer' },
          cleaned: { type: 'string' },
          status: { type: 'string', enum: ['clean', 'corrected', 'noise'] },
        },
        required: ['id', 'cleaned', 'status'],
      },
    },
  },
  required: ['blocks'],
}

// ocr-items only segments the page now -- no entity fields here at all.
// Entity/term extraction moved out to a separate, later, independent
// pass (transcribe/extract_terms.py + workflows/extract_terms.js) that
// reads items.full_text once this Workflow's results are ingested,
// rather than working inline off the page image + a candidate list.
// Removed 2026-08-09 (previously ENTITY_MENTION/people/organizations/
// places/products/events lived here) -- see CLAUDE.md and
// .claude/agents/term-extractor.md for the full split.
const ITEMS_SCHEMA = {
  type: 'object',
  properties: {
    items: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          label: { type: 'string' },
          // Synced 2026-08-09 to the same 11-value taxonomy as the
          // pre-1980 route's items-classifier.md -- see CLAUDE.md and
          // .claude/agents/ocr-items.md for the unification writeup.
          type: {
            type: 'string',
            enum: ['article', 'display_ad', 'classified_ad', 'notice', 'masthead',
                   'cartoon', 'letter', 'announcement', 'table', 'index', 'other'],
          },
          bbox: {
            type: 'object',
            properties: {
              x: { type: 'number' }, y: { type: 'number' },
              w: { type: 'number' }, h: { type: 'number' },
            },
            required: ['x', 'y', 'w', 'h'],
          },
          block_ids: { type: 'array', items: { type: 'integer' } },
          caption_block_ids: { type: 'array', items: { type: 'integer' } },
        },
        required: ['label', 'type', 'bbox', 'block_ids'],
      },
    },
  },
  required: ['items'],
}

const pages = Array.isArray(args) ? args : JSON.parse(args)

// Contingency for a content-filter block or other agent-call failure.
// agent() already retries transient API errors internally and returns
// null only after exhausting those (per its own contract) -- retrying
// an already-null result identically wouldn't help, so this only
// adds ONE extra attempt for a genuinely thrown error (a content-
// filter block can surface this way, and isn't always deterministic
// across an identical retry). This is a deliberately light contingency,
// not the older transcribe-issue pipeline's multi-tier reframing
// ladder -- if blocks turn out to recur often on this route, that's
// the pattern to reach for next, not this one.
const MAX_ATTEMPTS = 2

async function callWithRetry(prompt, opts) {
  let lastError = null
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    try {
      const result = await agent(prompt, opts)
      if (result) return { ok: true, result }
      return { ok: false, reason: 'agent returned no result after harness-level retries' }
    } catch (e) {
      lastError = e
    }
  }
  return { ok: false, reason: String((lastError && lastError.message) || lastError) }
}

const results = await pipeline(
  pages,
  page => callWithRetry(page.cleanup_prompt, {
    label: `cleanup:p${page.page}`, phase: 'Cleanup',
    agentType: 'ocr-cleanup', schema: CLEANUP_SCHEMA,
  }),
  async (cleanup, page) => {
    const items = await callWithRetry(page.items_prompt, {
      label: `items:p${page.page}`, phase: 'Items',
      agentType: 'ocr-items', schema: ITEMS_SCHEMA,
    })
    const problems = []
    if (!cleanup.ok) problems.push(`cleanup: ${cleanup.reason}`)
    if (!items.ok) problems.push(`items: ${items.reason}`)
    return {
      page_id: page.page_id,
      page: page.page,
      cleanup: cleanup.ok ? cleanup.result.blocks : [],
      items: items.ok ? items.result.items : [],
      // Explicit failure signal -- an empty items array on a page
      // whose agent call genuinely failed must never look the same
      // as "the agent ran fine and found nothing." The ingest side
      // (ocr_llm.py's ingest_workflow_result_data) checks this before
      // trusting an empty result as real.
      failed: problems.length > 0,
      failure_reason: problems.length ? problems.join('; ') : null,
    }
  }
)

// Every page now always produces a well-formed entry (callWithRetry
// never throws past this point, and the pipeline stage always
// returns an object) -- nothing to silently filter out anymore.
return results
