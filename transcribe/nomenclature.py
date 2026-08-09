"""Query helpers for Nomenclature for Museum Cataloging
(nomenclature.info) -- a controlled vocabulary for museum object
types, published as SKOS via a public SPARQL endpoint.

Not this project's own vocabulary: Nomenclature catalogs durable
museum-collectible object types (tools, equipment, structures,
documents, recreational objects...) across a fixed 10-category, up-
to-6-level hierarchy (Category > Class > Subclass > Primary/Secondary/
Tertiary Term). It does NOT cover perishables/retail groceries ("Apples"
has no Nomenclature term) -- those stay on our own organic
product_type taxonomy. Where a product genuinely matches something
Nomenclature catalogs (a physical good, a document, a piece of
equipment), prefer it and record the match; this module only does the
deterministic SPARQL lookup, same split as the rest of transcribe/ --
the judgment call ("is this candidate an obvious match?") is the
term-classifier agent's job, not this module's.

Endpoint discovered 2026-08-09 by inspecting the interactive query UI
at https://nomenclature.info/sparql/index.do -- the UI's own YASQE
editor posts to this REST path; it accepts a plain `query` GET param
and returns application/sparql-results+json, no auth needed for reads.

Usage::

    from transcribe import nomenclature
    nomenclature.search_terms("baking powder")
    # -> [{"uri": ..., "label": "baking powder", "category": "...", "path": [...]}, ...]
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

# Confirmed 2026-08-09 against nomenclature.info's own about page: the
# live site is a continuously-updated superset of the 2015 print
# edition, which is the last one with a version number ("Nomenclature
# 4.0" -- superseding "Revised Nomenclature" 1988 and the original
# 1978 edition). This is what goes in products.external_terminology.
TERMINOLOGY_NAME = "Nomenclature 4.0"

SPARQL_ENDPOINT = "https://nomenclature.info/sparql/rest/sparql/nom"
_TIMEOUT_S = 20

_PREFIXES = "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"


def _query(sparql: str) -> list[dict]:
    """Run a SPARQL SELECT, return the results.bindings list with each
    binding's values flattened to plain strings (dropping the SPARQL
    JSON's {"type","value"} wrapper)."""
    url = SPARQL_ENDPOINT + "?" + urllib.parse.urlencode({"query": _PREFIXES + sparql})
    req = urllib.request.Request(url, headers={"Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.load(resp)
    rows = []
    for binding in data["results"]["bindings"]:
        rows.append({k: v["value"] for k, v in binding.items()})
    return rows


def _escape_literal(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def broader_chain(uri: str, max_depth: int = 6) -> list[str]:
    """Walk skos:broader from `uri` up to the root, return labels
    root-first (e.g. ["Category 08: Communication Objects",
    "Documentary Objects", "Other Documents"]). Stops at max_depth to
    guard against an unexpected cycle."""
    chain = []
    current = uri
    for _ in range(max_depth):
        rows = _query(f"""
            SELECT ?broader ?label WHERE {{
              <{current}> skos:broader ?broader .
              ?broader skos:prefLabel ?label .
              FILTER(LANG(?label) = "en")
            }} LIMIT 1
        """)
        if not rows:
            break
        chain.append(rows[0]["label"])
        current = rows[0]["broader"]
    return list(reversed(chain))


def search_terms(name: str, limit: int = 5) -> list[dict]:
    """Find Nomenclature concepts whose prefLabel plausibly matches
    `name`. Exact (case-insensitive) match only, with a singular-form
    fallback ("Snowshoes" -> "snowshoe") since Nomenclature prefers
    singular labels and our product names are often plural.

    Deliberately does NOT fall back to a substring/CONTAINS search.
    An earlier version tried a first-significant-word CONTAINS
    fallback; tested against the live corpus 2026-08-09 in an
    unattended batch run and it was a real precision failure --
    "Coal Oil" matched "charcoal", "Comfort Soap" matched "comfort
    station" (a washroom), "Honey" matched "honey extractor" (a tool).
    A shared word is not a shared meaning. Precision over recall here:
    returning nothing for a name Nomenclature doesn't have an exact
    term for is the correct, expected outcome (see module docstring
    on perishables/groceries), not a gap to paper over with a fuzzy
    guess.

    Returns [{"uri", "label", "top_category", "path"}, ...] -- `path`
    is the full broader-chain from search_terms' single extra query
    per candidate, root-first; `top_category` is path[0] when present.
    """
    stripped = name.strip()
    candidates_to_try = [stripped]
    if stripped.lower().endswith("es") and len(stripped) > 4:
        candidates_to_try.append(stripped[:-2])
    if stripped.lower().endswith("s") and not stripped.lower().endswith("ss"):
        candidates_to_try.append(stripped[:-1])

    rows = []
    for candidate in candidates_to_try:
        lname = _escape_literal(candidate)
        rows = _query(f"""
            SELECT DISTINCT ?s ?label WHERE {{
              ?s skos:prefLabel ?label .
              FILTER(LANG(?label) = "en")
              FILTER(LCASE(STR(?label)) = LCASE("{lname}"))
            }} LIMIT {limit}
        """)
        if rows:
            break

    out = []
    for r in rows:
        path = broader_chain(r["s"])
        out.append({
            "uri": r["s"], "label": r["label"],
            "top_category": path[0] if path else None,
            "path": path,
        })
    return out
