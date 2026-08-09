export const meta = {
  name: 'classify-terms',
  description: 'Independent term-type classifier: backfills org_type/place_type/product_type/event_type on entities missing them, corpus-wide',
  phases: [
    { title: 'Classify' },
  ],
}

// args: [{ entity_type, type_field, entities_path, n, prompt }, ...]
// One batch per ticket -- see transcribe/classify_terms.py build_tickets().
// Batches are fully independent (different entities, no shared state),
// so this is a flat parallel fan-out, not a pipeline -- there's nothing
// sequential to preserve. Ingest happens after this workflow returns,
// in the caller (classify_terms.py ingest-workflow-result).

const ASSIGNMENTS_SCHEMA = {
  type: 'object',
  properties: {
    assignments: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          value: { type: 'string' },
          // products only, only when grounded in a nomenclature_candidates
          // match -- see term-classifier.md's Output section.
          nomenclature_category: { type: 'string' },
          nomenclature_uri: { type: 'string' },
        },
        required: ['id', 'value'],
      },
    },
  },
  required: ['assignments'],
}

const tickets = Array.isArray(args) ? args : JSON.parse(args)

const results = await parallel(tickets.map(ticket => () =>
  agent(ticket.prompt, {
    label: `classify:${ticket.entity_type}:${ticket.n}`, phase: 'Classify',
    agentType: 'term-classifier', schema: ASSIGNMENTS_SCHEMA,
  }).then(result => ({
    entity_type: ticket.entity_type,
    type_field: ticket.type_field,
    assignments: result ? result.assignments : [],
  }))
))

return results.filter(Boolean)
