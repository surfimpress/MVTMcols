"""Ingest one items-tidier (pass-3) result back into transcribe.db.

The orchestrator (Claude Code) saves the agent's JSON envelope for a
page to ``transcribe/work/results/<page-id>.json`` and then runs::

    python3 -m transcribe.ingest_item_tidy_result <page-id>

The envelope shape mirrors ``.claude/agents/items-tidier.md``::

    {
      "merges":      [{"source_item_ids": [...], "headline": "...",
                        "summary": "...", "item_type": "...",
                        "byline": null, "language": "en",
                        "classification_confidence": 0.9,
                        "uncertain": false, "reasoning": "..."}],
      "splits":      [{"source_item_id": "...", "pieces": [...],
                        "reasoning": "..."}],
      "insets":      [{"container_item_id": "...",
                        "inset_item_id": "..." | null,
                        "ad_uuid": "..." | null,
                        "reasoning": "..."}],
      "corrections": [{"item_id": "...", "field": "...",
                        "old_value": "...", "new_value": "...",
                        "reasoning": "..."}],
      "page_repair_needed": false,
      "page_repair_reason": ""
    }

Pass-3 is a *delta* batch. Only items the tidier touched (merge,
split, correction, or inset-polygon attachment) get pass-3 rows.
Pass-2 items not referenced anywhere stay authoritative unchanged.
A pass-3 row marks its provenance via ``derived_from_item_ids`` —
the consumer reconstructs the page snapshot by overlaying pass-3
deltas onto pass-2 (any pass-2 id present in some pass-3 row's
``derived_from_item_ids`` for the latest batch is superseded).

Polygons (``items.geometry_polygon_json``) are emitted only when an
inset relationship resolves cleanly to a flush-edge cut against the
container's bounding rectangle. Fully-internal "doughnut" knockouts
or full-container cuts raise a repair instead of fabricating
geometry.

Splits are accepted in the envelope but not yet implemented — they
raise a repair so a later iteration can land them. The 2026-05-06
agent prompt advises favouring merges, so this is the rare path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Iterable

from . import db as _db
from . import ingest_column_result as _col
from . import ingest_item_result as _items
from .ingest_item_result import normalise_key, upsert_entity  # re-export


RESULTS_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "results")
WORK_TICKETS_DIR = os.path.join(
    _db.REPO_ROOT, "transcribe", "work", "items_tidy")
AGENT_FILE_REL = ".claude/agents/items-tidier.md"


# Same buffer used by the cutter for slant tolerance, keeping the
# polygon visually faithful to what a careful reader would draw.
INSET_BUFFER_PCT = 1.0

# Tolerance for "this inset edge is flush with the container edge."
# Small enough to require a real flush, generous enough to absorb
# the rounding to 2 decimal places used elsewhere in the pipeline.
FLUSH_EPS_PCT = 0.5


_VALID_ITEM_TYPES = _items._VALID_ITEM_TYPES


# ---------- envelope parsing ---------------------------------------

def parse_envelope(raw: str) -> dict:
    """Validate the items-tidier envelope. Returns the parsed dict."""
    try:
        data = json.loads(_col.strip_fence(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"result is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(
            f"envelope must be a JSON object, got "
            f"{type(data).__name__}")

    for key in ("merges", "splits", "insets", "corrections"):
        if key in data and not isinstance(data[key], list):
            raise ValueError(f"envelope.{key} must be a list")
        data.setdefault(key, [])

    for i, m in enumerate(data["merges"]):
        if not isinstance(m, dict):
            raise ValueError(f"merges[{i}] must be a JSON object")
        if not isinstance(m.get("source_item_ids"), list) or \
                len(m["source_item_ids"]) < 2:
            raise ValueError(
                f"merges[{i}].source_item_ids must list >=2 ids")
        if "item_type" in m and m["item_type"] not in _VALID_ITEM_TYPES:
            raise ValueError(
                f"merges[{i}].item_type {m['item_type']!r} not in "
                f"{sorted(_VALID_ITEM_TYPES)}")

    for i, s in enumerate(data["splits"]):
        if not isinstance(s, dict):
            raise ValueError(f"splits[{i}] must be a JSON object")
        if "source_item_id" not in s:
            raise ValueError(f"splits[{i}] missing source_item_id")
        if not isinstance(s.get("pieces"), list) or not s["pieces"]:
            raise ValueError(
                f"splits[{i}] needs a non-empty pieces list")

    for i, ins in enumerate(data["insets"]):
        if not isinstance(ins, dict):
            raise ValueError(f"insets[{i}] must be a JSON object")
        if "container_item_id" not in ins:
            raise ValueError(
                f"insets[{i}] missing container_item_id")
        has_target = bool(ins.get("inset_item_id")) or \
                     bool(ins.get("ad_uuid"))
        if not has_target:
            raise ValueError(
                f"insets[{i}] needs one of inset_item_id or ad_uuid")

    for i, c in enumerate(data["corrections"]):
        if not isinstance(c, dict):
            raise ValueError(f"corrections[{i}] must be a JSON object")
        for k in ("item_id", "field", "new_value"):
            if k not in c:
                raise ValueError(
                    f"corrections[{i}] missing field {k!r}")
        if c["field"] not in {"headline", "item_type", "summary",
                              "byline", "language"}:
            raise ValueError(
                f"corrections[{i}].field {c['field']!r} not "
                f"correctable from pass-3")
        if c["field"] == "item_type" and \
                c["new_value"] not in _VALID_ITEM_TYPES:
            raise ValueError(
                f"corrections[{i}].new_value invalid item_type: "
                f"{c['new_value']!r}")

    return data


# ---------- ticket lookup ------------------------------------------

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


# ---------- pass-2 row fetching ------------------------------------

@dataclass
class Pass2Item:
    """Snapshot of a pass-2 items row plus its child rows. We hold
    everything needed to materialise a pass-3 derivation in memory
    so the ingestion transaction stays short."""
    id: str
    item_type: str
    headline: str | None
    byline: str | None
    summary: str | None
    language: str
    bbox: tuple[float, float, float, float]   # left, top, right, bot
    full_text: str
    classification_confidence: float | None
    repair_needed: bool
    repair_reason: str | None
    notes: str | None
    column_spans: list[dict]                   # rows from item_column_spans
    ad_uuids: list[str]


def _fetch_pass2_items(conn: sqlite3.Connection,
                       item_ids: Iterable[str]) -> dict[str, Pass2Item]:
    ids = list(item_ids)
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT id, item_type, headline, byline, summary, language,
                   bbox_left_pct, bbox_top_pct,
                   bbox_right_pct, bbox_bottom_pct,
                   full_text, classification_confidence,
                   repair_needed, repair_reason, notes
              FROM items
             WHERE id IN ({placeholders})""",
        ids).fetchall()
    out: dict[str, Pass2Item] = {}
    for r in rows:
        spans = conn.execute(
            """SELECT column_transcript_id, sequence,
                      start_offset, end_offset,
                      bbox_top_pct, bbox_bottom_pct
                 FROM item_column_spans
                WHERE item_id=?
                ORDER BY sequence""",
            (r["id"],)).fetchall()
        ad_rows = conn.execute(
            "SELECT ad_uuid FROM item_ad_associations "
            "WHERE item_id=? ORDER BY ad_uuid",
            (r["id"],)).fetchall()
        out[r["id"]] = Pass2Item(
            id=r["id"],
            item_type=r["item_type"],
            headline=r["headline"],
            byline=r["byline"],
            summary=r["summary"],
            language=r["language"] or "en",
            bbox=(r["bbox_left_pct"], r["bbox_top_pct"],
                  r["bbox_right_pct"], r["bbox_bottom_pct"]),
            full_text=r["full_text"] or "",
            classification_confidence=r["classification_confidence"],
            repair_needed=bool(r["repair_needed"]),
            repair_reason=r["repair_reason"],
            notes=r["notes"],
            column_spans=[dict(s) for s in spans],
            ad_uuids=[a["ad_uuid"] for a in ad_rows],
        )
    return out


# ---------- pass-3 plan --------------------------------------------

@dataclass
class Pass3Plan:
    """One pass-3 row to insert. ``derived_from`` lists the pass-2
    ids this row supersedes. ``edit_kind`` is informational."""
    derived_from: list[str]
    edit_kind: str                             # 'merge'|'correction'|'inset_only'|'split'
    item_type: str
    headline: str | None
    byline: str | None
    summary: str | None
    language: str
    classification_confidence: float | None
    bbox: tuple[float, float, float, float]
    column_spans: list[dict]
    ad_uuids: list[str]
    full_text: str
    polygon_vertices: list[list[float]] | None = None
    repair_needed: bool = False
    repair_reason: str | None = None
    reasoning: str | None = None
    uncertain: bool = False


def _bbox_union(boxes: list[tuple[float, float, float, float]]
                ) -> tuple[float, float, float, float]:
    lefts   = [b[0] for b in boxes]
    tops    = [b[1] for b in boxes]
    rights  = [b[2] for b in boxes]
    bottoms = [b[3] for b in boxes]
    return (round(min(lefts), 2),  round(min(tops), 2),
            round(max(rights), 2), round(max(bottoms), 2))


def _rectilinear_union_polygon(
        rects: list[tuple[float, float, float, float]]
        ) -> tuple[list[list[float]] | None, str | None]:
    """Boundary polygon of the union of N axis-aligned rectangles.

    Returns ``(vertices, repair_reason)``. ``vertices`` is a closed
    rectilinear polygon (first vertex repeated at end) when the union
    is a single simply-connected region; ``repair_reason`` is non-None
    when the union forms multiple disjoint pieces or has a hole — in
    that case ``vertices`` is None.

    Why this exists: the merge of N pass-2 items spans the bounding
    rectangle of their bboxes, but if the sources are arranged in a
    staircase (e.g. col 4 full + col 5 top + col 6 small slice) the
    bounding rectangle "claims" page area that no source actually
    occupies. The visible polygon is the union of the source
    rectangles, not their bounding rectangle. Storing this in
    ``geometry_polygon_json`` keeps the canvas representation honest.

    Algorithm: coordinate compression. Collect all unique x's and y's
    from the rectangles; build a small grid; mark each cell as covered
    if any rect contains it; trace the boundary by walking edges that
    separate a covered cell from an uncovered one (or the grid border).
    The traced loop is then deduplicated of collinear consecutive
    vertices and closed.
    """
    if not rects:
        return None, "no rectangles supplied"

    xs = sorted({r[0] for r in rects} | {r[2] for r in rects})
    ys = sorted({r[1] for r in rects} | {r[3] for r in rects})
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx == 0 or ny == 0:
        return None, "degenerate rectangles (zero width or height)"

    x_idx = {v: i for i, v in enumerate(xs)}
    y_idx = {v: j for j, v in enumerate(ys)}

    covered = [[False] * ny for _ in range(nx)]
    for (l, t, r, b) in rects:
        i0, i1 = x_idx[l], x_idx[r]
        j0, j1 = y_idx[t], y_idx[b]
        for i in range(i0, i1):
            for j in range(j0, j1):
                covered[i][j] = True

    # Directed boundary edges, oriented clockwise (interior on right).
    # Top edge → right; right edge → down; bottom edge → left; left
    # edge → up. ``edges[start_pt] = end_pt`` lets us chain by lookup.
    edges: dict[tuple[float, float], tuple[float, float]] = {}
    for i in range(nx):
        for j in range(ny):
            if not covered[i][j]:
                continue
            x1, x2 = xs[i],   xs[i + 1]
            y1, y2 = ys[j],   ys[j + 1]
            if j == 0 or not covered[i][j - 1]:
                edges[(x1, y1)] = (x2, y1)
            if i == nx - 1 or not covered[i + 1][j]:
                edges[(x2, y1)] = (x2, y2)
            if j == ny - 1 or not covered[i][j + 1]:
                edges[(x2, y2)] = (x1, y2)
            if i == 0 or not covered[i - 1][j]:
                edges[(x1, y2)] = (x1, y1)

    if not edges:
        return None, "no covered cells (sources don't form a region)"

    # Chain: start anywhere, walk until we return.
    start = next(iter(edges))
    chain: list[tuple[float, float]] = [start]
    seen: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    cur = edges[start]
    seen.add((start, cur))
    while cur != start:
        chain.append(cur)
        nxt = edges.get(cur)
        if nxt is None:
            return None, "boundary chain broke unexpectedly"
        if (cur, nxt) in seen:
            return None, "boundary chain looped on itself"
        seen.add((cur, nxt))
        cur = nxt

    # If we visited fewer edges than exist, the union has multiple
    # disjoint pieces or a hole — flag for repair instead of guessing.
    if len(seen) != len(edges):
        return None, ("union forms multiple disjoint pieces or has a "
                      "hole; rectangular bbox kept")

    # Drop interior collinear vertices (e.g. axis-aligned 3-point runs).
    n = len(chain)
    cleaned: list[tuple[float, float]] = []
    for i in range(n):
        prev_pt = chain[(i - 1) % n]
        p       = chain[i]
        next_pt = chain[(i + 1) % n]
        if ((prev_pt[0] == p[0] == next_pt[0]) or
                (prev_pt[1] == p[1] == next_pt[1])):
            continue
        cleaned.append(p)
    if len(cleaned) < 3:
        return None, "collapsed polygon (fewer than 3 distinct corners)"

    out = [[round(x, 2), round(y, 2)] for x, y in cleaned]
    out.append([out[0][0], out[0][1]])
    return out, None


def _ordered_column_spans(items: list[Pass2Item]) -> list[dict]:
    """Concatenate source items' column_spans in reading order
    (left-to-right by col_idx via column_transcripts, then by
    sequence within a col). Re-numbers the ``sequence`` field so the
    merged item has 0..N-1 sequences."""
    rows: list[tuple[int, int, dict]] = []
    for item in items:
        for s in item.column_spans:
            # We don't have col_idx directly; the ticket has it but
            # for ordering we use bbox_top_pct as a tie-breaker if
            # column_transcript_id sort isn't stable. The agent
            # delivers source_item_ids in left-to-right order
            # already, so a stable item-then-sequence order suffices.
            rows.append((items.index(item), s["sequence"], s))
    rows.sort(key=lambda r: (r[0], r[1]))
    out: list[dict] = []
    for new_seq, (_, _, s) in enumerate(rows):
        out.append({
            "column_transcript_id": s["column_transcript_id"],
            "sequence":             new_seq,
            "start_offset":         int(s["start_offset"]),
            "end_offset":           int(s["end_offset"]),
            "bbox_top_pct":         s.get("bbox_top_pct"),
            "bbox_bottom_pct":      s.get("bbox_bottom_pct"),
        })
    return out


def _concat_full_text(items: list[Pass2Item]) -> str:
    """Concatenate source items' full_text in source order, joined
    by a single newline. Pass-2 already produced these from
    transcript_text[start:end] joined per span; concatenating here
    gives the merged item's text in reading order."""
    parts = [it.full_text for it in items if it.full_text]
    return "\n".join(parts)


def _crosses_columns(spans: list[dict],
                     col_idx_by_id: dict[str, int]) -> int:
    cols = {col_idx_by_id.get(s["column_transcript_id"]) for s in spans}
    cols.discard(None)
    return 1 if len(cols) > 1 else 0


# ---------- polygon math --------------------------------------------

def _flush_edges(container: tuple[float, float, float, float],
                 inset:     tuple[float, float, float, float],
                 eps: float = FLUSH_EPS_PCT) -> set[str]:
    """Edges of ``inset`` flush with ``container`` (within eps)."""
    cl, ct, cr, cb = container
    il, it, ir, ib = inset
    edges: set[str] = set()
    if abs(it - ct) <= eps:
        edges.add("top")
    if abs(ib - cb) <= eps:
        edges.add("bottom")
    if abs(il - cl) <= eps:
        edges.add("left")
    if abs(ir - cr) <= eps:
        edges.add("right")
    return edges


def container_minus_inset(container: tuple[float, float, float, float],
                          inset:     tuple[float, float, float, float],
                          buffer: float = INSET_BUFFER_PCT,
                          eps: float = FLUSH_EPS_PCT,
                          ) -> tuple[list[list[float]] | None, str | None]:
    """Cut ``inset`` (buffered by ``buffer`` per edge, clipped to
    ``container``) out of ``container``. Returns
    ``(vertex_list, repair_reason)``: ``vertex_list`` is closed
    (first vertex repeated at end) when the cut is representable as
    a single rectilinear polygon; ``repair_reason`` is non-None when
    the cut would produce a hole, span the container edge to edge,
    or otherwise can't be expressed as a simple polygon.
    """
    cl, ct, cr, cb = container
    il = max(cl, inset[0] - buffer)
    it = max(ct, inset[1] - buffer)
    ir = min(cr, inset[2] + buffer)
    ib = min(cb, inset[3] + buffer)
    if il >= ir or it >= ib:
        return None, "inset has zero/negative area after buffering"

    edges = _flush_edges(container, (il, it, ir, ib), eps=eps)

    if not edges:
        return None, ("inset is fully internal — would create a hole; "
                      "polygon left unset, container kept rectangular")
    if len(edges) >= 4:
        return None, "inset covers the entire container"
    if edges == {"left", "right"} or edges == {"top", "bottom"}:
        return None, ("inset spans container edge to edge — would split "
                      "container into two disconnected pieces")

    def _close(pts: list[tuple[float, float]]) -> list[list[float]]:
        return [[round(x, 2), round(y, 2)] for x, y in pts] + \
               [[round(pts[0][0], 2), round(pts[0][1], 2)]]

    if len(edges) == 3:
        # Inset covers one strip; what remains is a rectangle.
        if "top" not in edges:
            pts = [(cl, ct), (cr, ct), (cr, it), (cl, it)]
        elif "bottom" not in edges:
            pts = [(cl, ib), (cr, ib), (cr, cb), (cl, cb)]
        elif "left" not in edges:
            pts = [(cl, ct), (il, ct), (il, cb), (cl, cb)]
        else:  # "right" not in edges
            pts = [(ir, ct), (cr, ct), (cr, cb), (ir, cb)]
        return _close(pts), None

    if len(edges) == 2:
        # Adjacent edges — corner cut, L-shape with 6 vertices.
        # (Opposite-edge case was caught above.)
        if edges == {"top", "left"}:
            pts = [(ir, ct), (cr, ct), (cr, cb),
                   (cl, cb), (cl, ib), (ir, ib)]
        elif edges == {"top", "right"}:
            pts = [(cl, ct), (il, ct), (il, ib),
                   (cr, ib), (cr, cb), (cl, cb)]
        elif edges == {"bottom", "right"}:
            pts = [(cl, ct), (cr, ct), (cr, it),
                   (il, it), (il, cb), (cl, cb)]
        elif edges == {"bottom", "left"}:
            pts = [(cl, ct), (cr, ct), (cr, cb),
                   (ir, cb), (ir, it), (cl, it)]
        else:
            return None, f"unexpected adjacent edge set {edges}"
        return _close(pts), None

    # len(edges) == 1 — notch on one side, U-shape with 8 vertices.
    if "top" in edges:
        pts = [(cl, ct), (il, ct), (il, ib),
               (ir, ib), (ir, ct), (cr, ct),
               (cr, cb), (cl, cb)]
    elif "bottom" in edges:
        pts = [(cl, ct), (cr, ct),
               (cr, cb), (ir, cb), (ir, it),
               (il, it), (il, cb), (cl, cb)]
    elif "left" in edges:
        pts = [(cl, ct), (cr, ct), (cr, cb),
               (cl, cb), (cl, ib), (ir, ib),
               (ir, it), (cl, it)]
    else:  # "right"
        pts = [(cl, ct), (cr, ct), (cr, it),
               (il, it), (il, ib), (cr, ib),
               (cr, cb), (cl, cb)]
    return _close(pts), None


# ---------- plan building ------------------------------------------

def _build_merge_plan(merge: dict,
                      pass2: dict[str, Pass2Item]
                      ) -> Pass3Plan:
    src_ids = list(merge["source_item_ids"])
    sources = [pass2[i] for i in src_ids if i in pass2]
    missing = [i for i in src_ids if i not in pass2]
    if missing:
        raise ValueError(
            f"merge references unknown source_item_ids: {missing}")

    spans = _ordered_column_spans(sources)
    full_text = _concat_full_text(sources)
    source_rects = [s.bbox for s in sources]
    bbox = _bbox_union(source_rects)

    # Compute the visible polygon as the rectilinear union of source
    # rectangles. If the union is just the bounding rectangle (e.g.
    # all sources stack into a clean rect), leave polygon_vertices
    # unset — consumers fall back to the bbox. A staircase / L / U
    # union produces a real polygon that we store on the row.
    union_poly, union_reason = _rectilinear_union_polygon(source_rects)
    polygon_vertices: list[list[float]] | None = None
    polygon_repair_reason: str | None = None
    if union_poly is None:
        polygon_repair_reason = union_reason
    else:
        # Strip the closing duplicate to compare against the rectangle.
        without_close = union_poly[:-1] if (len(union_poly) > 1 and
                                            union_poly[0] == union_poly[-1]
                                            ) else union_poly
        is_rect = (
            len(without_close) == 4
            and {tuple(p) for p in without_close} == {
                (bbox[0], bbox[1]), (bbox[2], bbox[1]),
                (bbox[2], bbox[3]), (bbox[0], bbox[3])
            })
        if not is_rect:
            polygon_vertices = union_poly

    ad_uuids: list[str] = []
    seen_ads: set[str] = set()
    for s in sources:
        for u in s.ad_uuids:
            if u not in seen_ads:
                seen_ads.add(u)
                ad_uuids.append(u)

    # Editorial fields: prefer the agent-supplied values, fall back
    # to the first source's value when not provided.
    first = sources[0]
    item_type = merge.get("item_type") or first.item_type
    headline  = merge.get("headline", first.headline)
    summary   = merge.get("summary",  first.summary)
    byline    = merge.get("byline",   first.byline)
    language  = merge.get("language") or first.language
    conf      = merge.get("classification_confidence")
    if conf is None:
        confs = [s.classification_confidence for s in sources
                 if s.classification_confidence is not None]
        conf = min(confs) if confs else None

    return Pass3Plan(
        derived_from=src_ids,
        edit_kind="merge",
        item_type=item_type,
        headline=headline,
        byline=byline,
        summary=summary,
        language=language,
        classification_confidence=conf,
        bbox=bbox,
        column_spans=spans,
        ad_uuids=ad_uuids,
        full_text=full_text,
        polygon_vertices=polygon_vertices,
        repair_needed=polygon_repair_reason is not None,
        repair_reason=polygon_repair_reason,
        reasoning=merge.get("reasoning"),
        uncertain=bool(merge.get("uncertain")),
    )


def _build_correction_plan(correction: dict,
                           pass2: dict[str, Pass2Item]
                           ) -> Pass3Plan:
    src_id = correction["item_id"]
    if src_id not in pass2:
        raise ValueError(
            f"correction references unknown item_id {src_id}")
    src = pass2[src_id]

    field_name = correction["field"]
    new_value = correction["new_value"]
    field_map = {
        "headline":  src.headline,
        "byline":    src.byline,
        "summary":   src.summary,
        "language":  src.language,
        "item_type": src.item_type,
    }
    field_map[field_name] = new_value

    return Pass3Plan(
        derived_from=[src_id],
        edit_kind="correction",
        item_type=field_map["item_type"],
        headline=field_map["headline"],
        byline=field_map["byline"],
        summary=field_map["summary"],
        language=field_map["language"] or "en",
        classification_confidence=src.classification_confidence,
        bbox=src.bbox,
        column_spans=[
            {
                "column_transcript_id": s["column_transcript_id"],
                "sequence":             s["sequence"],
                "start_offset":         int(s["start_offset"]),
                "end_offset":           int(s["end_offset"]),
                "bbox_top_pct":         s.get("bbox_top_pct"),
                "bbox_bottom_pct":      s.get("bbox_bottom_pct"),
            } for s in src.column_spans
        ],
        ad_uuids=list(src.ad_uuids),
        full_text=src.full_text,
        reasoning=correction.get("reasoning"),
    )


def _passthrough_plan(src: Pass2Item) -> Pass3Plan:
    """Build a plan that mirrors a pass-2 item exactly. Used when an
    inset relationship attaches to a container that wasn't otherwise
    edited — we still need a pass-3 row to hang the polygon on."""
    return Pass3Plan(
        derived_from=[src.id],
        edit_kind="inset_only",
        item_type=src.item_type,
        headline=src.headline,
        byline=src.byline,
        summary=src.summary,
        language=src.language,
        classification_confidence=src.classification_confidence,
        bbox=src.bbox,
        column_spans=[
            {
                "column_transcript_id": s["column_transcript_id"],
                "sequence":             s["sequence"],
                "start_offset":         int(s["start_offset"]),
                "end_offset":           int(s["end_offset"]),
                "bbox_top_pct":         s.get("bbox_top_pct"),
                "bbox_bottom_pct":      s.get("bbox_bottom_pct"),
            } for s in src.column_spans
        ],
        ad_uuids=list(src.ad_uuids),
        full_text=src.full_text,
        reasoning=None,
    )


def _bboxes_intersect(a: tuple[float, float, float, float],
                      b: tuple[float, float, float, float],
                      eps: float = 0.05) -> bool:
    """Strict-interior overlap test. Returns False when the rects only
    share an edge or a corner — those cases are normal layout, not
    insets. ``eps`` absorbs round-to-2-decimals jitter (the pipeline's
    canonical precision)."""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    return (al + eps < br and bl + eps < ar and
            at + eps < bb and bt + eps < ab)


def _detect_embedded_insets(
        source_rects: list[tuple[float, float, float, float]],
        *,
        source_ids: set[str],
        ad_uuids_in_plan: set[str],
        already_explicit: set[tuple[str, str | None, str | None]],
        ticket_items: list[dict],
        ticket_ads: list[dict],
        container_id_for_log: str,
        ) -> list[dict]:
    """Scan ticket items + ads for bboxes that intersect any **source
    rectangle** (not the bounding rectangle) — i.e. items that are
    truly embedded inside the merge's actual content area, rather than
    incidentally falling inside the bounding rectangle.

    Returns a list of dicts ``{kind, target_id, target_bbox}`` for
    each true embedded inset. The caller files these as repairs;
    polygon subtraction from the rectilinear union is not implemented
    (it requires a real polygon library, and the cases we see in
    practice are rare enough to surface for review rather than guess).

    Filtering rules:

    * Source items themselves (``source_ids``) — these *are* the
      merge's content, not insets within it.
    * Items / ads the agent already named in ``insets`` for this
      container (``already_explicit``).
    * Ads attached to the merge plan (``ad_uuids_in_plan``).
    * Items/ads that only share an edge with a source rect, but don't
      overlap its interior (filtered by ``_bboxes_intersect``).
    """
    found: list[dict] = []

    def _intersects_any_source(bbox: tuple[float, float, float, float]
                               ) -> bool:
        return any(_bboxes_intersect(bbox, r) for r in source_rects)

    for it in ticket_items:
        target_id = it.get("item_id")
        if not target_id or target_id in source_ids:
            continue
        if (container_id_for_log, target_id, None) in already_explicit:
            continue
        b = it.get("bbox_pct") or {}
        # Items in the ticket use {left, top, right, bottom}; ads use
        # the detected_ads four-key schema. Accept both.
        try:
            if "left" in b:
                tbox = (float(b["left"]),  float(b["top"]),
                        float(b["right"]), float(b["bottom"]))
            else:
                tbox = (float(b["x_pct"]),     float(b["y_pct"]),
                        float(b["x_end_pct"]), float(b["y_end_pct"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not _intersects_any_source(tbox):
            continue
        found.append({
            "kind": "item", "target_id": target_id, "target_bbox": tbox,
        })

    for ad in ticket_ads:
        ad_uuid = ad.get("ad_uuid")
        if not ad_uuid or ad_uuid in ad_uuids_in_plan:
            continue
        if (container_id_for_log, None, ad_uuid) in already_explicit:
            continue
        b = ad.get("bbox_pct") or {}
        try:
            if "left" in b:
                abox = (float(b["left"]),  float(b["top"]),
                        float(b["right"]), float(b["bottom"]))
            else:
                abox = (float(b["x_pct"]),     float(b["y_pct"]),
                        float(b["x_end_pct"]), float(b["y_end_pct"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not _intersects_any_source(abox):
            continue
        found.append({
            "kind": "ad", "target_id": ad_uuid, "target_bbox": abox,
        })

    return found


def _resolve_inset_target_bbox(ins: dict,
                               pass2: dict[str, Pass2Item],
                               ticket_ads: list[dict],
                               ) -> tuple[
                                       tuple[float, float, float, float],
                                       str]:
    """Find the bbox of the inset target. Returns (bbox, label).
    Label is informational (used in repair messages)."""
    if ins.get("ad_uuid"):
        for a in ticket_ads:
            if a["ad_uuid"] == ins["ad_uuid"]:
                b = a["bbox_pct"]
                return (
                    (float(b["x_pct"]),     float(b["y_pct"]),
                     float(b["x_end_pct"]), float(b["y_end_pct"])),
                    f"ad {ins['ad_uuid'][:8]}…")
        raise ValueError(
            f"inset ad_uuid not on this page: {ins['ad_uuid']}")

    target_id = ins["inset_item_id"]
    if target_id not in pass2:
        raise ValueError(
            f"inset_item_id not a pass-2 item on this page: {target_id}")
    return pass2[target_id].bbox, f"item {target_id[:8]}…"


def _attach_inset_to_plan(ins: dict,
                          plan: Pass3Plan,
                          pass2: dict[str, Pass2Item],
                          ticket_ads: list[dict]) -> None:
    """Apply one inset relationship onto an existing plan. Mutates
    ``plan.polygon_vertices`` and may set ``repair_needed``.

    Multi-inset is supported by sequential subtraction against the
    container's bounding rectangle. The cuts compose visually for
    corner+corner cases (each cut sits on a different edge of the
    container), but we don't intersect/union polygons here — if a
    second inset doesn't share an edge with the *original* container
    bbox, we fall back to a repair flag.
    """
    target_bbox, label = _resolve_inset_target_bbox(
        ins, pass2, ticket_ads)
    poly, reason = container_minus_inset(plan.bbox, target_bbox)
    if poly is None:
        plan.repair_needed = True
        plan.repair_reason = (
            f"polygon for inset ({label}) skipped: {reason}; "
            f"item kept with rectangular bbox")
        return

    if plan.polygon_vertices is None:
        plan.polygon_vertices = poly
    else:
        # A previous inset already set a polygon. We approximate by
        # also cutting the second inset out of the bbox and storing
        # whichever polygon has fewer vertices (i.e. more conservative
        # — leaves more area inside). Anything more clever needs a
        # real polygon library; flag for human review.
        plan.repair_needed = True
        plan.repair_reason = (
            "container has multiple insets; second cut not composed "
            "with first — polygon may overstate the visible area")


# ---------- ingestion ----------------------------------------------

def _col_idx_map(conn: sqlite3.Connection,
                 ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    placeholders = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, col_idx FROM column_transcripts "
        f"WHERE id IN ({placeholders})", ids).fetchall()
    return {r["id"]: int(r["col_idx"]) for r in rows}


def _copy_entity_mentions(conn: sqlite3.Connection,
                          *,
                          new_item_id: str,
                          source_item_ids: list[str]) -> int:
    """Copy entity mentions from the source pass-2 items onto the
    new pass-3 item. Junction PK is (item_id, entity_id, span_start);
    INSERT OR IGNORE collapses duplicates.

    Span offsets are pass-2 character offsets into the per-source
    full_text. After a merge they no longer index exactly into the
    pass-3 full_text; this is a known approximation called out in
    the agent prompt ('You don't extract entities. Pass-2 did that.').
    """
    if not source_item_ids:
        return 0
    placeholders = ", ".join("?" * len(source_item_ids))
    inserted = 0
    for table, fk_col in (
            ("item_people_mentions",        "person_id"),
            ("item_organizations_mentions", "organization_id"),
            ("item_places_mentions",        "place_id"),
            ("item_products_mentions",      "product_id"),
            ("item_events_mentions",        "event_id")):
        rows = conn.execute(
            f"""SELECT {fk_col}, role, mention_text,
                       span_start, span_end, confidence
                  FROM {table}
                 WHERE item_id IN ({placeholders})""",
            source_item_ids).fetchall()
        for r in rows:
            cur = conn.execute(
                f"""INSERT OR IGNORE INTO {table}
                    (item_id, {fk_col}, role, mention_text,
                     span_start, span_end, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (new_item_id, r[fk_col], r["role"],
                 r["mention_text"], r["span_start"],
                 r["span_end"], r["confidence"]))
            inserted += cur.rowcount
    return inserted


def _insert_plan(conn: sqlite3.Connection,
                 plan: Pass3Plan,
                 *,
                 year: int, month: int, day: int, page: int,
                 model: str,
                 prompt_h: str,
                 raw_response: str,
                 col_idx_by_id: dict[str, int],
                 chash: str) -> dict:
    item_id = _db.new_uuid()
    bb_left, bb_top, bb_right, bb_bottom = plan.bbox

    crosses_columns = _crosses_columns(plan.column_spans, col_idx_by_id)
    is_inset = 1 if (plan.ad_uuids and plan.column_spans) else 0

    column_span_json = json.dumps(sorted({
        col_idx_by_id[s["column_transcript_id"]]
        for s in plan.column_spans
        if s["column_transcript_id"] in col_idx_by_id
    }))

    polygon_json = (json.dumps(plan.polygon_vertices)
                    if plan.polygon_vertices else None)
    derived_json = json.dumps(plan.derived_from)

    note_parts = [f"content_hash={chash}",
                  f"edit_kind={plan.edit_kind}"]
    if plan.reasoning:
        note_parts.append(f"reason={plan.reasoning}")
    if plan.uncertain:
        note_parts.append("uncertain=true")
    notes = " | ".join(note_parts)

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
            geometry_polygon_json, derived_from_item_ids,
            created_at, notes)
           VALUES (?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?,
                   ?, ?, 0,
                   NULL, NULL,
                   ?, ?, ?, ?,
                   ?, ?,
                   ?, ?, ?,
                   ?, ?,
                   ?, ?,
                   ?, ?)""",
        (item_id, plan.item_type, year, month, day, page,
         bb_left, bb_top, bb_right, bb_bottom,
         column_span_json,
         crosses_columns, is_inset,
         plan.headline, plan.byline, plan.summary, plan.full_text,
         plan.language or "en",
         plan.classification_confidence,
         model, prompt_h, raw_response,
         1 if plan.repair_needed else 0,
         plan.repair_reason,
         polygon_json, derived_json,
         _db.now_iso(), notes))

    for s in plan.column_spans:
        conn.execute(
            """INSERT INTO item_column_spans
               (item_id, column_transcript_id, sequence,
                start_offset, end_offset,
                bbox_top_pct, bbox_bottom_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_id, s["column_transcript_id"],
             int(s["sequence"]),
             int(s["start_offset"]), int(s["end_offset"]),
             s.get("bbox_top_pct"), s.get("bbox_bottom_pct")))

    for ad_uuid in plan.ad_uuids:
        conn.execute(
            """INSERT OR IGNORE INTO item_ad_associations
               (item_id, ad_uuid) VALUES (?, ?)""",
            (item_id, ad_uuid))

    mentions_inserted = _copy_entity_mentions(
        conn,
        new_item_id=item_id,
        source_item_ids=plan.derived_from)

    return {
        "item_id": item_id,
        "spans":   len(plan.column_spans),
        "ads":     len(plan.ad_uuids),
        "mentions": mentions_inserted,
    }


def ingest(page_id: str,
           *,
           result_path: str | None = None,
           model: str | None = None) -> dict:
    """Validate and ingest one items-tidier result. Returns a small
    report. Idempotent: if the page already has pass-3 rows tagged
    with this prompt_hash, this raises ValueError so the caller can
    decide to bypass with ``--force`` (which deletes and re-ingests
    — implemented in the CLI wrapper, not here)."""
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
    pass2_prompt_h = ticket.get("pass2_prompt_hash", "")

    pass2_ids_in_ticket = [it["item_id"] for it in ticket["pass2_items"]]

    conn = _db.open_connection(attach_mvtm=False)
    repair_ids: list[str] = []
    inserted_items: list[dict] = []

    try:
        # Idempotency: refuse to ingest twice for the same prompt_hash.
        existing = conn.execute(
            """SELECT 1 FROM items
                WHERE year=? AND month=? AND day=? AND page=?
                  AND prompt_hash=?
                  AND derived_from_item_ids IS NOT NULL
                LIMIT 1""",
            (year, month, day, page, prompt_h)).fetchone()
        if existing is not None:
            raise ValueError(
                f"pass-3 rows for prompt_hash={prompt_h[:12]}… already "
                f"exist on {year:04d}-{month:02d}-{day:02d} p{page}; "
                f"delete them before re-ingesting")

        pass2 = _fetch_pass2_items(conn, pass2_ids_in_ticket)
        if len(pass2) != len(pass2_ids_in_ticket):
            missing = set(pass2_ids_in_ticket) - set(pass2)
            raise ValueError(
                f"ticket lists pass-2 ids not present in DB: {missing}")

        # Verify the pass-2 batch hash hasn't drifted under us (the
        # ticket pinned a specific prompt_hash; if rows have moved,
        # consumers will see a mismatched derivation).
        if pass2_prompt_h:
            row = conn.execute(
                """SELECT prompt_hash FROM items
                    WHERE id=? LIMIT 1""",
                (pass2_ids_in_ticket[0],)).fetchone()
            if row is not None and row["prompt_hash"] != pass2_prompt_h:
                raise ValueError(
                    f"pass-2 batch drift: ticket says "
                    f"{pass2_prompt_h[:12]}…, DB has "
                    f"{(row['prompt_hash'] or '')[:12]}…")

        # ---------- build plans ------------------------------------
        plans: list[Pass3Plan] = []
        # Map every pass-2 id touched by edit → the plan that
        # supersedes it. For inset attachment we need to find a plan
        # whose derived_from contains the container_item_id.
        edited_pass2: set[str] = set()
        plan_by_pass2: dict[str, Pass3Plan] = {}

        # Splits are not implemented yet — record a repair and skip.
        if envelope["splits"]:
            for s in envelope["splits"]:
                rid = _db.raise_repair(
                    conn,
                    target_kind="page",
                    target_ref={"year": year, "month": month,
                                "day": day, "page": page},
                    repair_kind="other",
                    description=(
                        f"items-tidier split not implemented; source "
                        f"item_id={s.get('source_item_id')}"),
                    raised_by=model,
                    related_item_id=s.get("source_item_id"))
                repair_ids.append(rid)

        for m in envelope["merges"]:
            plan = _build_merge_plan(m, pass2)
            plans.append(plan)
            for src_id in plan.derived_from:
                edited_pass2.add(src_id)
                plan_by_pass2[src_id] = plan

        for c in envelope["corrections"]:
            if c["item_id"] in edited_pass2:
                # Correction overlaps a merge — apply correction to
                # the merge plan's editorial fields rather than emit
                # a separate pass-3 row.
                plan = plan_by_pass2[c["item_id"]]
                fname = c["field"]
                if fname == "headline":  plan.headline = c["new_value"]
                elif fname == "byline":  plan.byline   = c["new_value"]
                elif fname == "summary": plan.summary  = c["new_value"]
                elif fname == "item_type":
                    plan.item_type = c["new_value"]
                elif fname == "language":
                    plan.language  = c["new_value"]
                continue
            plan = _build_correction_plan(c, pass2)
            plans.append(plan)
            edited_pass2.add(c["item_id"])
            plan_by_pass2[c["item_id"]] = plan

        explicit_inset_keys: set[tuple[str, str | None, str | None]] = set()
        for ins in envelope["insets"]:
            container_id = ins["container_item_id"]
            if container_id in plan_by_pass2:
                plan = plan_by_pass2[container_id]
            elif container_id in pass2:
                plan = _passthrough_plan(pass2[container_id])
                plans.append(plan)
                edited_pass2.add(container_id)
                plan_by_pass2[container_id] = plan
            else:
                raise ValueError(
                    f"inset.container_item_id not a pass-2 item or "
                    f"merge source: {container_id}")
            _attach_inset_to_plan(ins, plan, pass2, ticket["ads"])
            explicit_inset_keys.add(
                (container_id,
                 ins.get("inset_item_id"),
                 ins.get("ad_uuid")))

        # ---------- auto-detect embedded insets on merge plans ------
        #
        # The merge's *visible* polygon is the rectilinear union of its
        # source rectangles (computed in _build_merge_plan). Items or
        # ads whose bbox intersects any source rect — but the agent
        # didn't name as an inset and didn't include in the merge — are
        # likely true embedded insets (e.g. a display ad sitting inside
        # one of the columns the merge actually covers). Surface these
        # as repairs for human review; we do not yet subtract polygons
        # programmatically, so we don't fold them in as cuts.
        #
        # Items that incidentally fall inside the bounding rectangle
        # but outside the union polygon are *not* flagged here — by
        # construction the union excludes them, so the polygon already
        # tells the truth.
        for plan in list(plans):
            if plan.edit_kind != "merge":
                continue
            container_proxy_id = plan.derived_from[0] if plan.derived_from \
                else None
            if container_proxy_id is None:
                continue
            source_rects = [pass2[sid].bbox for sid in plan.derived_from
                            if sid in pass2]
            embedded = _detect_embedded_insets(
                source_rects,
                source_ids=set(plan.derived_from),
                ad_uuids_in_plan=set(plan.ad_uuids),
                already_explicit=explicit_inset_keys,
                ticket_items=ticket["pass2_items"],
                ticket_ads=ticket["ads"],
                container_id_for_log=container_proxy_id,
            )
            for m in embedded:
                rid = _db.raise_repair(
                    conn,
                    target_kind="page",
                    target_ref={"year": year, "month": month,
                                "day": day, "page": page},
                    repair_kind="polygon",
                    description=(
                        f"merge polygon (derived from "
                        f"{plan.derived_from[0][:8]}…) overlaps "
                        f"{m['kind']} {m['target_id'][:8]}… "
                        f"(bbox {m['target_bbox']}) which is not in "
                        f"the merge's source items or named insets. "
                        f"Likely an embedded inset (e.g. a display ad "
                        f"inside an article column); polygon "
                        f"subtraction is not yet implemented — review "
                        f"the merge or add an explicit inset."),
                    raised_by=model,
                    related_item_id=(
                        m["target_id"] if m["kind"] == "item" else None))
                repair_ids.append(rid)

        # ---------- apply ------------------------------------------
        all_col_ids = {s["column_transcript_id"]
                       for plan in plans
                       for s in plan.column_spans}
        col_idx_by_id = _col_idx_map(conn, sorted(all_col_ids))

        for plan in plans:
            rep = _insert_plan(
                conn, plan,
                year=year, month=month, day=day, page=page,
                model=model, prompt_h=prompt_h, raw_response=raw,
                col_idx_by_id=col_idx_by_id, chash=chash)
            inserted_items.append({
                **rep,
                "edit_kind":   plan.edit_kind,
                "polygon":     plan.polygon_vertices is not None,
                "derived_from": plan.derived_from,
                "repair_needed": plan.repair_needed,
            })
            if plan.repair_needed:
                rid = _db.raise_repair(
                    conn,
                    target_kind="item",
                    target_ref={"year": year, "month": month,
                                "day": day, "page": page,
                                "item_id": rep["item_id"]},
                    repair_kind="polygon",
                    description=plan.repair_reason or
                                "(no reason given)",
                    raised_by=model,
                    related_item_id=rep["item_id"])
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
        "page_id":         page_id,
        "model":           model,
        "items_inserted":  len(inserted_items),
        "polygons":        sum(1 for r in inserted_items if r["polygon"]),
        "items":           inserted_items,
        "repair_ids":      repair_ids,
        "content_hash":    chash,
        "prompt_hash":     prompt_h,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Ingest one items-tidier (pass-3) result.")
    p.add_argument("page_id",
                   help="Ticket stem, e.g. 1912-12-27_p3")
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
    print(f"  model:           {report['model']}")
    print(f"  items inserted:  {report['items_inserted']}")
    print(f"  polygons:        {report['polygons']}")
    print(f"  prompt_hash:     {report['prompt_hash'][:12]}…")
    for r in report["items"]:
        kind = r["edit_kind"]
        flag = " ⚑" if r["repair_needed"] else ""
        poly = " ◇" if r["polygon"] else ""
        print(f"    [{kind:11s}] {r['item_id'][:8]}…  "
              f"<- {len(r['derived_from'])} src  "
              f"spans={r['spans']} ads={r['ads']} "
              f"mentions={r['mentions']}{poly}{flag}")
    if report["repair_ids"]:
        print(f"  repairs raised:  {len(report['repair_ids'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
