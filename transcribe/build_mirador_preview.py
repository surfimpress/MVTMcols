"""Build a two-pass IIIF preview for one issue.

Produces, under ``preview/iiif/<YYYY-MM-DD>/``:

  * ``manifest_pass1.json`` — column + ad transcripts (one canvas per
    page; annotations placed on the column boundaries from
    ``mvtm.page_layouts`` and on each ``mvtm.detected_ads`` bbox).
    Skipped when no ``column_transcripts`` or ``ad_transcripts`` rows
    exist for the issue.
  * ``manifest_pass2.json`` — items (segmentation + classification +
    entity mentions). Annotations placed on stored item bboxes from
    ``transcribe.items``. Skipped when no items exist for the issue.
  * ``mirador.html`` — a per-issue copy of the central viewer. The
    HTML derives the issue date from its own path, so the same source
    works at the central ``preview/iiif/mirador.html?issue=...`` path
    and the per-issue path. The per-issue copy is what
    ``viewer.html`` links to today.

Image bodies in the manifests reference the existing
``columns/<issue>/p<N>/page_display.avif`` files — same asset the
main viewer uses. We deliberately do not pre-render a separate JPG
per page: it duplicates a known-good asset, costs disk, and lives
under a different Cloudflare Access path than the main viewer.

Usage::

    python3 -m transcribe.build_mirador_preview 1912-12-27
    python3 -m transcribe.build_mirador_preview 1912-12-27 1912-05-31

Why a separate module: this is preview-side glue, not part of the
cutting pipeline. It reads ``mvtm.db`` read-only and pulls
transcripts from ``transcribe.db``; nothing here writes either DB.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
MVTM_DB = ROOT / "data" / "mvtm.db"
TXC_DB = ROOT / "transcribe" / "data" / "transcribe.db"
COLUMNS_DIR = ROOT / "columns"
PREVIEW_ROOT = ROOT / "preview" / "iiif"
CENTRAL_VIEWER = PREVIEW_ROOT / "mirador.html"
PUBLIC_BASE = "https://mcmniintstdio.surfaceimpression.com/MVTM"


# ---------------------------------------------------------------- helpers ---

JK = {"people": "person_id", "organizations": "organization_id",
      "places": "place_id", "products": "product_id", "events": "event_id"}
NAMECOL = {"people": "full_name", "organizations": "name",
           "places": "name", "products": "name", "events": "name"}

TYPE_LABEL_OVERRIDES = {
    "patent_medicine": "Medicines and Remedies",
    "financial_services": "Financial Products",
}


def humanise_type(snake: str) -> str:
    if not snake:
        return ""
    s = snake.strip().lower()
    if s in TYPE_LABEL_OVERRIDES:
        return TYPE_LABEL_OVERRIDES[s]
    return " ".join(w.capitalize() if w.lower() not in ("and", "of", "the") else w
                    for w in s.replace("-", "_").split("_"))


def render_transcript(text: str) -> str:
    """Mirador strips style attrs and most tags; whitelist:
       p, br, hr, b, strong, i, em, small, a.
       Blank lines → paragraph break; single newlines → <br>."""
    if not text:
        return ""
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    blocks, cur = [], []
    for line in lines:
        if line == "":
            if cur:
                blocks.append("\n".join(cur)); cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    out = []
    for blk in blocks:
        s = blk.strip()
        if s == "---" or s == "--":
            out.append("<hr>")
            continue
        esc_lines = [html.escape(l) for l in blk.split("\n")]
        out.append("<p>" + "<br>".join(esc_lines) + "</p>")
    return "".join(out)


def fragment_xywh_pct(x_pct, y_pct, x2_pct, y2_pct, W, H):
    x = round(x_pct / 100 * W)
    y = round(y_pct / 100 * H)
    w = round((x2_pct - x_pct) / 100 * W)
    h = round((y2_pct - y_pct) / 100 * H)
    return f"xywh={x},{y},{w},{h}"


# ------------------------------------------------------------- discovery ---

def issue_pages(con_mvtm, year: int, month: int, day: int) -> list[int]:
    """Return the page numbers we have layout for, sorted."""
    rows = con_mvtm.execute(
        "SELECT page FROM page_layouts "
        "WHERE year=? AND month=? AND day=? ORDER BY page",
        (year, month, day),
    ).fetchall()
    return [r[0] for r in rows]


def page_dims(issue_dir: str, pages: list[int]) -> dict:
    """{page: (W, H)} pulled from page_display.avif on disk.

    The avif is what the manifest body references, so canvas dims must
    match. PIL doesn't always need the avif plugin to read width/height
    — Pillow ≥10 has built-in AVIF support — but if the open fails we
    fall back to ``page_raw.png`` so we still produce a valid manifest.
    """
    out = {}
    for p in pages:
        for fname in ("page_display.avif", "page_raw.png"):
            path = COLUMNS_DIR / issue_dir / f"p{p}" / fname
            if not path.exists():
                continue
            try:
                with Image.open(path) as im:
                    out[p] = im.size
                    break
            except Exception:
                continue
        if p not in out:
            raise FileNotFoundError(
                f"no readable page image for {issue_dir} p{p}")
    return out


# --------------------------------------------------------------- canvases ---

def make_canvas(issue: str, page: int, W: int, H: int):
    base = f"{PUBLIC_BASE}/preview/iiif/{issue}"
    cid = f"{base}/manifest.json/canvas/p{page}"
    img_url = f"{PUBLIC_BASE}/columns/{issue}/p{page}/page_display.avif"
    return cid, {
        "id": cid, "type": "Canvas",
        "label": {"en": [f"p.{page}"]},
        "width": W, "height": H,
        "items": [{
            "id": f"{cid}/page", "type": "AnnotationPage",
            "items": [{
                "id": f"{cid}/page/anno-paint",
                "type": "Annotation", "motivation": "painting",
                "body": {
                    "id": img_url,
                    "type": "Image", "format": "image/avif",
                    "width": W, "height": H,
                },
                "target": cid,
            }],
        }],
        "annotations": [],
    }


# --------------------------------------------------------------- pass-1 ---

def build_pass1(con_mvtm, con_txc, issue: str, year: int, month: int,
                day: int, pages: list[int], dims: dict):
    """Multi-canvas manifest with column transcripts + ad transcripts."""
    canvases = []
    total_cols = 0
    total_ads = 0

    boundaries = {}
    for row in con_mvtm.execute(
        "SELECT page, boundary_positions FROM page_layouts "
        "WHERE year=? AND month=? AND day=? ORDER BY page",
        (year, month, day),
    ):
        boundaries[row[0]] = json.loads(row[1])

    for page in pages:
        W, H = dims[page]
        cid, canvas = make_canvas(issue, page, W, H)
        anno_page_id = f"{cid}/anno-pass1"
        anno_page = {
            "id": anno_page_id, "type": "AnnotationPage", "items": []
        }

        page_boundaries = boundaries.get(page, [])
        col_rows = list(con_txc.execute(
            "SELECT id, col_idx, transcript_text, transcriber_notes, "
            "       quality_flags, repair_needed, repair_reason, model "
            "FROM column_transcripts "
            "WHERE year=? AND month=? AND day=? AND page=? "
            "  AND status='done' "
            "ORDER BY col_idx",
            (year, month, day, page),
        ))
        for row in col_rows:
            cid_id, col_idx, text, notes, qflags, rneeded, rreason, model = row
            if col_idx >= len(page_boundaries) - 1:
                continue
            x1 = page_boundaries[col_idx]
            x2 = page_boundaries[col_idx + 1]
            badges = [f"col {col_idx}"]
            if model:
                badges.append(model)
            if rneeded:
                badges.append("repair")
            qf = []
            try:
                qf = json.loads(qflags) if qflags else []
            except Exception:
                qf = []
            if qf:
                badges.extend(qf)
            parts = ["<p><small>" + html.escape(" · ".join(badges)) + "</small></p>"]
            if rneeded and rreason:
                parts.append("<p><em>repair:</em> " + html.escape(rreason) + "</p>")
            if notes:
                parts.append("<p><em>notes:</em> " + html.escape(notes) + "</p>")
            if text:
                parts.append("<hr>")
                parts.append(render_transcript(text))
            body_html = "".join(parts)
            anno_page["items"].append({
                "id": f"{anno_page_id}/col-{cid_id}",
                "type": "Annotation", "motivation": "commenting",
                "body": [
                    {"type": "TextualBody", "format": "text/html",
                     "value": body_html},
                    {"type": "TextualBody", "format": "text/plain",
                     "purpose": "tagging", "value": "column"},
                ],
                "target": {
                    "type": "SpecificResource", "source": cid,
                    "selector": {"type": "FragmentSelector",
                                 "conformsTo": "http://www.w3.org/TR/media-frags/",
                                 "value": fragment_xywh_pct(x1, 0, x2, 100, W, H)},
                },
            })
            total_cols += 1

        for ad_row in con_mvtm.execute(
            "SELECT uuid, x_pct, y_pct, x_end_pct, y_end_pct "
            "FROM detected_ads "
            "WHERE year=? AND month=? AND day=? AND page=? "
            "ORDER BY y_pct, x_pct",
            (year, month, day, page),
        ):
            ad_uuid, ax1, ay1, ax2, ay2 = ad_row
            txc = con_txc.execute(
                "SELECT id, transcript_text, transcriber_notes, quality_flags, "
                "       repair_needed, repair_reason, model "
                "FROM ad_transcripts "
                "WHERE ad_uuid=? AND status='done' "
                "ORDER BY created_at DESC LIMIT 1",
                (ad_uuid,),
            ).fetchone()
            badges = ["ad"]
            parts = []
            if txc:
                tid, text, notes, qflags, rneeded, rreason, model = txc
                if model:
                    badges.append(model)
                if rneeded:
                    badges.append("repair")
                qf = []
                try:
                    qf = json.loads(qflags) if qflags else []
                except Exception:
                    qf = []
                if qf:
                    badges.extend(qf)
                parts.append("<p><small>" + html.escape(" · ".join(badges)) + "</small></p>")
                if rneeded and rreason:
                    parts.append("<p><em>repair:</em> " + html.escape(rreason) + "</p>")
                if notes:
                    parts.append("<p><em>notes:</em> " + html.escape(notes) + "</p>")
                if text:
                    parts.append("<hr>")
                    parts.append(render_transcript(text))
            else:
                badges.append("not yet transcribed")
                parts.append("<p><small>" + html.escape(" · ".join(badges)) + "</small></p>")
                parts.append("<p><em>(ad transcript pending — pass-1B)</em></p>")
            body_html = "".join(parts)
            anno_page["items"].append({
                "id": f"{anno_page_id}/ad-{ad_uuid}",
                "type": "Annotation", "motivation": "commenting",
                "body": [
                    {"type": "TextualBody", "format": "text/html",
                     "value": body_html},
                    {"type": "TextualBody", "format": "text/plain",
                     "purpose": "tagging", "value": "ad"},
                ],
                "target": {
                    "type": "SpecificResource", "source": cid,
                    "selector": {"type": "FragmentSelector",
                                 "conformsTo": "http://www.w3.org/TR/media-frags/",
                                 "value": fragment_xywh_pct(ax1, ay1, ax2, ay2, W, H)},
                },
            })
            total_ads += 1

        canvas["annotations"] = [anno_page]
        canvases.append(canvas)

    base = f"{PUBLIC_BASE}/preview/iiif/{issue}"
    manifest = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": f"{base}/manifest_pass1.json", "type": "Manifest",
        "label": {"en": [f"Almonte Gazette — {issue} (pass-1: column + ad transcripts)"]},
        "metadata": [
            {"label": {"en": ["Issue date"]}, "value": {"en": [issue]}},
            {"label": {"en": ["Pass"]},
             "value": {"en": ["1 — raw column and ad transcripts"]}},
            {"label": {"en": ["Columns transcribed"]},
             "value": {"en": [str(total_cols)]}},
            {"label": {"en": ["Ads transcribed"]},
             "value": {"en": [str(total_ads)]}},
            {"label": {"en": ["Source"]},
             "value": {"en": ["Mississippi Valley Textile Museum / Almonte Gazette archive"]}},
        ],
        "rights": "http://rightsstatements.org/vocab/UND/1.0/",
        "items": canvases,
    }
    return manifest, total_cols, total_ads


# --------------------------------------------------------------- pass-2 ---

def ents(con_txc, item_id, table):
    jk = JK[table]; col = NAMECOL[table]
    rows = con_txc.execute(
        f"SELECT DISTINCT e.{col} FROM item_{table}_mentions m "
        f"JOIN {table} e ON e.id=m.{jk} WHERE m.item_id=? ORDER BY e.{col}",
        (item_id,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def product_categories(con_txc, item_id):
    rows = con_txc.execute(
        "SELECT p.name, p.manufacturer, p.product_type "
        "FROM item_products_mentions m JOIN products p ON p.id=m.product_id "
        "WHERE m.item_id=?",
        (item_id,),
    ).fetchall()
    cats, seen = [], set()
    for name, manuf, ptype in rows:
        ptype_l = (ptype or "").strip().lower()
        if ptype_l and ptype_l != "other":
            label = humanise_type(ptype_l)
        else:
            fb = manuf or name or ""
            if not fb:
                continue
            label = f"{fb} (uncategorised)"
        if label not in seen:
            seen.add(label); cats.append(label)
    cats.sort()
    return cats


def linked_ad_text(con_txc, item_id):
    rows = con_txc.execute(
        "SELECT t.transcript_text FROM item_ad_associations a "
        "JOIN ad_transcripts t ON t.ad_uuid=a.ad_uuid "
        "WHERE a.item_id=? ORDER BY t.id",
        (item_id,),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def item_body_html(idx, it, con_txc):
    badges = [f"#{idx}", it["item_type"]]
    if it["crosses_columns"]:
        badges.append("cross-col")
    if it["is_inset"]:
        badges.append("inset")
    if it["classification_confidence"] is not None:
        badges.append(f'conf {it["classification_confidence"]:.2f}')
    parts = ["<p><small>" + html.escape(" · ".join(badges)) + "</small></p>"]
    if it["headline"]:
        parts.append("<p><b>" + html.escape(it["headline"]) + "</b></p>")
    if it["byline"]:
        parts.append("<p><em>by " + html.escape(it["byline"]) + "</em></p>")
    full = it["full_text"] or ""
    ads = linked_ad_text(con_txc, it["id"])
    pieces = []
    if it["item_type"] == "display_ad" and ads:
        pieces.extend(ads)
    else:
        if full.strip():
            pieces.append(full)
        pieces.extend(ads)
    combined = "\n\n---\n\n".join(pieces) if pieces else ""
    if combined:
        parts.append("<hr>")
        parts.append("<p><b>Transcript</b></p>")
        parts.append(render_transcript(combined))
    chip_parts = []
    for label, table in [("People", "people"), ("Orgs", "organizations"),
                         ("Places", "places")]:
        names = ents(con_txc, it["id"], table)
        if names:
            chip_parts.append("<p><b>" + label + ":</b> "
                              + html.escape(", ".join(names)) + "</p>")
    pcats = product_categories(con_txc, it["id"])
    if pcats:
        chip_parts.append("<p><b>Products:</b> " + html.escape(", ".join(pcats)) + "</p>")
    enames = ents(con_txc, it["id"], "events")
    if enames:
        chip_parts.append("<p><b>Events:</b> " + html.escape(", ".join(enames)) + "</p>")
    if chip_parts:
        parts.append("<hr>")
        parts.extend(chip_parts)
    return "".join(parts)


def build_pass2(con_txc, issue: str, year: int, month: int, day: int,
                pages: list[int], dims: dict):
    canvases = []
    total_items = 0
    pages_with_items = []

    for page in pages:
        W, H = dims[page]
        cid, canvas = make_canvas(issue, page, W, H)
        anno_page_id = f"{cid}/anno-pass2"
        anno_page = {
            "id": anno_page_id, "type": "AnnotationPage", "items": []
        }
        items = list(con_txc.execute(
            "SELECT id, item_type, headline, byline, full_text, "
            "       classification_confidence, crosses_columns, is_inset, "
            "       bbox_left_pct, bbox_top_pct, bbox_right_pct, bbox_bottom_pct "
            "FROM items WHERE year=? AND month=? AND day=? AND page=? "
            "ORDER BY bbox_top_pct",
            (year, month, day, page),
        ))
        col_names = ["id", "item_type", "headline", "byline", "full_text",
                     "classification_confidence", "crosses_columns", "is_inset",
                     "bbox_left_pct", "bbox_top_pct",
                     "bbox_right_pct", "bbox_bottom_pct"]
        for idx, row in enumerate(items, 1):
            it = dict(zip(col_names, row))
            anno_page["items"].append({
                "id": f"{anno_page_id}/item-{it['id']}",
                "type": "Annotation", "motivation": "commenting",
                "body": [
                    {"type": "TextualBody", "format": "text/html",
                     "value": item_body_html(idx, it, con_txc)},
                    {"type": "TextualBody", "format": "text/plain",
                     "purpose": "tagging", "value": it["item_type"]},
                ],
                "target": {
                    "type": "SpecificResource", "source": cid,
                    "selector": {
                        "type": "FragmentSelector",
                        "conformsTo": "http://www.w3.org/TR/media-frags/",
                        "value": fragment_xywh_pct(
                            it["bbox_left_pct"], it["bbox_top_pct"],
                            it["bbox_right_pct"], it["bbox_bottom_pct"],
                            W, H),
                    },
                },
            })
            total_items += 1
        if items:
            pages_with_items.append(page)
        canvas["annotations"] = [anno_page]
        canvases.append(canvas)

    base = f"{PUBLIC_BASE}/preview/iiif/{issue}"
    manifest = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": f"{base}/manifest_pass2.json", "type": "Manifest",
        "label": {"en": [f"Almonte Gazette — {issue} (pass-2: items)"]},
        "metadata": [
            {"label": {"en": ["Issue date"]}, "value": {"en": [issue]}},
            {"label": {"en": ["Pass"]},
             "value": {"en": ["2 — items, segmentation, classification, entity mentions"]}},
            {"label": {"en": ["Pages with items"]},
             "value": {"en": [", ".join(f"p{p}" for p in pages_with_items) or "(none yet)"]}},
            {"label": {"en": ["Items detected"]},
             "value": {"en": [str(total_items)]}},
            {"label": {"en": ["Source"]},
             "value": {"en": ["Mississippi Valley Textile Museum / Almonte Gazette archive"]}},
        ],
        "rights": "http://rightsstatements.org/vocab/UND/1.0/",
        "items": canvases,
    }
    return manifest, total_items, pages_with_items


# --------------------------------------------------------------- footprints ---

def has_pass1_data(con_txc, year: int, month: int, day: int) -> bool:
    n = con_txc.execute(
        "SELECT COUNT(*) FROM column_transcripts "
        "WHERE year=? AND month=? AND day=? AND status='done'",
        (year, month, day),
    ).fetchone()[0]
    if n:
        return True
    n = con_txc.execute(
        "SELECT COUNT(*) FROM ad_transcripts "
        "WHERE year=? AND month=? AND day=? AND status='done'",
        (year, month, day),
    ).fetchone()[0]
    return bool(n)


def has_pass2_data(con_txc, year: int, month: int, day: int) -> bool:
    n = con_txc.execute(
        "SELECT COUNT(*) FROM items "
        "WHERE year=? AND month=? AND day=?",
        (year, month, day),
    ).fetchone()[0]
    return bool(n)


# --------------------------------------------------------------- main ---

def build_one(issue: str) -> dict:
    """Build manifests + per-issue mirador.html for one issue.

    Returns a small report dict. Skips silently when neither pass has
    any data — avoids creating empty preview folders.
    """
    try:
        year, month, day = (int(x) for x in issue.split("-"))
    except Exception:
        raise ValueError(f"bad issue date: {issue!r} (want YYYY-MM-DD)")

    issue_columns_dir = COLUMNS_DIR / issue
    if not issue_columns_dir.is_dir():
        raise FileNotFoundError(
            f"no cutting-pipeline output at {issue_columns_dir}; "
            f"run the cutting pipeline for {issue} first")

    with closing(sqlite3.connect(f"file:{MVTM_DB}?mode=ro", uri=True)) as cm, \
         closing(sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)) as ct:

        pages = issue_pages(cm, year, month, day)
        if not pages:
            raise ValueError(f"no page_layouts rows for {issue}")

        do_pass1 = has_pass1_data(ct, year, month, day)
        do_pass2 = has_pass2_data(ct, year, month, day)
        if not do_pass1 and not do_pass2:
            return {"issue": issue, "skipped": True,
                    "reason": "no transcribe data yet"}

        dims = page_dims(issue, pages)

        out_dir = PREVIEW_ROOT / issue
        out_dir.mkdir(parents=True, exist_ok=True)

        report = {"issue": issue, "pages": len(pages),
                  "pass1": None, "pass2": None}

        if do_pass1:
            manifest, ncols, nads = build_pass1(
                cm, ct, issue, year, month, day, pages, dims)
            (out_dir / "manifest_pass1.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2))
            report["pass1"] = {"columns": ncols, "ads": nads}
        if do_pass2:
            manifest, nitems, ppages = build_pass2(
                ct, issue, year, month, day, pages, dims)
            (out_dir / "manifest_pass2.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2))
            report["pass2"] = {"items": nitems,
                               "pages_with_items": ppages}

        # Per-issue mirador.html — copy of the central viewer. The HTML
        # derives the issue date from its URL path, so a vanilla copy
        # works without any per-issue templating.
        if CENTRAL_VIEWER.exists():
            shutil.copy2(CENTRAL_VIEWER, out_dir / "mirador.html")
            report["mirador_html"] = True
        else:
            report["mirador_html"] = False

    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Build IIIF preview manifests for one or more issues.")
    p.add_argument("dates", nargs="+",
                   help="Issue dates as YYYY-MM-DD")
    args = p.parse_args(argv)

    rc = 0
    for d in args.dates:
        try:
            r = build_one(d)
        except (FileNotFoundError, ValueError) as e:
            print(f"!!! {d}: {e}", file=sys.stderr)
            rc = 1
            continue
        if r.get("skipped"):
            print(f"--- {d}: {r.get('reason')}")
            continue
        bits = [f"{r['pages']} pages"]
        if r["pass1"]:
            bits.append(f"pass-1 ({r['pass1']['columns']} cols, "
                        f"{r['pass1']['ads']} ads)")
        if r["pass2"]:
            bits.append(f"pass-2 ({r['pass2']['items']} items on "
                        f"{len(r['pass2']['pages_with_items'])} pages)")
        bits.append("mirador.html" if r["mirador_html"] else "(no central viewer)")
        print(f"=== {d}: " + ", ".join(bits))
    return rc


if __name__ == "__main__":
    sys.exit(main())
