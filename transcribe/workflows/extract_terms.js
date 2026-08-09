export const meta = {
  name: 'extract-terms',
  description: 'Independent term extractor: finds every person/organization/place/product/event mentioned in already-segmented OCR+LLM-route items, corpus-wide',
  phases: [
    { title: 'Extract' },
  ],
}

// args: [{ items_path, n, prompt }, ...]
// One batch per ticket -- see transcribe/extract_terms.py build_tickets().
// Batches are fully independent (different items, no shared state), so
// this is a flat parallel fan-out, not a pipeline -- same shape as
// classify_terms.js. Ingest happens after this workflow returns, in the
// caller (extract_terms.py ingest-workflow-result).
//
// No `id` field on mentions, no candidate list, no dedup-matching
// attempt here -- see term-extractor.md. Matching is entirely
// upsert_entity's normalise_key(name) job, run at ingest time.

const MENTION = {
  type: 'array',
  items: {
    type: 'object',
    properties: {
      name: { type: 'string' },
      mention_text: { type: 'string' },
      manufacturer: { type: 'string' },
    },
    required: ['name'],
  },
}

const EXTRACTIONS_SCHEMA = {
  type: 'object',
  properties: {
    extractions: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          people: MENTION,
          organizations: MENTION,
          places: MENTION,
          products: MENTION,
          events: MENTION,
        },
        required: ['id'],
      },
    },
  },
  required: ['extractions'],
}

const tickets = Array.isArray(args) ? args : JSON.parse(args)

const results = await parallel(tickets.map(ticket => () =>
  agent(ticket.prompt, {
    label: `extract:${ticket.n}`, phase: 'Extract',
    agentType: 'term-extractor', schema: EXTRACTIONS_SCHEMA,
  }).then(result => result ? result.extractions : [])
))

// Flattened across tickets, not one-entry-per-ticket like
// classify_terms.js -- every item here already carries its own id, so
// there's no per-ticket routing metadata the ingest side needs to keep
// separate.
return results.filter(Boolean).flat()
