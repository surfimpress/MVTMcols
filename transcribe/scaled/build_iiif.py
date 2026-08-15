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
DERIVED = ("grid1", "grid2", "grid")


def _derived_layers(conn, page_id, cid, W, H, variant):
    """Annotation pages for a derived stage. Returns [] when the stage has
    produced nothing for this page, so an empty layer never masquerades as
    a real result."""
    out = []
    if variant in ("grid", "grid1", "grid2"):
        # Both passes as separate layers, so the refinement can be judged
        # against what it started from rather than taken on trust.
        res = _detect_grid.detect(conn, page_id)
        g = res.get("grid")
        if not g:
            return out

        pass1 = []
        for k in range(g["n_columns"]):
            l = g["offset"] + k * g["pitch"]
            r = l + g["col_width"]
            x0, x1 = _sup.pct_to_px(l, W), _sup.pct_to_px(r, W)
            pass1.append(_anno(
                f"{cid}/anno/grid1/{k}", cid, x0, 0, max(1, x1 - x0), H,
                "", f"slot {k}", kind="columns (1) rigid lattice",
                detail=f"slot {k} · {l:.2f}%-{r:.2f}% · pitch {g['pitch']}% · "
                       f"col {g['col_width']}% · gutter {g['gutter']}%"))
        if variant in ("grid", "grid1"):
            out.append((f"Columns (1) — rigid lattice, "
                        f"{g['n_columns']} slots @ pitch {g['pitch']}%", pass1))

        pass2 = []
        cols = res.get("columns", [])
        for i, c in enumerate(cols):
            x0 = _sup.pct_to_px(c["left_pct"], W)
            x1 = _sup.pct_to_px(c["right_pct"], W)
            gut = (cols[i + 1]["left_pct"] - c["right_pct"]) if i + 1 < len(cols) else None
            pass2.append(_anno(
                f"{cid}/anno/grid2/{c['col_idx']}", cid, x0, 0, max(1, x1 - x0), H,
                "", f"column {c['col_idx']}", kind="columns (2) leaned to extremes",
                detail=f"col {c['col_idx']} · {c['left_pct']:.2f}%-{c['right_pct']:.2f}% "
                       f"(w {c['right_pct'] - c['left_pct']:.2f}%)"
                       + (f" · gutter {gut:+.2f}%" if gut is not None else " · right margin")))
        if pass2 and variant in ("grid", "grid2"):
            out.append((f"Columns (2) — leaned to extremes, "
                        f"{len(pass2)} columns", pass2))
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

        for label, items in _derived_layers(conn, p["id"], cid, W, H, variant):
            canvas["annotations"].append({
                "id": f"{cid}/annopage/{label.split(' ')[0].lower()}",
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
            "grid": "stage 2: columns (1)+(2)",
            "grid1": "stage 2: columns (1) — rigid lattice",
            "grid2": "stage 2: columns (2) — leaned to extremes",
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
