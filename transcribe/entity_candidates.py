"""Token-efficient entity candidate-list prefetch.

One bulk SELECT per call, not one lookup per mention. An item-markup
or entity-extraction LLM pass has no DB access anyway (agents don't
call SQL) -- the orchestrator prefetches a compact candidate list
once per page/issue and embeds it in the ticket, and the agent
matches mentions against it *by name text*: if a mention matches a
candidate, reuse that candidate's exact spelling. There is no id in
this wire format -- ingest-side upsert_entity() (in
ingest_item_result.py) dedups purely on normalise_key(name), never on
any id the agent might have referenced, so an id here would only be
decorative. Reusing a known spelling is what actually merges a
mention into the right entity; the candidate list is a hint that
saves the agent from re-inventing an entity the corpus already knows
about under a different spelling, not a substitute for the real
dedup key.

People are filtered to a year-window around the target issue's date
-- a person active in 1880 won't appear in a 1959 issue (see the
entity-registry discussion in this project's history: initials/
children-of-same-name collisions are the risk this guards against).
Organizations/places/products/events aren't date-filtered: they
persist across decades (a town or long-lived business name doesn't
retire), and excluding one by date risks silently minting a
duplicate, which is worse than the token cost of including it. When a
list is too long to send in full, it's truncated by recency instead
(see _sort_by_recency/MAX_CANDIDATES) rather than dropped by an
arbitrary DB-order slice.
"""

from __future__ import annotations

import sqlite3

PEOPLE_YEAR_WINDOW = 40  # years either side of the target issue's year
MAX_CANDIDATES = 500  # soft cap per list; truncation is logged, never silent


def _decade_prefixes(lo_year: int, hi_year: int) -> list[str]:
    """Decade-bucket prefixes (first 3 digits of the year, e.g. 1930s
    -> '193') covering every decade that overlaps [lo_year, hi_year].
    Matches the idx_people_decade expression index in schema.sql."""
    lo_decade = (lo_year // 10) * 10
    hi_decade = (hi_year // 10) * 10
    return [str(d)[:3] for d in range(lo_decade, hi_decade + 1, 10)]


def _sort_by_recency(rows: list[dict]) -> list[dict]:
    """Order candidate rows so a forced cap drops the least-recently-
    seen long tail first, not an arbitrary DB-order slice. Rows with
    no first_seen_date yet (never linked to a mention) are always
    kept first/protected, ahead of the recency sort -- excluding them
    would make them permanently unmatchable by any future
    candidate-list lookup, since nothing else would ever reintroduce
    them as a candidate.
    """
    protected = [r for r in rows if r["first_seen_date"] is None]
    dated = [r for r in rows if r["first_seen_date"] is not None]
    dated.sort(key=lambda r: r["last_seen_date"] or r["first_seen_date"], reverse=True)
    return protected + dated


def people_candidates(conn: sqlite3.Connection, year: int,
                       window: int = PEOPLE_YEAR_WINDOW) -> list[str]:
    """Names of people plausibly active near `year`.

    Uses the decade index as a coarse pre-filter (cheap, index-backed),
    then a precise year-range check in Python. People with no
    first_seen_date yet (not linked to any mention) are always
    included -- excluding them would make them permanently unmatchable
    by any future candidate-list lookup.
    """
    lo, hi = year - window, year + window
    prefixes = _decade_prefixes(lo, hi)
    placeholders = ",".join("?" * len(prefixes))
    rows = conn.execute(
        f"SELECT id, full_name, first_seen_date, last_seen_date FROM people "
        f"WHERE first_seen_date IS NULL "
        f"OR substr(first_seen_date, 1, 3) IN ({placeholders})",
        prefixes,
    ).fetchall()

    matched = []
    for r in rows:
        if r["first_seen_date"] is None:
            matched.append({"name": r["full_name"],
                             "first_seen_date": None, "last_seen_date": None})
            continue
        fy = int(r["first_seen_date"][:4])
        ly = int((r["last_seen_date"] or r["first_seen_date"])[:4])
        if ly >= lo and fy <= hi:
            matched.append({"name": r["full_name"],
                             "first_seen_date": r["first_seen_date"],
                             "last_seen_date": r["last_seen_date"]})
    ordered = _sort_by_recency(matched)
    return _cap([r["name"] for r in ordered], "people")


def all_rows(conn: sqlite3.Connection, table: str,
             name_col: str = "name") -> list[dict]:
    """Every row from an entity table, uncapped --
    {id, name, first_seen_date, last_seen_date}. Base helper: shared
    by _unfiltered_candidates below (which adds the prompt-size cap)
    and build_entities_stats.py (the full browsing index, which wants
    everything including real ids, not a prompt-sized sample)."""
    rows = conn.execute(
        f"SELECT id, {name_col} AS name, first_seen_date, last_seen_date "
        f"FROM {table}"
    ).fetchall()
    return [
        {"id": r["id"], "name": r["name"],
         "first_seen_date": r["first_seen_date"], "last_seen_date": r["last_seen_date"]}
        for r in rows
    ]


def _unfiltered_candidates(conn: sqlite3.Connection, table: str,
                            name_col: str = "name") -> list[str]:
    rows = all_rows(conn, table, name_col)
    ordered = _sort_by_recency(rows)
    return _cap([r["name"] for r in ordered], table)


def _cap(candidates: list[str], label: str) -> list[str]:
    if len(candidates) > MAX_CANDIDATES:
        print(f"entity_candidates: {label} list truncated "
              f"{len(candidates)} -> {MAX_CANDIDATES}; consider narrowing "
              f"the filter for this table if this recurs")
        return candidates[:MAX_CANDIDATES]
    return candidates


def build_candidate_lists(conn: sqlite3.Connection, year: int,
                           window: int = PEOPLE_YEAR_WINDOW) -> dict:
    """One dict, one round of queries, ready to embed in an LLM ticket:
    {"people": [...], "organizations": [...], "places": [...],
     "products": [...], "events": [...]} -- each a flat list of name
    strings, no ids (see module docstring for why).
    """
    return {
        "people": people_candidates(conn, year, window),
        "organizations": _unfiltered_candidates(conn, "organizations"),
        "places": _unfiltered_candidates(conn, "places"),
        "products": _unfiltered_candidates(conn, "products"),
        "events": _unfiltered_candidates(conn, "events"),
    }
