"""Stage 7 (starting point): IIIF Presentation 3.0 manifests showing the
RAW Tesseract hOCR as annotation layers.

Deliberately dumb and faithful: this replicates what is already in the
.hocr files, including the "stray" blocks the production parser drops
(`ocr_separator`, `ocr_photo`). Nothing here is derived, scored, or
corrected -- it is a window onto the OCR as it actually came out, so the
input to every later stage can be judged by eye.

Layer design follows the Presentation 3.0 spec: the page image is a
`painting` annotation in Canvas `items`; every hOCR layer is a separate
labelled `AnnotationPage` in Canvas `annotations` with
`motivation: "supplementing"`, targeted with an `#xywh=` fragment. That
is what lets a viewer list and toggle them independently.

Coordinate note: hOCR boxes are in full-resolution page space (e.g.
3908x5655) but the canvas uses `page_display.png` (1400px wide). The DB
stores page-percentages, so everything is converted pct -> display px
here. Getting this wrong is the pipeline's classic wrong-origin bug.

Usage::

    python3 -m transcribe.scaled.build_iiif 1980-04-06 1997-07-16
"""

from __future__ import annotations

import argparse
import json
import os

from . import _support as _sup
from . import detect_grid as _detect_grid
from . import detect_hlines as _detect_hlines
from . import detect_content_area as _detect_content_area
from . import detect_zones as _detect_zones
from . import detect_captions as _detect_captions


def _slug(label: str) -> str:
    """A label as an id fragment: lowercase, alphanumerics, hyphen-joined."""
    out, word = [], []
    for ch in label.lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return "-".join(out) or "layer"


# The repo root is served here (behind Cloudflare Access -- a browser
# session can read it, an unauthenticated third-party server cannot).
PUBLIC_BASE = "https://mcmniintstdio.surfaceimpression.com/MVTM"
OUT_REL = os.path.join("preview", "scaled", "iiif")

# Layers, in the order a reviewer most wants them. Colour is advisory for
# our own viewer; blue/orange/black-ish choices stay distinguishable
# without relying on hue discrimination.
BLOCK_LAYERS = [
    ("ocr_carea", "Text blocks (ocr_carea)", "#0a5ac8"),
    ("ocr_separator", "STRAY: printed rules (ocr_separator)", "#e07800"),
    ("ocr_photo", "STRAY: image regions (ocr_photo)", "#00964f"),
]
LINE_LAYERS = [
    ("ocr_line", "Lines: body (ocr_line)", "#555555"),
    ("ocr_header", "Lines: Tesseract headings (ocr_header)", "#c8007a"),
    ("ocr_caption", "Lines: Tesseract captions (ocr_caption)", "#7a3ec8"),
    ("ocr_textfloat", "Lines: floats (ocr_textfloat)", "#0a8ec8"),
]


def _esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _manifest_name(variant: str) -> str:
    return "manifest.json" if variant == "all" else f"manifest_{variant}.json"


def _rel_url(abs_path: str) -> str:
    rel = os.path.relpath(abs_path, _sup.REPO_ROOT).replace(os.sep, "/")
    return f"{PUBLIC_BASE}/{rel}"


def _anno(anno_id, canvas_id, x, y, w, h, text, label, granularity=None,
          kind=None, detail=None):
    """One supplementing annotation over a canvas region.

    `granularity` emits the IIIF Text Granularity extension's
    `textGranularity` property, which declares *what unit of text* this
    annotation covers -- so a client can tell a block-level transcription
    from a line-level one instead of guessing. Allowed values are
    page|block|paragraph|line|word|glyph. The extension requires
    motivation `supplementing`, which is what we already use.

    Deliberately omitted for ocr_separator / ocr_photo: those regions
    carry no text at all, so no granularity honestly applies to them.
    """
    # Every annotation carries a type subhead in its own body, so the
    # structure is legible in the viewer without cross-referencing which
    # layer you happen to be looking at. Mirador sanitises HTML bodies;
    # a <small> line plus the text survives that.
    if kind:
        head = kind if not detail else f"{kind} &middot; {detail}"
        value = (f"<small><b>{head}</b></small>"
                 + (f"<br>{_esc(text)}" if text and not text.startswith("(no text") else ""))
        body = {"type": "TextualBody", "format": "text/html",
                "language": "en", "value": value}
    else:
        body = {"type": "TextualBody", "format": "text/plain",
                "language": "en", "value": text}
    a = {
        "id": anno_id,
        "type": "Annotation",
        "motivation": "supplementing",
        "body": body,
        "target": f"{canvas_id}#xywh={x},{y},{w},{h}",
        "label": {"en": [label]},
    }
    if granularity:
        a["textGranularity"] = granularity
    return a


# Layer subsets. Block and line boxes overlap heavily when drawn
# together, which makes column structure hard to read -- so each subset
# also gets its own manifest. "blocks" is the one to study columns with
# (careas plus the vertical rules that ARE column boundaries); "lines"
# shows the flush-left edges that the leftedge signal clusters on.
VARIANTS = {
    "all": (True, True),
    "blocks": (True, False),
    "lines": (False, True),
}

# Derived stages get their own manifests so the viewer can step through
# the pipeline: Tesseract (raw) -> Columns -> Items -> Refined. Items and
# Refined are not built yet; the viewer shows them disabled rather than
# pretending they exist.
DERIVED = ("content", "grid", "hlines", "boxes", "captions", "boxphotos",
           "separators", "photos", "overlay")


def _derived_layers(conn, page_id, cid, W, H, variant):
    """Annotation pages for a derived stage. Returns [] when the stage has
    produced nothing for this page, so an empty layer never masquerades as
    a real result."""
    out = []
    if variant == "grid":
        # ONE layer: the fitted lattice. The former "columns (2)"
        # per-edge refinement was archived 2026-08-15 (see
        # transcribe/scaled/archive/refine_columns.py).
        res = _detect_grid.detect(conn, page_id)
        g = res.get("grid")
        if not g or not res.get("columns"):
            return out

        cols = res["columns"]
        boxes = []
        for i, c in enumerate(cols):
            x0 = _sup.pct_to_px(c["left_pct"], W)
            x1 = _sup.pct_to_px(c["right_pct"], W)
            gut = (cols[i + 1]["left_pct"] - c["right_pct"]) if i + 1 < len(cols) else None
            boxes.append(_anno(
                f"{cid}/anno/grid/{c['col_idx']}", cid, x0, 0, max(1, x1 - x0), H,
                "", f"column {c['col_idx']}", kind="column",
                detail=f"col {c['col_idx']} · {c['left_pct']:.2f}%-{c['right_pct']:.2f}% "
                       f"(w {c['right_pct'] - c['left_pct']:.2f}%)"
                       + (f" · gutter {gut:+.2f}%" if gut is not None else " · right margin")))
        label = (f"Columns — {g['n_columns']} @ pitch {g['pitch']}%, "
                 f"gutter {g['gutter']}%")
        if res.get("low_evidence"):
            label += f" · LOW EVIDENCE ({res['n_lines']} text lines)"
        out.append((label, boxes))
    if variant in ("captions", "boxphotos"):
        # ONE layer: the encompassing rectangle per photo -- photo plus
        # its caption, or just the photo where no caption was found. A
        # photo and its caption are one editorial unit, and drawing the
        # halves separately only cluttered the page. Everything else
        # (line count, legs, ocr_caption tagging, the caption text) is
        # kept and surfaced in the annotation detail instead.
        res = _detect_captions.detect(conn, page_id)
        boxes = []
        for i, pr in enumerate(res["pairs"]):
            p_, c = pr["photo"], pr["caption"]
            L, T, R, B = _detect_captions.photo_unit(pr)
            if c:
                legs = f", {c['n_runs']} legs" if c["n_runs"] > 1 else ""
                detail = (f"photo {p_['L']:.2f}%-{p_['R']:.2f}% x "
                          f"{p_['T']:.2f}%-{p_['B']:.2f}% · caption "
                          f"{c['n_lines']} lines{legs}"
                          + (" · Tesseract tagged ocr_caption"
                             if c["tesseract_caption"] else "")
                          + " — " + (c["text"][:180] or ""))
                kind = "photo + caption"
            else:
                detail = (f"photo {L:.2f}%-{R:.2f}% x {T:.2f}%-{B:.2f}%"
                          " · no caption found")
                kind = "photo, no caption"
            x0, x1 = _sup.pct_to_px(L, W), _sup.pct_to_px(R, W)
            y0, y1 = _sup.pct_to_px(T, H), _sup.pct_to_px(B, H)
            boxes.append(_anno(
                f"{cid}/anno/cap/{i}", cid, x0, y0,
                max(1, x1 - x0), max(1, y1 - y0), "",
                f"photo + caption {i}", kind=kind, detail=detail))
        if boxes:
            out.append((f"Photos with captions ({len(boxes)}, "
                        f"{res['n_captioned']} captioned)", boxes))

    if variant == "photos":
        # Tesseract's ocr_photo regions on their own. Split into the ones
        # that survive the size/edge filter and the ones that don't:
        # Tesseract reports the sheet edge and binding shadow as photos
        # too (a 0.3%-wide sliver at x 99.7 on 1980-04-06 p1), and seeing
        # which is which is the point of having this layer.
        kept = _detect_captions.real_photos(conn, page_id)
        keptset = {(round(k["L"], 2), round(k["T"], 2)) for k in kept}
        rows = [dict(r) for r in conn.execute(
            "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B "
            "FROM page_hocr_regions WHERE page_id=? AND region_class='ocr_photo' "
            "ORDER BY top_pct", (page_id,))]
        groups = {"Photos": [], "Rejected as scan artefacts": []}
        for i, r in enumerate(rows):
            key = "Photos" if (round(r["L"], 2), round(r["T"], 2)) in keptset \
                  else "Rejected as scan artefacts"
            x0, x1 = _sup.pct_to_px(r["L"], W), _sup.pct_to_px(r["R"], W)
            y0, y1 = _sup.pct_to_px(r["T"], H), _sup.pct_to_px(r["B"], H)
            groups[key].append(_anno(
                f"{cid}/anno/photo/{i}", cid, x0, y0,
                max(1, x1 - x0), max(1, y1 - y0), "", "ocr_photo",
                kind="ocr_photo",
                detail=f"{r['L']:.2f}%-{r['R']:.2f}% x "
                       f"{r['T']:.2f}%-{r['B']:.2f}% "
                       f"({r['R'] - r['L']:.2f} x {r['B'] - r['T']:.2f})"))
        for label, boxes in groups.items():
            if boxes:
                out.append((f"{label} ({len(boxes)})", boxes))

    if variant == "separators":
        # Tesseract's raw ocr_separator regions, undigested. This is the
        # signal every ruled-structure stage is built on, so being able to
        # see it directly is what makes those stages debuggable -- it is
        # how the box detector's failures were diagnosed.
        for orient, label in (("vertical", "Vertical rules"),
                              ("horizontal", "Horizontal rules")):
            rows = [dict(r) for r in conn.execute(
                "SELECT left_pct L, top_pct T, right_pct R, bottom_pct B, "
                "width_px wd, height_px ht FROM page_hocr_regions "
                "WHERE page_id=? AND region_class='ocr_separator' "
                "AND orientation=? ORDER BY top_pct, left_pct",
                (page_id, orient))]
            if not rows:
                continue
            boxes = []
            for i, r in enumerate(rows):
                x0, x1 = _sup.pct_to_px(r["L"], W), _sup.pct_to_px(r["R"], W)
                y0, y1 = _sup.pct_to_px(r["T"], H), _sup.pct_to_px(r["B"], H)
                # Thickness is the point of this layer as much as position
                # -- it distinguishes a hairline from a drop shadow -- so
                # the box is drawn at its true size, never padded.
                thick = r["wd"] if orient == "vertical" else r["ht"]
                boxes.append(_anno(
                    f"{cid}/anno/sep/{orient}/{i}", cid, x0, y0,
                    max(1, x1 - x0), max(1, y1 - y0), "",
                    f"{orient} rule", kind=f"ocr_separator ({orient})",
                    detail=f"{r['L']:.2f}%-{r['R']:.2f}% x "
                           f"{r['T']:.2f}%-{r['B']:.2f}% · "
                           f"{thick or '?'}px thick"))
            out.append((f"{label} ({len(boxes)})", boxes))

    if variant in ("boxes", "boxphotos"):
        # Zones from the grid: RAW Tesseract separators -> square cells ->
        # corners -> one predicate (no corner may interrupt a side).
        # Content travels with each zone as evidence, never as a filter.
        res = _detect_zones.detect(conn, page_id)
        zones = res["zones"]
        if zones:
            out.append((f"Boxed zones ({len(zones)})", [
                _anno(f"{cid}/anno/zone/{z['idx']}", cid,
                      _sup.pct_to_px(z["left_pct"], W),
                      _sup.pct_to_px(z["top_pct"], H),
                      max(1, _sup.pct_to_px(z["right_pct"], W)
                          - _sup.pct_to_px(z["left_pct"], W)),
                      max(1, _sup.pct_to_px(z["bottom_pct"], H)
                          - _sup.pct_to_px(z["top_pct"], H)),
                      "", f"zone {z['idx']}", kind="boxed zone",
                      detail=f"{z['left_pct']:.2f}%-{z['right_pct']:.2f}% x "
                             f"{z['top_pct']:.2f}%-{z['bottom_pct']:.2f}%"
                             + (f" · columns {z['col_lo']}-{z['col_hi']}"
                                if z["col_lo"] is not None else "")
                             + f" · {len(z['blocks'])} blocks, "
                               f"{z['n_lines']} lines, {z['n_photos']} photos"
                             + (f" · {z['reasons']}" if z["reasons"] else "")
                             + (f" · FLAGS {z['flags']}" if z["flags"] else ""))
                for z in zones]))

    if variant == "hlines":
        # Stage 3: horizontal alignments. Each is drawn only across the
        # columns it spans -- that span IS the claim, and a page-wide box
        # would hide the thing worth checking.
        res = _detect_hlines.detect(conn, page_id)
        cols = _detect_grid.detect(conn, page_id).get("columns") or []
        if not res.get("alignments") or not cols:
            return out

        # Grouped by how many columns agree, so a reviewer can turn the
        # weaker evidence off in the viewer rather than having it filtered
        # away here. Storing everything and letting the client choose is
        # the project rule (don't destroy data downstream needs).
        tiers = [("4+ columns", 4, 99), ("3 columns", 3, 3), ("2 columns", 2, 2)]
        for label, lo_n, hi_n in tiers:
            boxes = []
            for i, a in enumerate(res["alignments"]):
                if not (lo_n <= a["n_columns"] <= hi_n):
                    continue
                x0 = _sup.pct_to_px(cols[a["col_lo"]]["left_pct"], W)
                x1 = _sup.pct_to_px(
                    cols[min(a["col_hi"], len(cols) - 1)]["right_pct"], W)
                y = _sup.pct_to_px(a["y_pct"], H)
                th = max(2, H // 400)
                boxes.append(_anno(
                    f"{cid}/anno/hl/{lo_n}/{i}", cid, x0, max(0, y - th // 2),
                    max(1, x1 - x0), th, "",
                    f"y {a['y_pct']}%", kind="horizontal alignment",
                    detail=f"y {a['y_pct']}% · columns {a['col_lo']}-{a['col_hi']} "
                           f"· {a['n_columns']} agreeing · {a['n_edges']} edges "
                           f"· {a['kinds']}"))
            if boxes:
                out.append((f"Horizontal alignments — {label} ({len(boxes)})", boxes))

    if variant == "content":
        # Stage 1c's content rectangle, drawn whole. Every later stage is
        # measured from it, so it is the first thing to check when a page
        # looks displaced. BOTH derivations are drawn, as separate layers,
        # because the only way to choose between them is to look at them on
        # the page -- see scaled_pipeline.md §5z.
        box = _detect_content_area.content_box(conn, page_id)
        if box.get("left") is not None and box.get("top") is not None:
            x0, x1 = _sup.pct_to_px(box["left"], W), _sup.pct_to_px(box["right"], W)
            y0, y1 = _sup.pct_to_px(box["top"], H), _sup.pct_to_px(box["bottom"], H)
            out.append(("Content area — from text LINES (current)", [_anno(
                f"{cid}/anno/content/box", cid, x0, y0,
                max(1, x1 - x0), max(1, y1 - y0), "", "content area",
                kind="content area",
                detail=f"{box['left']}%-{box['right']}% x "
                       f"{box['top']}%-{box['bottom']}% "
                       f"(w {box['width']}% h {box['height']}%) "
                       f"from {box['n_lines']} text lines")]))

        blk = _detect_content_area.content_box_blocks(conn, page_id)
        if blk.get("left") is not None and blk.get("top") is not None:
            x0, x1 = _sup.pct_to_px(blk["left"], W), _sup.pct_to_px(blk["right"], W)
            y0, y1 = _sup.pct_to_px(blk["top"], H), _sup.pct_to_px(blk["bottom"], H)
            ag = blk.get("agree") or {}
            sanity = blk.get("sanity") or []
            out.append(("Content area — from AGREEMENT (proposed)", [_anno(
                f"{cid}/anno/contentblk/box", cid, x0, y0,
                max(1, x1 - x0), max(1, y1 - y0), "", "content area (agreement)",
                kind="content area (agreement)",
                detail=f"{blk['left']}%-{blk['right']}% x "
                       f"{blk['top']}%-{blk['bottom']}% "
                       f"(w {blk['width']}% h {blk['height']}%) · "
                       "items agreeing on each edge: "
                       + ", ".join(f"{k} {ag.get(k, 0)}"
                                   for k in ("left", "right", "top", "bottom"))
                       + f" · of {blk['n_items']} items (all types, shadows "
                         f"removed), {blk['n_outside']} fall outside"
                       + (f" · SENSE CHECK: {', '.join(sanity)} implausible"
                          if sanity else ""))]))
    return out


def build_manifest(conn, date: str, base: str, variant: str = "all") -> dict:
    want_blocks, want_lines = VARIANTS.get(variant, (False, False))
    y, m, d = (int(v) for v in date.split("-"))
    pages = [dict(r) for r in conn.execute(
        "SELECT id, page, display_image_path, display_width_px, display_height_px "
        "FROM pages WHERE year=? AND month=? AND day=? ORDER BY page", (y, m, d))]

    canvases = []
    for p in pages:
        if not p["display_image_path"] or not os.path.isfile(p["display_image_path"]):
            continue
        W, H = p["display_width_px"], p["display_height_px"]
        if not W or not H:
            continue
        cid = f"{base}/canvas/p{p['page']}"
        img_url = _rel_url(p["display_image_path"])

        canvas = {
            "id": cid,
            "type": "Canvas",
            "label": {"en": [f"Page {p['page']}"]},
            "width": W, "height": H,
            "items": [{
                "id": f"{cid}/painting",
                "type": "AnnotationPage",
                "items": [{
                    "id": f"{cid}/painting/1",
                    "type": "Annotation",
                    "motivation": "painting",
                    "body": {"id": img_url, "type": "Image",
                             "format": "image/png", "width": W, "height": H},
                    "target": cid,
                }],
            }],
            "annotations": [],
        }

        # The overlay variant paints a SECOND image onto the same canvas.
        # IIIF Presentation 3 has no opacity property, and Mirador's own
        # roadmap lists per-layer transparency as deferred, so the alpha is
        # baked into the PNG instead of relied on from the viewer: white is
        # fully transparent there and the marks are graded. Multiple
        # painting bodies on one canvas is the standard mechanism -- Mirador
        # surfaces them through its CanvasLayers panel, so they can also be
        # reordered and toggled.
        #
        # ORDER MATTERS AND IT IS COUNTER-INTUITIVE: position 0 is the TOP
        # layer, not the bottom. Verified in the Mirador 3.4.2 bundle --
        #     layerIndexOfImageResource: return t.total - t.index - 1
        #     moveToTop(id): moves that id to position 0
        # so a low list index becomes a HIGH OpenSeadragon index, which
        # draws in front. Appending the overlay put it at index 1, i.e.
        # BEHIND the opaque page image, where it could not be seen at all.
        # It is therefore INSERTED AT 0.
        if variant == "overlay":
            ov = os.path.join(
                _sup.REPO_ROOT, "preview", "scaled", "grids", date,
                f"p{p['page']}_overlay.png")
            if os.path.isfile(ov):
                canvas["items"][0]["items"].insert(0, {
                    "id": f"{cid}/painting/overlay",
                    "type": "Annotation",
                    "motivation": "painting",
                    "body": {"id": _rel_url(ov), "type": "Image",
                             "format": "image/png", "width": W, "height": H,
                             "label": {"en": ["Separator grid overlay"]}},
                    "target": cid,
                })

        # --- block layers, straight from page_ocr_blocks / regions ---
        for cls, label, _colour in (BLOCK_LAYERS if want_blocks else []):
            items = []
            if cls == "ocr_carea":
                rows = conn.execute(
                    "SELECT block_idx, bbox_left_pct l, bbox_top_pct t, "
                    "bbox_right_pct r, bbox_bottom_pct b, raw_text, conf, n_words "
                    "FROM page_ocr_blocks WHERE page_id=? ORDER BY block_idx",
                    (p["id"],)).fetchall()
                for i, rr in enumerate(rows):
                    x0, y0 = _sup.pct_to_px(rr["l"], W), _sup.pct_to_px(rr["t"], H)
                    x1, y1 = _sup.pct_to_px(rr["r"], W), _sup.pct_to_px(rr["b"], H)
                    txt = (rr["raw_text"] or "").strip() or "(no text)"
                    items.append(_anno(
                        f"{cid}/anno/{cls}/{i}", cid, x0, y0, max(1, x1 - x0),
                        max(1, y1 - y0), txt,
                        f"block {rr['block_idx']} · conf {rr['conf']}",
                        granularity="block", kind="ocr_carea",
                        detail=f"block {rr['block_idx']} · conf {rr['conf']} · "
                               f"{rr['n_words'] or 0} words"))
            else:
                rows = conn.execute(
                    "SELECT left_pct l, top_pct t, right_pct r, bottom_pct b, "
                    "orientation, width_px, height_px FROM page_hocr_regions "
                    "WHERE page_id=? AND region_class=?", (p["id"], cls)).fetchall()
                for i, rr in enumerate(rows):
                    x0, y0 = _sup.pct_to_px(rr["l"], W), _sup.pct_to_px(rr["t"], H)
                    x1, y1 = _sup.pct_to_px(rr["r"], W), _sup.pct_to_px(rr["b"], H)
                    items.append(_anno(
                        f"{cid}/anno/{cls}/{i}", cid, x0, y0, max(1, x1 - x0),
                        max(1, y1 - y0),
                        "(no text — region only)",
                        f"{cls} {rr['orientation']}", kind=cls,
                        detail=f"{rr['orientation']} · "
                               f"{rr['width_px']}x{rr['height_px']}px"))
            if items:
                canvas["annotations"].append({
                    "id": f"{cid}/annopage/{cls}",
                    "type": "AnnotationPage",
                    "label": {"en": [f"{label} — {len(items)}"]},
                    "items": items,
                })

        # --- line layers, with the x_size Tesseract reported ---
        for cls, label, _colour in (LINE_LAYERS if want_lines else []):
            rows = conn.execute(
                "SELECT left_pct l, top_pct t, right_pct r, bottom_pct b, "
                "x_size, text FROM page_hocr_lines WHERE page_id=? AND line_class=? "
                "ORDER BY top_pct", (p["id"], cls)).fetchall()
            if not rows:
                continue
            items = []
            for i, rr in enumerate(rows):
                x0, y0 = _sup.pct_to_px(rr["l"], W), _sup.pct_to_px(rr["t"], H)
                x1, y1 = _sup.pct_to_px(rr["r"], W), _sup.pct_to_px(rr["b"], H)
                xs = f"x_size {rr['x_size']:.1f}" if rr["x_size"] else "x_size n/a"
                items.append(_anno(
                    f"{cid}/anno/{cls}/{i}", cid, x0, y0, max(1, x1 - x0),
                    max(1, y1 - y0), (rr["text"] or "").strip() or "(no text)",
                    f"{cls} · {xs}", granularity="line", kind=cls, detail=xs))
            canvas["annotations"].append({
                "id": f"{cid}/annopage/{cls}",
                "type": "AnnotationPage",
                "label": {"en": [f"{label} — {len(items)}"]},
                "items": items,
            })

        # The id came from the label's FIRST WORD, which is not unique: the
        # hlines variant emits three tiers all labelled "Horizontal
        # alignments -- N columns", so 89 of 90 canvases carried three
        # AnnotationPages sharing the id `.../annopage/horizontal`. IIIF
        # requires ids to be unique. Slug the whole label, and count off
        # any remaining collision within the canvas.
        used: dict[str, int] = {}
        for label, items in _derived_layers(conn, p["id"], cid, W, H, variant):
            slug = _slug(label)
            used[slug] = used.get(slug, 0) + 1
            if used[slug] > 1:
                slug = f"{slug}-{used[slug]}"
            canvas["annotations"].append({
                "id": f"{cid}/annopage/{slug}",
                "type": "AnnotationPage",
                "label": {"en": [label]},
                "items": items,
            })

        canvases.append(canvas)

    return {
        "@context": [
            "http://iiif.io/api/presentation/3/context.json",
            # IIIF Text Granularity extension -- lets a client tell a
            # block-level transcription from a line-level one.
            "http://iiif.io/api/extension/text-granularity/context.json",
        ],
        "id": f"{base}/{_manifest_name(variant)}",
        "type": "Manifest",
        "label": {"en": [f"Almonte Gazette {date} — " + {
            "all": "raw Tesseract hOCR (blocks + lines)",
            "blocks": "raw Tesseract hOCR (blocks)",
            "lines": "raw Tesseract hOCR (lines)",
            "grid": "stage 2: columns",
            "content": "stage 1c: the page content area",
            "hlines": "stage 3: horizontal alignments",
            "boxes": "stage 2b: boxed zones",
            "separators": "stage 1: raw ocr_separator rules",
            "photos": "stage 1: raw ocr_photo regions",
            "captions": "stage 2c: photos with captions",
            "boxphotos": "stage 2b+2c: boxed zones and photos with captions",
            "overlay": "separator grid painted over the page",
        }.get(variant, variant)]},
        "summary": {"en": [
            "Unmodified Tesseract hOCR rendered as IIIF annotation layers, "
            "including the ocr_separator and ocr_photo blocks the production "
            "parser discards. Nothing here is derived or corrected."
            if variant in VARIANTS else
            "Derived layout from transcribe/scaled -- computed from hOCR "
            "geometry alone, no pixels and no LLM. Compare against the raw "
            "Tesseract manifests for the same issue."]},
        "requiredStatement": {
            "label": {"en": ["Source"]},
            "value": {"en": [
                "Mississippi Valley Textile Museum — Almonte Gazette. "
                "OCR: Tesseract 5.5.3, tessdata_best, Sauvola thresholding."]},
        },
        "items": canvases,
    }


def _cmd(args):
    conn = _sup.open_connection()
    try:
        out_root = os.path.join(_sup.REPO_ROOT, OUT_REL)
        os.makedirs(out_root, exist_ok=True)
        built = []
        for date in args.dates:
            base = f"{PUBLIC_BASE}/{OUT_REL.replace(os.sep, '/')}/{date}"
            d = os.path.join(out_root, date)
            os.makedirs(d, exist_ok=True)
            wrote_any = False
            for variant in list(VARIANTS) + list(DERIVED):
                man = build_manifest(conn, date, base, variant)
                if not man["items"]:
                    continue
                path = os.path.join(d, _manifest_name(variant))
                with open(path, "w") as f:
                    json.dump(man, f, indent=1)
                n_annos = sum(len(ap["items"]) for c in man["items"]
                              for ap in c["annotations"])
                print(f"  {date} [{variant:6s}]: {len(man['items'])} canvases, "
                      f"{n_annos} annotations -> {os.path.basename(path)}")
                wrote_any = True
            if not wrote_any:
                print(f"  {date}: no canvases (no display images on disk) -- skipped")
                continue
            built.append(date)
        if built:
            print("\nOpen:")
            for date in built:
                print(f"  {PUBLIC_BASE}/{OUT_REL.replace(os.sep, '/')}/viewer.html?issue={date}")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dates", nargs="+", help="YYYY-MM-DD ...")
    p.set_defaults(func=_cmd)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
