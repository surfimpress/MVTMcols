"""Ingest one items-classifier result back into transcribe.db.

The orchestrator (Claude Code) saves the agent's JSON envelope for a
page to ``transcribe/work/results/<page-id>.json`` and then runs::

    python3 -m transcribe.ingest_item_result <page-id>

where ``<page-id>`` is the ticket's stem (e.g. ``1912-12-27_p6``).

The envelope shape is::

    {
      "items": [
        {
          "item_type": "article|display_ad|classified_ad|notice|masthead|
                        cartoon|letter|announcement|table|index|other",
          "headline": "..." | null,
          "byline":   "..." | null,
          "summary":  "...",
          "language": "en",
          "classification_confidence": 0.0..1.0,
          "column_spans": [                             # zero or more
            {"column_transcript_id": "uuid",
             "sequence": 0,
             "start_offset": int,
             "end_offset":   int}
          ],
          "ad_uuids": ["uuid", ...],                    # zero or more
          "people":        [<mention>, ...],            # optional
          "organizations": [<mention>, ...],            # optional
          "places":        [<mention>, ...],            # optional
          "products":      [<mention>, ...],            # optional
          "events":        [<mention>, ...],            # optional
          "continued_from_item_id": null,
          "continued_to_item_id":   null,
          "repair_needed": false,
          "repair_reason": ""
        }
      ],
      "page_repair_needed": false,
      "page_repair_reason": ""
    }

Each ``<mention>`` is a small dict::

    {"full_name": "James McLeod",        # for people
     "first_name": "James",
     "last_name":  "McLeod",
     "title":      "Mr.",
     # OR for orgs/places/products/events:
     # "name": "...", "place_type": "...", etc.
     "role":         "subject|byline|mentioned|...",
     "mention_text": "Mr. James McLeod",
     "span_start":   42,                 # offset into items.full_text
     "span_end":     58,
     "confidence":   0.85}

The ingester:

  * loads the ticket (so we have the column extents, ad bboxes, slice
    metadata, content_hash);
  * validates the envelope;
  * for each item, derives a page-percent bbox from the column spans
    + slice_boundaries (vertical) and the page_layouts boundary
    positions (horizontal), unioning with ad bboxes where present;
  * builds full_text by concatenating each column span's
    transcript_text[start:end];
  * inserts items, item_column_spans, item_ad_associations, and
    entity rows + mentions in one transaction;
  * raises a repair if the agent flagged ``page_repair_needed`` or
    any per-item ``repair_needed``;
  * tags every item's ``notes`` field with
    ``content_hash=<hex>`` so a later re-run can detect that this
    page has already been classified for this set of transcripts.

No mvtm.db writes; ad bbox lookups go through the ATTACHed read-only
connection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

from . import db as _db
from . import ingest_column_result as _col


RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
WORK_TICKETS_DIR = os.path.join(
    _db.REPO_ROOT, "transcribe", "work", "items")
AGENT_FILE_REL = ".claude/agents/items-classifier.md"


_VALID_ITEM_TYPES = {
    "article", "display_ad", "classified_ad", "notice", "masthead",
    "cartoon", "letter", "announcement", "table", "index", "other",
}


# ---------- envelope parsing ---------------------------------------

def parse_envelope(raw: str) -> dict:
    """Validate the items envelope. Returns the parsed dict on success.

    Raises ValueError with a helpful message on bad shape.
    """
    try:
        data = json.loads(_col.strip_fence(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"result is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"result must be a JSON object, got {type(data).__name__}")

    if "items" not in data or not isinstance(data["items"], list):
        raise ValueError("envelope must have an 'items' list")

    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] must be a JSON object")
        for k in ("item_type", "summary"):
            if k not in item:
                raise ValueError(
                    f"items[{i}] missing required field {k!r}")
        if item["item_type"] not in _VALID_ITEM_TYPES:
            raise ValueError(
                f"items[{i}].item_type {item['item_type']!r} not in "
                f"{sorted(_VALID_ITEM_TYPES)}")

        spans = item.get("column_spans", [])
        ads = item.get("ad_uuids", [])
        if not spans and not ads:
            raise ValueError(
                f"items[{i}] has neither column_spans nor ad_uuids; "
                f"every item must anchor to at least one")

        for j, span in enumerate(spans):
            for k in ("column_transcript_id", "start_offset",
                      "end_offset"):
                if k not in span:
                    raise ValueError(
                        f"items[{i}].column_spans[{j}] missing {k!r}")

    return data


# ---------- bbox derivation ----------------------------------------

def _column_extent(boundary_positions: list[float],
                   col_idx: int) -> tuple[float, float]:
    """Return (left_pct, right_pct) for a column index using the
    page_layouts.boundary_positions list. boundary_positions has
    num_columns+1 entries; col_idx 0 spans positions[0..1].
    """
    if col_idx < 0 or col_idx + 1 >= len(boundary_positions):
        raise ValueError(
            f"col_idx {col_idx} out of range for "
            f"{len(boundary_positions)}-entry boundary_positions")
    return (float(boundary_positions[col_idx]),
            float(boundary_positions[col_idx + 1]))


def _interpolate_y_pct(slice_boundaries: list[dict],
                       char_offset: int) -> float | None:
    """Map a char offset into a column's transcript_text to a
    page-percent y position by finding the slice that contains the
    offset and linear-interpolating between its y_top_pct and
    y_bottom_pct.

    Returns None if slice_boundaries is empty / missing or the
    offset is past the end (in which case the caller should fall
    back to the column's overall extent).
    """
    if not slice_boundaries:
        return None

    for s in slice_boundaries:
        a = s.get("char_offset_start")
        b = s.get("char_offset_end")
        if a is None or b is None:
            continue
        if a <= char_offset <= b:
            span = b - a
            if span <= 0:
                return float(s["y_top_pct"])
            frac = (char_offset - a) / span
            return float(s["y_top_pct"]) + frac * (
                float(s["y_bottom_pct"]) - float(s["y_top_pct"]))

    # past-end fallback: clamp to last slice's y_bottom_pct
    last = slice_boundaries[-1]
    if char_offset > (last.get("char_offset_end") or 0):
        return float(last["y_bottom_pct"])
    return None


def _span_y_extents(span: dict,
                    column: dict,
                    snap_ctx: dict | None = None
                    ) -> tuple[float | None, float | None]:
    """Return (top_pct, bottom_pct) for one column-span using the
    column's slice_boundaries, then snap to natural divisions when
    a snap context is provided. Falls back to None when slice meta
    is missing — the caller should then pad with sensible defaults.

    The slice interpolation is approximate because chars per pct are
    not uniform (headlines have lower density than body, ads have
    different leading from articles). The snap pulls the bbox edge
    onto a real horizontal feature: a detected h-rule (preferred) or
    the start/end of a body-text run (a whitespace gap that often
    coincides with an item-separating rule the cutter missed).
    """
    sb = column.get("slice_boundaries")
    if isinstance(sb, str):
        try:
            sb = json.loads(sb)
        except json.JSONDecodeError:
            sb = None
    if not sb:
        return None, None

    top = _interpolate_y_pct(sb, span["start_offset"])
    bot = _interpolate_y_pct(sb, span["end_offset"])

    if snap_ctx is not None:
        col_idx = int(column["col_idx"])
        top = _snap_y(top, "top", col_idx, snap_ctx)
        bot = _snap_y(bot, "bottom", col_idx, snap_ctx)

    return top, bot


# ---------- y-snap to natural divisions ----------------------------

# Tolerances for snapping. Tuned to be permissive enough to catch
# missed-rule cases (where the linear-interp y is several percent
# off the real division) but tight enough not to pull a clean
# boundary off its true position.
H_RULE_SNAP_PCT = 1.5
BODY_SNAP_PCT   = 5.0
# Body=False runs shorter than this (typical inter-paragraph
# leading) are filtered out so we don't snap to mid-paragraph gaps.
MIN_GAP_PCT     = 0.40


def _body_runs_from_chart(chart: dict) -> list[tuple[float, float]]:
    """Compute body=True runs from a body_text_chart entry.

    Short body=False gaps (< MIN_GAP_PCT) are absorbed into the
    surrounding body=True run — those are paragraph leading, not
    item separators. Item-separating gaps in this corpus are
    consistently 0.4–0.6% page-pct wide.
    """
    ys = chart.get("y_pct") or []
    body = chart.get("body") or []
    if not ys or not body or len(ys) != len(body):
        return []

    runs: list[tuple[float, float]] = []
    i = 0
    n = len(body)
    while i < n:
        # advance past any leading non-body
        while i < n and not body[i]:
            i += 1
        if i >= n:
            break
        run_start = ys[i]
        # extend through body=True, absorbing short body=False gaps
        last_body_y = ys[i]
        while i < n:
            if body[i]:
                last_body_y = ys[i]
                i += 1
                continue
            # found a False; peek ahead for end of this False-run
            j = i
            while j < n and not body[j]:
                j += 1
            gap_width = ys[min(j, n - 1)] - ys[i]
            if gap_width < MIN_GAP_PCT and j < n:
                # short gap (paragraph leading) — keep extending
                i = j
                continue
            # real gap: terminate run here
            break
        runs.append((run_start, last_body_y))
    return runs


def _h_rules_for_col(h_rules: list[dict],
                     col_left_pct: float,
                     col_right_pct: float) -> list[float]:
    """y_pct of every h_rule whose x-extent overlaps this column.

    The detector tags each rule with the column it was found in
    (col_idx), but we filter by x-extent here too so a multi-column
    rule that was detected in a neighbouring column's strip still
    counts for this column.
    """
    out: list[float] = []
    for r in h_rules:
        x1 = r.get("x1_pct")
        x2 = r.get("x2_pct")
        if x1 is None or x2 is None:
            continue
        # any x-overlap with the column counts
        if x2 < col_left_pct or x1 > col_right_pct:
            continue
        y = r.get("y_pct")
        if y is None:
            continue
        out.append(float(y))
    return sorted(out)


def build_snap_context(year: int, month: int, day: int, page: int,
                       boundary_positions: list[float] | None = None,
                       ) -> dict | None:
    """Build the per-column snap context from page_analysis.json.

    Returns ``{col_idx: {"body_runs": [(y0, y1), ...],
                          "h_rule_ys": [y, ...]}}`` or ``None`` when
    page_analysis.json is missing (the ingester then falls back to
    pure linear interpolation, the previous behaviour).
    """
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    path = os.path.join(_db.REPO_ROOT, "columns", date_str,
                        f"p{page}", "page_analysis.json")
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        pa = json.load(f)

    ctx: dict[int, dict] = {}
    for chart in pa.get("body_text_charts", []) or []:
        ci = int(chart["col_idx"])
        ctx[ci] = {"body_runs": _body_runs_from_chart(chart),
                   "h_rule_ys": []}

    # h_rules: filter to this column's x-extent. Need column
    # boundaries; if not provided, fall back to col_idx tag on each
    # rule (the detector emits one).
    h_rules = pa.get("h_rules", []) or []
    if boundary_positions:
        for ci in ctx:
            if 0 <= ci < len(boundary_positions) - 1:
                ctx[ci]["h_rule_ys"] = _h_rules_for_col(
                    h_rules,
                    float(boundary_positions[ci]),
                    float(boundary_positions[ci + 1]))
    else:
        for r in h_rules:
            ci = r.get("col_idx")
            if ci is None or ci not in ctx:
                continue
            y = r.get("y_pct")
            if y is not None:
                ctx[ci]["h_rule_ys"].append(float(y))
        for ci in ctx:
            ctx[ci]["h_rule_ys"].sort()

    return ctx


def _snap_y(y: float | None, kind: str, col_idx: int,
            snap_ctx: dict | None) -> float | None:
    """Snap a candidate y_pct to the nearest natural division.

    ``kind`` is ``"top"`` or ``"bottom"`` — for ``top`` we snap to
    body-run starts, for ``bottom`` to body-run ends. h_rules are
    eligible regardless of kind.

    No-op when ``y`` is None, the snap context is missing, or the
    column has no chart data. No-op when the nearest target is
    farther than the tolerance.
    """
    if y is None or snap_ctx is None:
        return y
    info = snap_ctx.get(col_idx)
    if info is None:
        return y

    # Pass 1: tight h_rule snap.
    h_ys = info.get("h_rule_ys") or []
    if h_ys:
        best = min(h_ys, key=lambda hy: abs(hy - y))
        if abs(best - y) <= H_RULE_SNAP_PCT:
            return float(best)

    # Pass 2: body-run edge snap.
    runs = info.get("body_runs") or []
    if not runs:
        return y

    if kind == "top":
        edges = [r_start for r_start, _ in runs]
    else:
        edges = [r_end for _, r_end in runs]

    best_edge = min(edges, key=lambda e: abs(e - y))
    if abs(best_edge - y) <= BODY_SNAP_PCT:
        return float(best_edge)
    return y


def _ad_bbox(ticket_ads: list[dict],
             ad_uuid: str) -> tuple[float, float, float, float] | None:
    for a in ticket_ads:
        if a["ad_uuid"] == ad_uuid:
            b = a["bbox_pct"]
            return (float(b["x_pct"]),     float(b["y_pct"]),
                    float(b["x_end_pct"]), float(b["y_end_pct"]))
    return None


def _column_index_for_id(columns: list[dict],
                         column_transcript_id: str) -> int | None:
    for c in columns:
        if c["column_transcript_id"] == column_transcript_id:
            return int(c["col_idx"])
    return None


def _column_for_id(columns: list[dict],
                   column_transcript_id: str) -> dict | None:
    for c in columns:
        if c["column_transcript_id"] == column_transcript_id:
            return c
    return None


def derive_item_bbox(item: dict,
                     ticket: dict,
                     snap_ctx: dict | None = None,
                     ) -> tuple[float, float, float, float,
                                list[int], list[float | None],
                                list[float | None]]:
    """Compute (left_pct, top_pct, right_pct, bottom_pct,
    column_idxs, span_top_pcts, span_bottom_pcts) for one item.

    span_top_pcts / span_bottom_pcts are aligned with the item's
    column_spans list (one entry each), to be written into
    item_column_spans rows. None entries indicate the slice
    interpolation didn't yield a value (rare; the bbox ends up using
    the column's full extent for those).

    When ``snap_ctx`` is provided, span y-extents are snapped to
    nearby h-rules / body-run edges (see ``_snap_y``) so the bbox
    pulls onto natural divisions instead of sub-slice pixel cuts.
    """
    boundary_positions = ticket["page_state"]["boundary_positions"]
    columns = ticket["columns"]
    ads = ticket["ads"]

    lefts:   list[float] = []
    rights:  list[float] = []
    tops:    list[float] = []
    bottoms: list[float] = []
    col_idxs:        list[int] = []
    span_tops:    list[float | None] = []
    span_bottoms: list[float | None] = []

    for span in item.get("column_spans", []):
        cid = span["column_transcript_id"]
        col = _column_for_id(columns, cid)
        if col is None:
            raise ValueError(
                f"unknown column_transcript_id in span: {cid}")
        ci = int(col["col_idx"])
        col_idxs.append(ci)
        l, r = _column_extent(boundary_positions, ci)
        lefts.append(l)
        rights.append(r)

        t, b = _span_y_extents(span, col, snap_ctx=snap_ctx)
        span_tops.append(t)
        span_bottoms.append(b)
        if t is not None:
            tops.append(t)
        if b is not None:
            bottoms.append(b)

    for ad_uuid in item.get("ad_uuids", []):
        ab = _ad_bbox(ads, ad_uuid)
        if ab is None:
            raise ValueError(
                f"unknown ad_uuid in item: {ad_uuid}")
        x1, y1, x2, y2 = ab
        lefts.append(x1)
        tops.append(y1)
        rights.append(x2)
        bottoms.append(y2)

    if not lefts or not rights:
        raise ValueError(
            "could not derive bbox: item has no resolvable column "
            "or ad anchors")

    bbox_left   = min(lefts)
    bbox_right  = max(rights)

    # If we got no vertical info (no slices, no ads), default to the
    # full column extent — page geometry doesn't carry vertical
    # text-area extents today, so 0/100 is the honest fallback.
    bbox_top    = min(tops)    if tops    else 0.0
    bbox_bottom = max(bottoms) if bottoms else 100.0

    return (round(bbox_left, 2),   round(bbox_top, 2),
            round(bbox_right, 2),  round(bbox_bottom, 2),
            col_idxs, span_tops, span_bottoms)


# ---------- full_text assembly -------------------------------------

def assemble_full_text(item: dict,
                       column_text_by_id: dict[str, str]) -> str:
    """Concatenate transcript_text[start:end] per span (in sequence
    order) joined by a single newline. Ad-only items return the empty
    string here — the ad transcript is captured separately on the
    ad_transcripts row (joined to the item via item_ad_associations).
    """
    spans = sorted(item.get("column_spans", []),
                   key=lambda s: s.get("sequence", 0))
    parts: list[str] = []
    for s in spans:
        cid = s["column_transcript_id"]
        text = column_text_by_id.get(cid)
        if text is None:
            raise ValueError(
                f"column_transcript_id {cid} not found in DB; the "
                f"agent referenced a column not on this page")
        a = max(0, int(s["start_offset"]))
        b = min(len(text), int(s["end_offset"]))
        if b > a:
            parts.append(text[a:b])
    return "\n".join(parts)


# ---------- entity dedup -------------------------------------------

_NORM_RE = re.compile(r"[^a-z0-9]+")


def normalise_key(name: str) -> str:
    """Loose dedup key: lowercase, strip non-alphanumerics."""
    return _NORM_RE.sub("", name.lower()).strip()


def upsert_entity(conn: sqlite3.Connection,
                  table: str,
                  *,
                  name: str,
                  extra_cols: dict | None = None,
                  mention_date: str | None = None,
                  alias: str | None = None) -> str:
    """Find-or-insert. Returns entity id.

    ``extra_cols`` carries the type-specific columns (first_name,
    last_name, title etc.) that get filled in on insert. On lookup,
    we use ``normalised_key`` only — first-write-wins on the rich
    columns; later mentions inherit the existing canonical row.

    ``mention_date`` (ISO 'YYYY-MM-DD', the issue date this mention
    came from) widens first_seen_date/last_seen_date via MIN/MAX on
    every call, insert or lookup-hit alike — unlike the rich columns,
    these track the full span of mentions seen, not just the first.

    ``alias`` (e.g. "Wm. Garvin" when ``name`` is the expanded
    "William Garvin" -- the caller derives this from the mention's
    own ``mention_text`` field, already part of the mention schema;
    see _insert_mentions) is recorded as a note, same format
    ``merge_entity.py`` uses ("alias: X") so both paths land in the
    same place -- appended once, not duplicated on repeat mentions.
    """
    nk = normalise_key(name)
    if not nk:
        raise ValueError(f"empty normalised key for name {name!r}")

    row = conn.execute(
        f"SELECT id, notes FROM {table} WHERE normalised_key=? LIMIT 1",
        (nk,)).fetchone()
    if row is not None:
        if mention_date:
            conn.execute(
                f"UPDATE {table} SET "
                f"first_seen_date = min(coalesce(first_seen_date, ?), ?), "
                f"last_seen_date  = max(coalesce(last_seen_date, ?), ?) "
                f"WHERE id=?",
                (mention_date, mention_date, mention_date, mention_date,
                 row["id"]))
        if alias:
            alias_note = f"alias: {alias}"
            notes = (row["notes"] or "")
            if alias_note not in notes:
                notes = (notes + "; " + alias_note).strip("; ")
                conn.execute(f"UPDATE {table} SET notes=? WHERE id=?", (notes, row["id"]))
        return row["id"]

    new_id = _db.new_uuid()
    cols = ["id", "normalised_key", "created_at"]
    vals: list = [new_id, nk, _db.now_iso()]
    if table == "people":
        cols.append("full_name")
        vals.append(name)
    else:
        cols.append("name")
        vals.append(name)

    if mention_date:
        cols += ["first_seen_date", "last_seen_date"]
        vals += [mention_date, mention_date]

    if alias:
        cols.append("notes")
        vals.append(f"alias: {alias}")

    if extra_cols:
        for k, v in extra_cols.items():
            cols.append(k)
            vals.append(v)

    placeholders = ", ".join("?" * len(cols))
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) "
        f"VALUES ({placeholders})", vals)
    return new_id


# ---------- ingestion ----------------------------------------------

def load_ticket(page_id: str) -> dict:
    path = os.path.join(WORK_TICKETS_DIR, f"{page_id}.json")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no ticket file at {path}")
    with open(path) as f:
        return json.load(f)


def load_result(page_id: str, result_path: str | None = None) -> str:
    if result_path is None:
        result_path = os.path.join(RESULTS_DIR, f"{page_id}.json")
    if not os.path.isfile(result_path):
        raise FileNotFoundError(
            f"no result file at {result_path}; the orchestrator "
            f"should save the agent's envelope there before ingest")
    with open(result_path) as f:
        return f.read()


def _fetch_column_texts(conn: sqlite3.Connection,
                        column_ids: list[str]) -> dict[str, str]:
    if not column_ids:
        return {}
    placeholders = ", ".join("?" * len(column_ids))
    rows = conn.execute(
        f"SELECT id, transcript_text FROM column_transcripts "
        f"WHERE id IN ({placeholders})", column_ids).fetchall()
    return {r["id"]: (r["transcript_text"] or "") for r in rows}


def _insert_mentions(conn: sqlite3.Connection,
                     *,
                     item_id: str,
                     mentions: list[dict],
                     entity_table: str,
                     junction_table: str,
                     junction_fk_col: str,
                     name_keys: tuple[str, ...],
                     mention_date: str | None = None) -> int:
    """Insert a list of mentions into the appropriate junction table,
    upserting the entity by normalised name. Returns the count
    inserted. ``name_keys`` is the ordered list of candidate name
    fields (e.g. ('full_name', 'name')) — the first one present on
    the mention dict is used for normalisation.
    """
    inserted = 0
    seen_keys: set[tuple[str, int]] = set()  # (entity_id, span_start)
    for m in mentions:
        if not isinstance(m, dict):
            continue
        name = None
        for k in name_keys:
            if m.get(k):
                name = m[k]
                break
        if not name:
            continue

        extra: dict = {}
        if entity_table == "people":
            for k in ("first_name", "last_name", "title", "suffix"):
                if m.get(k):
                    extra[k] = m[k]
        elif entity_table == "organizations":
            if m.get("org_type"):
                extra["org_type"] = m["org_type"]
        elif entity_table == "places":
            if m.get("place_type"):
                extra["place_type"] = m["place_type"]
        elif entity_table == "products":
            if m.get("manufacturer"):
                extra["manufacturer"] = m["manufacturer"]
            if m.get("product_type"):
                extra["product_type"] = m["product_type"]
        elif entity_table == "events":
            if m.get("year_known") is not None:
                extra["year_known"] = m["year_known"]
            if m.get("date_known"):
                extra["date_known"] = m["date_known"]
            if m.get("event_type"):
                extra["event_type"] = m["event_type"]

        # mention_text is the exact original token as printed (see the
        # schema docstring in items-classifier.md/ocr-items.md) --
        # when it differs from the canonical `name` (e.g. name was
        # expanded from an abbreviation like "Wm." -> "William"),
        # that's exactly the alias worth recording. No separate
        # "alias" field needed; this is what mention_text is for.
        mention_text = m.get("mention_text")
        alias = None
        if mention_text and normalise_key(mention_text) != normalise_key(name):
            alias = mention_text

        eid = upsert_entity(conn, entity_table,
                            name=name, extra_cols=extra,
                            mention_date=mention_date, alias=alias)

        span_start = int(m.get("span_start") or 0)
        span_end   = m.get("span_end")
        span_end   = int(span_end) if span_end is not None else None

        # Junction PK is (item_id, entity_id, span_start). If the
        # agent emitted two mentions of the same entity at the same
        # span_start (rare; usually the agent collapses them), keep
        # the first.
        dedup_key = (eid, span_start)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        conn.execute(
            f"INSERT OR IGNORE INTO {junction_table} "
            f"(item_id, {junction_fk_col}, role, mention_text, "
            f" span_start, span_end, confidence) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, eid,
             m.get("role"),
             m.get("mention_text") or name,
             span_start, span_end,
             m.get("confidence")))
        inserted += 1
    return inserted


def ingest(page_id: str,
           *,
           result_path: str | None = None,
           model: str | None = None) -> dict:
    """Validate and ingest one items result. Returns a small report."""
    ticket = load_ticket(page_id)
    raw = load_result(page_id, result_path)
    envelope = parse_envelope(raw)

    if model is None:
        agent_path = os.path.join(_db.REPO_ROOT, AGENT_FILE_REL)
        if os.path.isfile(agent_path):
            model = _db.read_agent_default_model(agent_path) or "unknown"
        else:
            model = "unknown"

    year   = ticket["issue"]["year"]
    month  = ticket["issue"]["month"]
    day    = ticket["issue"]["day"]
    page   = ticket["page"]
    chash  = ticket.get("content_hash", "")
    prompt_h = ticket.get("prompt_hash", "")

    column_ids = [c["column_transcript_id"] for c in ticket["columns"]]

    # Snap context: per-column body-runs and h-rules, used to pull
    # bbox edges off slice-pixel boundaries onto natural divisions
    # (see C in the bbox-too-tall plan).
    boundary_positions = ticket["page_state"]["boundary_positions"]
    snap_ctx = build_snap_context(year, month, day, page,
                                  boundary_positions)

    conn = _db.open_connection(attach_mvtm=False)
    items_inserted = 0
    spans_inserted = 0
    ad_assocs_inserted = 0
    mentions_inserted = 0
    repair_ids: list[str] = []
    item_ids: list[str] = []

    try:
        column_text_by_id = _fetch_column_texts(conn, column_ids)

        for item in envelope["items"]:
            (bb_left, bb_top, bb_right, bb_bottom,
             col_idxs, span_tops, span_bottoms) = derive_item_bbox(
                item, ticket, snap_ctx=snap_ctx)

            full_text = assemble_full_text(item, column_text_by_id)
            crosses_columns = 1 if len(set(col_idxs)) > 1 else 0
            is_inset = 1 if (item.get("ad_uuids")
                             and item.get("column_spans")) else 0

            item_id = _db.new_uuid()
            item_ids.append(item_id)

            conn.execute(
                """INSERT INTO items
                   (id, item_type, year, month, day, page,
                    bbox_left_pct, bbox_top_pct, bbox_right_pct,
                    bbox_bottom_pct, column_span_json,
                    crosses_columns, is_inset, crosses_pages,
                    continued_to_item_id, continued_from_item_id,
                    headline, byline, summary, full_text,
                    language, classification_confidence,
                    model, prompt_hash, raw_response_json,
                    repair_needed, repair_reason,
                    created_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?,
                           ?, ?, 0,
                           ?, ?,
                           ?, ?, ?, ?,
                           ?, ?,
                           ?, ?, ?,
                           ?, ?,
                           ?, ?)""",
                (item_id, item["item_type"], year, month, day, page,
                 bb_left, bb_top, bb_right, bb_bottom,
                 json.dumps(sorted(set(col_idxs))),
                 crosses_columns, is_inset,
                 item.get("continued_to_item_id"),
                 item.get("continued_from_item_id"),
                 item.get("headline"),
                 item.get("byline"),
                 item.get("summary"),
                 full_text,
                 item.get("language") or "en",
                 item.get("classification_confidence"),
                 model, prompt_h, raw,
                 1 if item.get("repair_needed") else 0,
                 item.get("repair_reason") or None,
                 _db.now_iso(),
                 f"content_hash={chash}"))
            items_inserted += 1

            # Column spans
            for i, span in enumerate(item.get("column_spans", [])):
                seq = int(span.get("sequence", i))
                conn.execute(
                    """INSERT INTO item_column_spans
                       (item_id, column_transcript_id, sequence,
                        start_offset, end_offset,
                        bbox_top_pct, bbox_bottom_pct)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (item_id, span["column_transcript_id"], seq,
                     int(span["start_offset"]),
                     int(span["end_offset"]),
                     span_tops[i], span_bottoms[i]))
                spans_inserted += 1

            # Ad associations
            for ad_uuid in item.get("ad_uuids", []):
                conn.execute(
                    """INSERT OR IGNORE INTO item_ad_associations
                       (item_id, ad_uuid) VALUES (?, ?)""",
                    (item_id, ad_uuid))
                ad_assocs_inserted += 1

            # Entity mentions
            mention_date = f"{year:04d}-{month:02d}-{day:02d}"
            mentions_inserted += _insert_mentions(
                conn, item_id=item_id,
                mentions=item.get("people", []),
                entity_table="people",
                junction_table="item_people_mentions",
                junction_fk_col="person_id",
                name_keys=("full_name", "name"),
                mention_date=mention_date)
            mentions_inserted += _insert_mentions(
                conn, item_id=item_id,
                mentions=item.get("organizations", []),
                entity_table="organizations",
                junction_table="item_organizations_mentions",
                junction_fk_col="organization_id",
                name_keys=("name", "full_name"),
                mention_date=mention_date)
            mentions_inserted += _insert_mentions(
                conn, item_id=item_id,
                mentions=item.get("places", []),
                entity_table="places",
                junction_table="item_places_mentions",
                junction_fk_col="place_id",
                name_keys=("name", "full_name"),
                mention_date=mention_date)
            mentions_inserted += _insert_mentions(
                conn, item_id=item_id,
                mentions=item.get("products", []),
                entity_table="products",
                junction_table="item_products_mentions",
                junction_fk_col="product_id",
                name_keys=("name", "full_name"),
                mention_date=mention_date)
            mentions_inserted += _insert_mentions(
                conn, item_id=item_id,
                mentions=item.get("events", []),
                entity_table="events",
                junction_table="item_events_mentions",
                junction_fk_col="event_id",
                mention_date=mention_date,
                name_keys=("name", "full_name"))

            if item.get("repair_needed"):
                rid = _db.raise_repair(
                    conn,
                    target_kind="page",
                    target_ref={"year": year, "month": month,
                                "day": day, "page": page},
                    repair_kind="other",
                    description=item.get("repair_reason") or
                                "(no reason given)",
                    raised_by=model,
                    related_item_id=item_id)
                repair_ids.append(rid)

        if envelope.get("page_repair_needed"):
            rid = _db.raise_repair(
                conn,
                target_kind="page",
                target_ref={"year": year, "month": month,
                            "day": day, "page": page},
                repair_kind="other",
                description=envelope.get("page_repair_reason") or
                            "(no reason given)",
                raised_by=model)
            repair_ids.append(rid)

        conn.commit()
    finally:
        conn.close()

    return {
        "page_id": page_id,
        "model": model,
        "items_inserted": items_inserted,
        "spans_inserted": spans_inserted,
        "ad_assocs_inserted": ad_assocs_inserted,
        "mentions_inserted": mentions_inserted,
        "repair_ids": repair_ids,
        "item_ids": item_ids,
        "content_hash": chash,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest one items-classifier result.")
    p.add_argument("page_id",
                   help="Ticket stem, e.g. 1912-12-27_p6")
    p.add_argument("--result-file", default=None,
                   help="Path to the agent's JSON envelope file "
                        "(default: transcribe/work/results/"
                        "<page-id>.json)")
    p.add_argument("--model", default=None,
                   help="Model name the agent ran as (default: read "
                        "from agent file frontmatter)")
    args = p.parse_args(argv)

    try:
        report = ingest(args.page_id,
                        result_path=args.result_file,
                        model=args.model)
    except (FileNotFoundError, ValueError) as e:
        print(f"ingest failed: {e}", file=sys.stderr)
        return 1

    print(f"ingested {report['page_id']}")
    print(f"  model:               {report['model']}")
    print(f"  items inserted:      {report['items_inserted']}")
    print(f"  column spans:        {report['spans_inserted']}")
    print(f"  ad associations:     {report['ad_assocs_inserted']}")
    print(f"  entity mentions:     {report['mentions_inserted']}")
    print(f"  content_hash:        {report['content_hash'][:12]}...")
    if report["repair_ids"]:
        print(f"  repairs raised:      {len(report['repair_ids'])}")
        for rid in report["repair_ids"]:
            print(f"    {rid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
