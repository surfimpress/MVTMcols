export const meta = {
  name: 'ocr-llm-issue',
  description: 'OCR+LLM route: cleanup + item/entity markup for one issue\'s already-rendered pages',
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

// mention_text and manufacturer were being silently dropped -- the agent
// prompt (ocr-items.md) asks for them but this schema never declared
// them, so structured-output validation stripped them before ingest ever
// saw them. Fixed 2026-08-09 alongside the item_type enum sync below.
const ENTITY_MENTION = {
  type: 'array',
  items: {
    type: 'object',
    properties: {
      id: { type: ['string', 'null'] },
      name: { type: 'string' },
      mention_text: { type: 'string' },
      manufacturer: { type: 'string' },
    },
    required: ['name'],
  },
}

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
          people: ENTITY_MENTION,
          organizations: ENTITY_MENTION,
          places: ENTITY_MENTION,
          products: ENTITY_MENTION,
          events: ENTITY_MENTION,
        },
        required: ['label', 'type', 'bbox', 'block_ids'],
      },
    },
  },
  required: ['items'],
}

const pages = Array.isArray(args) ? args : JSON.parse(args)

const results = await pipeline(
  pages,
  page => agent(page.cleanup_prompt, {
    label: `cleanup:p${page.page}`, phase: 'Cleanup',
    agentType: 'ocr-cleanup', schema: CLEANUP_SCHEMA,
  }),
  (cleanupResult, page) => agent(page.items_prompt, {
    label: `items:p${page.page}`, phase: 'Items',
    agentType: 'ocr-items', schema: ITEMS_SCHEMA,
  }).then(itemsResult => ({
    page_id: page.page_id,
    page: page.page,
    cleanup: cleanupResult ? cleanupResult.blocks : [],
    items: itemsResult ? itemsResult.items : [],
  }))
)

return results.filter(Boolean)
