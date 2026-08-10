export const meta = {
  name: 'reconcile-terms',
  description: 'Unit 4b: LLM-based entity-matching tier, catching spelling/abbreviation duplicates the Python heuristics in terminology_cleanup.py structurally can\'t (no shared first character, no substring relationship)',
  phases: [
    { title: 'Reconcile' },
  ],
}

// args: [{ entity_type, candidates_path, n, prompt }, ...]
// One batch per ticket, one ticket per entity type -- see
// transcribe/reconcile_terms.py build_tickets(). Batches are fully
// independent (different entity types, no shared state), so this is a
// flat parallel fan-out, not a pipeline -- same shape as
// classify_terms.js/extract_terms.js. Ingest happens after this workflow
// returns, in the caller (reconcile_terms.py ingest-workflow-result).

const MATCHES_SCHEMA = {
  type: 'object',
  properties: {
    matches: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id_a: { type: 'string' },
          id_b: { type: 'string' },
          confidence: { type: 'number' },
          rationale: { type: 'string' },
        },
        required: ['id_a', 'id_b'],
      },
    },
  },
  required: ['matches'],
}

const tickets = Array.isArray(args) ? args : JSON.parse(args)

const results = await parallel(tickets.map(ticket => () =>
  agent(ticket.prompt, {
    label: `reconcile:${ticket.entity_type}:${ticket.n}`, phase: 'Reconcile',
    agentType: 'term-reconciler', schema: MATCHES_SCHEMA,
  }).then(result => ({
    entity_type: ticket.entity_type,
    matches: result ? result.matches : [],
  }))
))

return results.filter(Boolean)
