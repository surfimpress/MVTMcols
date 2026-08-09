"""Token-efficient entity candidate-list prefetch.

One bulk SELECT per call, not one lookup per mention. An item-markup
or entity-extraction LLM pass has no DB access anyway (agents don't
call SQL) -- the orchestrator prefetches a compact candidate list
once per page/issue and embeds it in the ticket, and the agent
matches mentions against it *textually*: "this name is already
entity <id>" or "this is new". ingest-side upsert_entity() still owns
the actual dedup decision; the candidate list is a hint that saves
the agent from re-inventing an entity the corpus already knows about,
not a substitute for the real dedup key.

People are filtered to a year-window around the target issue's date
-- a person active in 1880 won't appear in a 1959 issue (see the
entity-registry discussion in this project's history: initials/
children-of-same-name collisions are the risk this guards against).
Organizations/places/products/events aren't filtered: they persist
across decades, and at current corpus scale (under 150 rows each)
filtering them buys nothing and risks hiding a genuinely long-lived
match. Revisit the no-filter choice if those tables grow enough that
list size (not query speed) becomes the real cost -- see MAX_CANDIDATES.
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


def people_candidates(conn: sqlite3.Connection, year: int,
                       window: int = PEOPLE_YEAR_WINDOW) -> list[dict]:
    """{id, name} for people plausibly active near `year`.

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

    out = []
    for r in rows:
        if r["first_seen_date"] is None:
            out.append({"id": r["id"], "name": r["full_name"]})
            continue
        fy = int(r["first_seen_date"][:4])
        ly = int((r["last_seen_date"] or r["first_seen_date"])[:4])
        if ly >= lo and fy <= hi:
            out.append({"id": r["id"], "name": r["full_name"]})
    return _cap(out, "people")


def _unfiltered_candidates(conn: sqlite3.Connection, table: str,
                            name_col: str = "name") -> list[dict]:
    rows = conn.execute(f"SELECT id, {name_col} AS name FROM {table}").fetchall()
    return _cap([{"id": r["id"], "name": r["name"]} for r in rows], table)


def _cap(candidates: list[dict], label: str) -> list[dict]:
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
     "products": [...], "events": [...]}
    """
    return {
        "people": people_candidates(conn, year, window),
        "organizations": _unfiltered_candidates(conn, "organizations"),
        "places": _unfiltered_candidates(conn, "places"),
        "products": _unfiltered_candidates(conn, "products"),
        "events": _unfiltered_candidates(conn, "events"),
    }
