"""OCR+LLM route: whole-page Tesseract OCR + LLM correction/segmentation.

An alternate to the column_transcripts/items pass-1/pass-2 pipeline,
for issues (1980s+) where column detection doesn't work on the
modular layout (see instructions/layout_observations.md). Validated
on 2001-01-03 pages 1-3 this session; see
transcribe/comparison_tesseract_2001-01-03.html for the writeup and
transcribe/backfill_2001_ocr_llm.py for how that test data was
captured (a historical record, not this module -- this module is the
first pass at making the same steps repeatable).

Same three-layer split as the column-cut pipeline
(transcribe/CLAUDE.md): this module does the deterministic parts
(render, OCR, hOCR parsing, DB writes, prompt/ticket construction)
and never calls an LLM itself. The orchestrating Claude Code session
dispatches the two LLM passes (text-only cleanup, then item markup)
via the Agent tool and feeds the JSON result back into
ingest_cleanup_result / ingest_items_result.

Usage::

    python3 -m transcribe.ocr_llm render 2001-01-03 --page 4
    # -> prints page_id + the two ticket paths (cleanup, items)
    # dispatch the two LLM calls per the tickets' prompts, save each
    # result as JSON, then:
    python3 -m transcribe.ocr_llm ingest-cleanup <page_id> <result.json>
    python3 -m transcribe.ocr_llm ingest-items <page_id> <result.json>
"""

from __future__ import annotations

import argparse
import concurrent.futures as _futures
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

import fitz
from PIL import Image

import coordinates as _coords
from pdf_utils import get_full_pixmap, try_embedded_bitmap_pil

from . import db as _db
from . import download as _dl
from . import routing as _routing
from . import workflow_usage as _wf_usage

WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "ocr_llm")
TESSDATA_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "tessdata_best")
TESSDATA_URL = (
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata"
)
OCR_ENGINE = "tesseract 5.5.3"
RENDER_DPI = 300

# A page's LLM stages (cleanup+items) failing this many times across
# SEPARATE render-issue/dispatch cycles auto-flips it to 'damaged' --
# render-issue then skips it by default (see pages.llm_status,
# schema.sql v14). Deliberately small: this isn't punishing a single
# in-run retry (the workflow already retries once itself, see
# ocr_llm_issue.js) -- it's the backstop against repeatedly re-
# dispatching a page that keeps failing across whole runs.
DAMAGED_THRESHOLD = 2
DISPLAY_MAX_W = 1400
CONF_TRIAGE_THRESHOLD = 85

HOCR_NS = {"x": "http://www.w3.org/1999/xhtml"}
_BBOX_RE = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_WCONF_RE = re.compile(r"x_wconf (\d+)")

# LLM item type -> items.item_type. item_type is free TEXT (no CHECK
# constraint). Synced 2026-08-09 to the same 11-value taxonomy as the
# pre-1980 route's items-classifier.md -- ocr-items.md now asks for
# these values, not the old 'photo'/'ad'/'promo' set (see CLAUDE.md
# for the unification writeup). Anything outside this set silently
# becomes 'other' at ingest -- see ingest_items_data() below.
ITEM_TYPE_PASSTHROUGH = {
    "article", "display_ad", "classified_ad", "notice", "masthead",
    "cartoon", "letter", "announcement", "table", "index", "other",
}


# --------------------------------------------------------------------
# Trained data
# --------------------------------------------------------------------

def ensure_tessdata_best() -> str:
    """Download tessdata_best's eng.traineddata once, cache under
    transcribe/work/tessdata_best/. Returns the directory to pass as
    --tessdata-dir. Idempotent: skips the fetch if already cached."""
    dest = os.path.join(TESSDATA_DIR, "eng.traineddata")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return TESSDATA_DIR
    os.makedirs(TESSDATA_DIR, exist_ok=True)
    req = urllib.request.Request(
        TESSDATA_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("Empty response downloading tessdata_best")
    with open(dest, "wb") as f:
        f.write(data)
    return TESSDATA_DIR


# --------------------------------------------------------------------
# Render + OCR
# --------------------------------------------------------------------

def resolve_pdf_path(conn, year: int, month: int, day: int, page: int) -> str:
    """Look up the page's source PDF in mvtm.db and download it via
    the existing Drive-download cache. `conn` must be opened with
    attach_mvtm=True."""
    row = conn.execute(
        "SELECT drive_id, directory_path FROM mvtm.files "
        "WHERE year=? AND month=? AND day=? AND page=? AND file_type='pdf'",
        (year, month, day, page),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No pdf file row for {year}-{month:02d}-{day:02d} page {page}")
    filename = os.path.basename(row["directory_path"])
    date_str = f"{year:04d}-{month:02d}-{day:02d}"
    dest = _dl.local_cache_path(_db.REPO_ROOT, date_str, page, filename)
    _dl.download_column(row["drive_id"], dest)
    return dest


def enumerate_issue_pages(conn, year: int, month: int, day: int) -> list[int]:
    """All page numbers this issue has a source PDF for, per mvtm.db.
    `conn` must be opened with attach_mvtm=True."""
    rows = conn.execute(
        "SELECT DISTINCT page FROM mvtm.files "
        "WHERE year=? AND month=? AND day=? AND file_type='pdf' ORDER BY page",
        (year, month, day),
    ).fetchall()
    return [r["page"] for r in rows]


def _page_work_dir(date_str: str, page: int) -> str:
    out_dir = os.path.join(WORK_DIR, date_str, f"p{page}")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _issue_work_dir(date_str: str) -> str:
    out_dir = os.path.join(WORK_DIR, date_str)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _extract_native_page_image(pdf_path: str, page_number: int = 0):
    """Extract the page's single embedded image byte-for-byte (decode
    only, no MuPDF re-render/resample), or return (None, None) if the
    page doesn't have exactly one embedded image.

    Complements pdf_utils.try_embedded_bitmap_pil, which is gated to
    bpc==1 bilevel scans (the pre-1980s corpus's JBIG2 masters) --
    this handles the later-era grayscale/color JPEG/PNG scans that
    fast path doesn't cover. Confirmed empirically 2026-08-09: a fixed
    300dpi MuPDF render would have upsampled a 1994 test page (native
    200dpi DeviceGray JPEG, no real detail gained) and downsampled a
    1986 test page (native ~521dpi ICCBased PNG, real detail lost) --
    exactly the "re-rendered at multiple DPIs, throwing away the
    source's native resolution" anti-pattern this project's cutting
    pipeline already spent real effort eliminating (see instructions/
    rasterisation_pipeline.md).

    Returns (PIL.Image, native_dpi) -- native_dpi is a round number
    computed from the image's own pixel dimensions against the page's
    point size, for accurate record-keeping (not a chosen target)."""
    doc = fitz.open(pdf_path)
    try:
        page_obj = doc[page_number]
        imgs = page_obj.get_images(full=True)
        if len(imgs) != 1:
            return None, None
        xref = imgs[0][0]
        info = doc.extract_image(xref)
        img = Image.open(io.BytesIO(info["image"]))
        img.load()
        bbox = page_obj.get_image_bbox(imgs[0])
        if bbox.width <= 0 or bbox.height <= 0:
            return None, None
        dpi_w = img.width / (bbox.width / 72.0)
        dpi_h = img.height / (bbox.height / 72.0)
        return img, round((dpi_w + dpi_h) / 2)
    finally:
        doc.close()


def render_page(pdf_path: str, date_str: str, page: int) -> dict:
    """Full-res OCR raster (native embedded image where the page has
    exactly one -- see _extract_native_page_image -- else a
    RENDER_DPI MuPDF render as a fallback for composite/vector pages)
    plus a downscaled display copy (what the item-markup LLM pass
    sees). Returns paths + pixel dimensions for both rasters + which
    path was used."""
    out_dir = _page_work_dir(date_str, page)

    native_img, native_dpi = _extract_native_page_image(pdf_path, 0)
    if native_img is not None:
        img = native_img
        actual_dpi = native_dpi
        source = "native"
    else:
        bilevel_img = try_embedded_bitmap_pil(pdf_path, 0)
        if bilevel_img is not None:
            img = bilevel_img
            actual_dpi = None  # page-shaped canvas at the bitmap's own native PPI; not tracked here
            source = "bilevel_fast_path"
        else:
            pix = get_full_pixmap(pdf_path, 0, RENDER_DPI)
            img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)
            actual_dpi = RENDER_DPI
            source = f"rendered@{RENDER_DPI}dpi"

    full_png = os.path.join(out_dir, "page_full.png")
    img.save(full_png)

    scale = DISPLAY_MAX_W / img.width
    display_h = round(img.height * scale)
    display_img = img.convert("RGB").resize((DISPLAY_MAX_W, display_h), Image.LANCZOS)
    display_png = os.path.join(out_dir, "page_display.png")
    display_img.save(display_png)

    return {
        "out_dir": out_dir,
        "full_png": full_png,
        "display_png": display_png,
        "page_px_w": img.width,
        "page_px_h": img.height,
        "display_w": DISPLAY_MAX_W,
        "display_h": display_h,
        "render_dpi": actual_dpi,
        "render_source": source,
    }


_HOCR_CONFIG_CANDIDATES = [
    "/opt/homebrew/share/tessdata/configs/hocr",
    "/opt/homebrew/Cellar/tesseract/5.5.3/share/tessdata/configs/hocr",
    "/usr/local/share/tessdata/configs/hocr",
]


def _find_hocr_config() -> str:
    """--tessdata-dir overrides Tesseract's entire search path,
    including where it looks for the built-in 'hocr' output-format
    config -- that file only exists under the standard tessdata
    install (not our custom tessdata_best cache, which holds only
    eng.traineddata). Reference it by absolute path instead of the
    bare config name so it resolves regardless of --tessdata-dir."""
    for path in _HOCR_CONFIG_CANDIDATES:
        if os.path.isfile(path):
            return path
    found = subprocess.run(
        ["find", "/opt/homebrew", "-iname", "hocr", "-path", "*configs*"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if found:
        return found[0]
    raise RuntimeError(
        "Could not locate Tesseract's built-in hocr config file under "
        "any known Homebrew path.")


def run_tesseract_hocr(image_path: str, output_base: str,
                        tessdata_dir: str, dpi: int | None = None) -> str:
    """Run Tesseract with Sauvola local-adaptive thresholding (fixes
    grey-sidebar/fold-shadow blackout on greyscale scans -- see
    comparison_tesseract_2001-01-03.html) and tessdata_best. `dpi`
    should be the image's real resolution (Tesseract uses it as a hint
    for stroke-width assumptions) -- falls back to RENDER_DPI only
    when the real figure isn't known (the bilevel fast path's
    page-shaped canvas doesn't track one). Returns the .hocr path."""
    subprocess.run(
        [
            "tesseract", image_path, output_base,
            "--dpi", str(dpi or RENDER_DPI),
            "--psm", "3",
            "--oem", "1",
            "-c", "thresholding_method=2",
            "--tessdata-dir", tessdata_dir,
            _find_hocr_config(),
        ],
        check=True, capture_output=True,
    )
    return output_base + ".hocr"


def _parse_bbox(title: str | None) -> list[int]:
    m = _BBOX_RE.search(title or "")
    return [int(x) for x in m.groups()] if m else [0, 0, 0, 0]


def _parse_wconf(title: str | None) -> int:
    m = _WCONF_RE.search(title or "")
    return int(m.group(1)) if m else 0


def parse_hocr(hocr_path: str) -> dict:
    """Block granularity = ocr_carea (Tesseract's own top-level layout
    block) -- confirmed empirically against the 2001-01-03 test data
    (114/68/150 blocks per page, exact match). A block's confidence is
    the mean of its child words' x_wconf; ocr_carea itself carries no
    confidence of its own."""
    tree = ET.parse(hocr_path)
    root = tree.getroot()
    careas = root.findall(".//x:div[@class='ocr_carea']", HOCR_NS)

    blocks, words = [], []
    for carea in careas:
        bbox = _parse_bbox(carea.get("title"))
        word_elems = carea.findall(".//x:span[@class='ocrx_word']", HOCR_NS)
        block_words = []
        for w in word_elems:
            wbbox = _parse_bbox(w.get("title"))
            wconf = _parse_wconf(w.get("title"))
            text = (w.text or "").strip()
            entry = {"bbox": wbbox, "conf": wconf, "text": text}
            block_words.append(entry)
            words.append(entry)
        block_text = " ".join(w["text"] for w in block_words if w["text"])
        avg_conf = (
            round(sum(w["conf"] for w in block_words) / len(block_words), 1)
            if block_words else 0.0
        )
        blocks.append({
            "bbox": bbox, "text": block_text,
            "avg_conf": avg_conf, "n_words": len(block_words),
        })
    return {"blocks": blocks, "words": words}


# --------------------------------------------------------------------
# DB writes -- deterministic (OCR) side
# --------------------------------------------------------------------

def write_page_and_blocks(conn, year: int, month: int, day: int, page: int,
                           pdf_path: str, render: dict, hocr_path: str,
                           parsed: dict, layout_class: str | None = None
                           ) -> tuple[str, dict]:
    """Insert the pages row and one page_ocr_blocks row per Tesseract
    block. Returns (page_id, {block_idx: page_ocr_blocks.id}).

    layout_class defaults to routing.layout_class_for_date(year) --
    pass it explicitly only to override the corpus-wide default for a
    specific issue (see routing.py's docstring)."""
    if layout_class is None:
        layout_class = _routing.layout_class_for_date(year)
    blocks, words = parsed["blocks"], parsed["words"]
    confs = [w["conf"] for w in words] if words else [b["avg_conf"] for b in blocks]
    hocr_mean_conf = round(sum(confs) / len(confs), 2) if confs else None

    page_id = _db.new_uuid()
    now = _db.now_iso()
    conn.execute(
        """INSERT INTO pages (
            id, year, month, day, page, pdf_path, page_raw_path,
            render_dpi, ocr_engine, ocr_trained_data, thresholding_method,
            hocr_path, hocr_word_count, hocr_mean_confidence, layout_class,
            display_image_path, display_width_px, display_height_px,
            created_at, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            page_id, year, month, day, page, pdf_path, render["full_png"],
            render["render_dpi"], OCR_ENGINE, "tessdata_best", "sauvola",
            hocr_path, len(words), hocr_mean_conf, layout_class,
            render["display_png"], render["display_w"], render["display_h"],
            now, f"render_source={render['render_source']}",
        ),
    )

    block_id_by_idx = {}
    page_px_w, page_px_h = render["page_px_w"], render["page_px_h"]
    for idx, b in enumerate(blocks):
        x0, y0, x1, y1 = b["bbox"]
        block_id = _db.new_uuid()
        block_id_by_idx[idx] = block_id
        conn.execute(
            """INSERT INTO page_ocr_blocks (
                id, page_id, block_idx, bbox_left_pct, bbox_top_pct,
                bbox_right_pct, bbox_bottom_pct, conf, n_words,
                raw_text, triaged, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                block_id, page_id, idx,
                _coords.px_to_pct(x0, page_px_w), _coords.px_to_pct(y0, page_px_h),
                _coords.px_to_pct(x1, page_px_w), _coords.px_to_pct(y1, page_px_h),
                b["avg_conf"], b["n_words"], b["text"], 0, now,
            ),
        )
    conn.commit()
    return page_id, block_id_by_idx


# --------------------------------------------------------------------
# LLM tickets -- prompt + input JSON, no LLM call happens here
# --------------------------------------------------------------------

# Durable task rules live in .claude/agents/ocr-cleanup.md and
# ocr-items.md (dispatched via subagent_type, not the generic Agent
# tool -- matches column-transcriber's established split, measured to
# cut ~35% of per-call tokens by not re-sending static instructions
# and by capping the deferred-tools listing to what Read-only needs).
# These per-call prompts carry only what actually varies call to call.

CLEANUP_PROMPT_TEMPLATE = """Almonte Gazette, {date} page {page}. Low-confidence Tesseract blocks are at {blocks_path}."""

ITEMS_PROMPT_TEMPLATE = """Almonte Gazette, {date} page {page}. Blocks: {blocks_path} (page image space, {display_w}x{display_h} px). Page image: {display_png}."""


def build_cleanup_ticket(conn, page_id: str,
                          threshold: int = CONF_TRIAGE_THRESHOLD) -> str:
    page_row = conn.execute(
        "SELECT year, month, day, page FROM pages WHERE id=?", (page_id,)
    ).fetchone()
    date_str = f"{page_row['year']:04d}-{page_row['month']:02d}-{page_row['day']:02d}"
    out_dir = _page_work_dir(date_str, page_row["page"])

    rows = conn.execute(
        "SELECT block_idx, conf, raw_text FROM page_ocr_blocks "
        "WHERE page_id=? AND conf<? ORDER BY block_idx",
        (page_id, threshold),
    ).fetchall()
    payload = [{"id": r["block_idx"], "conf": r["conf"], "text": r["raw_text"]}
               for r in rows]
    blocks_path = os.path.join(out_dir, "cleanup_input.json")
    with open(blocks_path, "w") as f:
        json.dump(payload, f)

    prompt = CLEANUP_PROMPT_TEMPLATE.format(
        date=date_str, page=page_row["page"], blocks_path=blocks_path)
    ticket_path = os.path.join(out_dir, "cleanup_ticket.json")
    with open(ticket_path, "w") as f:
        json.dump({
            "page_id": page_id, "kind": "cleanup", "agent_type": "ocr-cleanup",
            "blocks_path": blocks_path, "n_blocks": len(payload),
            "prompt": prompt,
        }, f, indent=2)
    return ticket_path


def build_items_ticket(conn, page_id: str) -> str:
    page_row = conn.execute(
        "SELECT year, month, day, page, display_image_path, "
        "display_width_px, display_height_px FROM pages WHERE id=?",
        (page_id,),
    ).fetchone()
    date_str = f"{page_row['year']:04d}-{page_row['month']:02d}-{page_row['day']:02d}"
    out_dir = _page_work_dir(date_str, page_row["page"])

    rows = conn.execute(
        "SELECT block_idx, bbox_left_pct, bbox_top_pct, bbox_right_pct, "
        "bbox_bottom_pct, conf, raw_text FROM page_ocr_blocks "
        "WHERE page_id=? ORDER BY block_idx", (page_id,)
    ).fetchall()

    disp_w, disp_h = page_row["display_width_px"], page_row["display_height_px"]
    payload = []
    for r in rows:
        x0 = _coords.pct_to_px(r["bbox_left_pct"], disp_w)
        y0 = _coords.pct_to_px(r["bbox_top_pct"], disp_h)
        x1 = _coords.pct_to_px(r["bbox_right_pct"], disp_w)
        y1 = _coords.pct_to_px(r["bbox_bottom_pct"], disp_h)
        payload.append({
            "id": r["block_idx"], "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
            "conf": r["conf"], "text": (r["raw_text"] or "")[:90],
        })
    blocks_path = os.path.join(out_dir, "items_input.json")
    with open(blocks_path, "w") as f:
        json.dump(payload, f)

    prompt = ITEMS_PROMPT_TEMPLATE.format(
        date=date_str, page=page_row["page"],
        display_png=page_row["display_image_path"],
        display_w=disp_w, display_h=disp_h, blocks_path=blocks_path,
    )
    ticket_path = os.path.join(out_dir, "items_ticket.json")
    with open(ticket_path, "w") as f:
        json.dump({
            "page_id": page_id, "kind": "items", "agent_type": "ocr-items",
            "blocks_path": blocks_path, "display_png": page_row["display_image_path"],
            "n_blocks": len(payload), "prompt": prompt,
        }, f, indent=2)
    return ticket_path


# --------------------------------------------------------------------
# Ingest -- LLM results back into the DB
# --------------------------------------------------------------------

def ingest_cleanup_data(conn, page_id: str, results: list[dict],
                         model: str = "sonnet") -> int:
    """Core ingest, takes an already-parsed result list -- used both by
    the file-path CLI path below and directly by Workflow-orchestrated
    runs (whose agent() schema option returns parsed JSON, not a file
    on disk)."""
    n = 0
    for r in results:
        conn.execute(
            "UPDATE page_ocr_blocks SET cleaned_text=?, cleanup_status=?, "
            "triaged=1, model=? WHERE page_id=? AND block_idx=?",
            (r["cleaned"], r["status"], model, page_id, r["id"]),
        )
        n += 1
    conn.commit()
    return n


def ingest_cleanup_result(conn, page_id: str, result_path: str,
                           model: str = "sonnet") -> int:
    with open(result_path) as f:
        results = json.load(f)
    return ingest_cleanup_data(conn, page_id, results, model)


def _block_line(cleaned_by_idx: dict, raw_by_idx: dict, bid: int) -> str | None:
    c = cleaned_by_idx.get(bid)
    if c is None:
        return raw_by_idx.get(bid)
    if c["status"] == "noise":
        return None
    return c["cleaned"]


def ingest_items_data(conn, page_id: str, items: list[dict],
                       model: str = "sonnet") -> int:
    """Core ingest, takes an already-parsed items list -- see
    ingest_cleanup_data's docstring for why this split exists."""
    page_row = conn.execute(
        "SELECT year, month, day, page, display_width_px, display_height_px "
        "FROM pages WHERE id=?", (page_id,),
    ).fetchone()
    disp_w, disp_h = page_row["display_width_px"], page_row["display_height_px"]

    block_rows = conn.execute(
        "SELECT id, block_idx, raw_text, cleaned_text, cleanup_status "
        "FROM page_ocr_blocks WHERE page_id=?", (page_id,),
    ).fetchall()
    block_id_by_idx = {r["block_idx"]: r["id"] for r in block_rows}
    raw_by_idx = {r["block_idx"]: r["raw_text"] for r in block_rows}
    cleaned_by_idx = {
        r["block_idx"]: {"cleaned": r["cleaned_text"], "status": r["cleanup_status"]}
        for r in block_rows if r["cleanup_status"] is not None
    }

    now = _db.now_iso()
    n = 0
    for it in items:
        b = it["bbox"]
        item_type = it.get("type") if it.get("type") in ITEM_TYPE_PASSTHROUGH else "other"
        item_id = _db.new_uuid()

        body_lines = [_block_line(cleaned_by_idx, raw_by_idx, bid)
                      for bid in (it.get("block_ids") or [])]
        full_text = "\n".join(l for l in body_lines if l)
        cap_lines = [_block_line(cleaned_by_idx, raw_by_idx, bid)
                     for bid in (it.get("caption_block_ids") or [])]
        caption = "\n".join(l for l in cap_lines if l)
        if caption:
            full_text = (full_text + "\n\nCaption: " + caption).strip()

        conn.execute(
            """INSERT INTO items (
                id, item_type, year, month, day, page,
                bbox_left_pct, bbox_top_pct, bbox_right_pct, bbox_bottom_pct,
                headline, full_text, model, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item_id, item_type, page_row["year"], page_row["month"],
                page_row["day"], page_row["page"],
                _coords.px_to_pct(b["x"], disp_w), _coords.px_to_pct(b["y"], disp_h),
                _coords.px_to_pct(b["x"] + b["w"], disp_w),
                _coords.px_to_pct(b["y"] + b["h"], disp_h),
                it.get("label"), full_text, model, now,
            ),
        )
        conn.execute(
            "INSERT INTO items_ocr_ext (item_id, created_at) VALUES (?,?)",
            (item_id, now),
        )

        seq = 0
        for role, key in (("body", "block_ids"), ("caption", "caption_block_ids")):
            for bid in (it.get(key) or []):
                block_id = block_id_by_idx.get(bid)
                if block_id is None:
                    continue
                conn.execute(
                    "INSERT INTO item_ocr_block_spans "
                    "(item_id, page_ocr_block_id, role, sequence) VALUES (?,?,?,?)",
                    (item_id, block_id, role, seq),
                )
                seq += 1

        # No entity-mention ingest here -- ocr-items only segments the
        # page now. Entity extraction is a separate, later, independent
        # pass (transcribe/extract_terms.py, reads items.full_text once
        # it's committed here) that reuses the exact same
        # ingest_item_result._insert_mentions/upsert_entity path this
        # used to call inline. See schema.sql v12 (items.terms_extracted_at)
        # and CLAUDE.md for the full split.
        n += 1
    conn.commit()
    return n


def recover_orphaned_blocks(conn, page_id: str) -> int:
    """Automatic, Python-only fallback for the coverage gap that
    verify-coverage otherwise leaves for a human to find and hand-patch
    (SKILL.md's "does not auto-fix" note; confirmed real on 2001-01-03
    p9, 4/188 blocks silently unclaimed despite the items-pass prompt's
    explicit instruction). Called right after ingest_items_data(), not
    as a separate manual step, so a coverage gap can no longer slip
    through just because nobody remembered to run verify-coverage.

    Bundles every still-unclaimed block's already-persisted text
    (cleaned if triaged, raw Tesseract output otherwise -- the same
    fallback ingest_items_data's own items use, ultimately sourced
    from the page's saved .hocr file, see pages.hocr_path) into one
    honest catch-all 'other' item, flagged repair_needed so it surfaces
    like any other upstream issue this pipeline flags rather than
    blending in as a genuine LLM classification -- never fabricate a
    confident label for content no LLM pass actually classified. Zero
    LLM tokens spent: this never calls an agent, only reads what OCR
    already wrote. Idempotent -- a page with no gap is a no-op.

    Requires at least one real item to already exist for this page --
    a page with zero items means the items-pass hasn't run at all yet
    (still pending, nothing dropped), not that it ran and left a gap.
    Confirmed live on 1997-01-08: every block on every page shows as
    "unclaimed" simply because that issue's items-pass hasn't been
    dispatched yet -- without this guard, recovery would wrongly
    swallow an entire unprocessed page into one fake catch-all item."""
    has_items = conn.execute(
        "SELECT 1 FROM items WHERE year=(SELECT year FROM pages WHERE id=?) "
        "AND month=(SELECT month FROM pages WHERE id=?) "
        "AND day=(SELECT day FROM pages WHERE id=?) "
        "AND page=(SELECT page FROM pages WHERE id=?) LIMIT 1",
        (page_id, page_id, page_id, page_id),
    ).fetchone()
    if not has_items:
        return 0
    block_rows = conn.execute(
        "SELECT id, block_idx, bbox_left_pct, bbox_top_pct, bbox_right_pct, "
        "bbox_bottom_pct, raw_text, cleaned_text, cleanup_status "
        "FROM page_ocr_blocks WHERE page_id=?", (page_id,),
    ).fetchall()
    if not block_rows:
        return 0
    covered = {r[0] for r in conn.execute(
        "SELECT DISTINCT page_ocr_block_id FROM item_ocr_block_spans "
        "WHERE page_ocr_block_id IN ({})".format(",".join("?" * len(block_rows))),
        [r["id"] for r in block_rows],
    )}
    orphaned = [r for r in block_rows if r["id"] not in covered]
    if not orphaned:
        return 0

    page_row = conn.execute(
        "SELECT year, month, day, page FROM pages WHERE id=?", (page_id,),
    ).fetchone()
    cleaned_by_idx = {
        r["block_idx"]: {"cleaned": r["cleaned_text"], "status": r["cleanup_status"]}
        for r in block_rows if r["cleanup_status"] is not None
    }
    raw_by_idx = {r["block_idx"]: r["raw_text"] for r in block_rows}
    lines = [_block_line(cleaned_by_idx, raw_by_idx, r["block_idx"]) for r in orphaned]
    full_text = "\n".join(l for l in lines if l)

    now = _db.now_iso()
    item_id = _db.new_uuid()
    conn.execute(
        """INSERT INTO items (
            id, item_type, year, month, day, page,
            bbox_left_pct, bbox_top_pct, bbox_right_pct, bbox_bottom_pct,
            headline, full_text, model, created_at,
            repair_needed, repair_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id, "other", page_row["year"], page_row["month"],
            page_row["day"], page_row["page"],
            min(r["bbox_left_pct"] for r in orphaned),
            min(r["bbox_top_pct"] for r in orphaned),
            max(r["bbox_right_pct"] for r in orphaned),
            max(r["bbox_bottom_pct"] for r in orphaned),
            "[Auto-recovered: unclaimed OCR blocks]", full_text,
            "auto-recovery", now, 1,
            f"items-pass left {len(orphaned)} block(s) unclaimed "
            f"(idx {sorted(r['block_idx'] for r in orphaned)}); bundled "
            f"automatically from saved OCR text, not reviewed by any LLM pass",
        ),
    )
    conn.execute(
        "INSERT INTO items_ocr_ext (item_id, created_at) VALUES (?,?)",
        (item_id, now),
    )
    for seq, r in enumerate(orphaned):
        conn.execute(
            "INSERT INTO item_ocr_block_spans "
            "(item_id, page_ocr_block_id, role, sequence) VALUES (?,?,?,?)",
            (item_id, r["id"], "body", seq),
        )
    conn.commit()
    return len(orphaned)


def ingest_items_result(conn, page_id: str, result_path: str,
                         model: str = "sonnet") -> int:
    with open(result_path) as f:
        items = json.load(f)
    n = ingest_items_data(conn, page_id, items, model)
    recovered = recover_orphaned_blocks(conn, page_id)
    if recovered:
        print(f"recover_orphaned_blocks: {recovered} block(s) bundled into a catch-all item")
    return n


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------

def _render_one_page(conn, year: int, month: int, day: int, page: int) -> dict:
    """Render + OCR + write DB rows + build both LLM tickets for one
    page. Returns {page_id, cleanup_prompt, items_prompt}. Shared by
    the single-page `render` command and the issue-wide `render-issue`
    command below -- skips work already done (idempotent) the same
    way both callers need."""
    existing = conn.execute(
        "SELECT id FROM pages WHERE year=? AND month=? AND day=? AND page=?",
        (year, month, day, page),
    ).fetchone()
    if existing:
        page_id = existing["id"]
    else:
        pdf_path = resolve_pdf_path(conn, year, month, day, page)
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        render = render_page(pdf_path, date_str, page)
        tessdata_dir = ensure_tessdata_best()
        hocr_base = os.path.join(render["out_dir"], "page")
        hocr_path = run_tesseract_hocr(render["full_png"], hocr_base, tessdata_dir,
                                        dpi=render["render_dpi"])
        parsed = parse_hocr(hocr_path)
        page_id, _ = write_page_and_blocks(
            conn, year, month, day, page, pdf_path, render, hocr_path, parsed)

    cleanup_ticket = build_cleanup_ticket(conn, page_id)
    items_ticket = build_items_ticket(conn, page_id)
    return {
        "page_id": page_id, "page": page,
        "cleanup_prompt": json.load(open(cleanup_ticket))["prompt"],
        "items_prompt": json.load(open(items_ticket))["prompt"],
        "already_rendered": bool(existing),
    }


def _cmd_render(args):
    year, month, day = (int(x) for x in args.date.split("-"))
    conn = _db.open_connection(attach_mvtm=True)
    try:
        result = _render_one_page(conn, year, month, day, args.page)
        print(f"page_id: {result['page_id']}"
              f" ({'already rendered' if result['already_rendered'] else 'rendered'})")
    finally:
        conn.close()


def _render_one_page_own_connection(year: int, month: int, day: int, page: int) -> dict:
    """Worker-pool entry point -- opens and closes its own connection
    rather than sharing the caller's. sqlite3 connections aren't safe
    to use from a thread other than the one that created them; each
    page render gets its own, and WAL mode (see schema.sql) lets
    concurrent writers serialize safely at the DB level rather than
    corrupt anything -- a second writer just queues briefly behind
    the first's commit."""
    conn = _db.open_connection(attach_mvtm=True)
    try:
        return _render_one_page(conn, year, month, day, page)
    finally:
        conn.close()


def _cmd_render_issue(args):
    conn = _db.open_connection(attach_mvtm=True)
    try:
        year, month, day = (int(x) for x in args.date.split("-"))
        pages = enumerate_issue_pages(conn, year, month, day)
        if not pages:
            print(f"No pdf files found for {args.date} in mvtm.files")
            return
        route = _routing.route_for_date(year)
        if route != "ocr_llm" and not args.force:
            print(f"{args.date} routes to '{route}', not 'ocr_llm' "
                  f"(cutoff year {_routing.COLUMN_CUT_CUTOFF_YEAR}). "
                  f"Pass --force to render anyway.")
            return
    finally:
        conn.close()

    # Render+OCR is CPU/IO-bound and page-independent -- a worker pool
    # cuts issue-level wall-clock by roughly the pool width instead of
    # rendering strictly one page at a time. Threads are fine despite
    # the GIL: Tesseract runs as a subprocess (run_tesseract_hocr),
    # which releases it for the duration of the OCR call.
    results_by_page = {}
    with _futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_render_one_page_own_connection, year, month, day, page): page
            for page in pages
        }
        for fut in _futures.as_completed(futures):
            page = futures[fut]
            r = fut.result()
            status = "already rendered" if r["already_rendered"] else "rendered"
            print(f"  page {page}: {status}, page_id={r['page_id']}")
            results_by_page[page] = r

    results = [results_by_page[page] for page in pages]

    # A page marked 'damaged' (repeated LLM-stage failures across
    # separate runs, see DAMAGED_THRESHOLD/_record_page_failure) is
    # excluded from dispatch by default -- the point is to stop
    # agents churning against something that keeps failing, not to
    # keep re-trying it silently forever. --include-damaged overrides
    # this for a deliberate human-decided retry.
    if not args.include_damaged:
        conn = _db.open_connection()
        try:
            damaged = {
                r["id"]: (r["llm_status_notes"] or "")
                for r in conn.execute(
                    "SELECT id, llm_status_notes FROM pages WHERE id IN ({}) AND llm_status='damaged'"
                    .format(",".join("?" * len(results))),
                    [r["page_id"] for r in results],
                )
            }
        finally:
            conn.close()
        if damaged:
            skipped_pages = [r["page"] for r in results if r["page_id"] in damaged]
            print(f"\nSkipping {len(skipped_pages)} damaged page(s): {skipped_pages} "
                  f"-- pass --include-damaged to retry anyway.")
            results = [r for r in results if r["page_id"] not in damaged]

    out_dir = _issue_work_dir(args.date)
    args_path = os.path.join(out_dir, "workflow_args.json")
    with open(args_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n{len(results)} pages ready. Workflow args written to:\n{args_path}")
    print("Next: invoke Workflow with scriptPath="
          "'transcribe/workflows/ocr_llm_issue.js' and this file's "
          "contents as args, then save its result and run "
          "'ingest-workflow-result'.")


def _record_page_failure(conn, page_id: str, reason: str) -> str:
    """Increments llm_failure_count and sets llm_status -- 'damaged'
    once the count crosses DAMAGED_THRESHOLD, 'failed' otherwise (a
    single bad run shouldn't permanently block a page; repeated
    failures across separate runs should). Returns the status set, so
    the caller can report it."""
    row = conn.execute(
        "SELECT llm_failure_count FROM pages WHERE id=?", (page_id,)
    ).fetchone()
    count = (row["llm_failure_count"] or 0) + 1
    status = "damaged" if count >= DAMAGED_THRESHOLD else "failed"
    conn.execute(
        "UPDATE pages SET llm_failure_count=?, llm_status=?, llm_status_notes=? WHERE id=?",
        (count, status, reason, page_id),
    )
    conn.commit()
    return status


def _record_page_success(conn, page_id: str) -> None:
    """A page that succeeds gets a clean slate -- past failures no
    longer matter once the actual problem is resolved."""
    conn.execute(
        "UPDATE pages SET llm_status='done', llm_failure_count=0, llm_status_notes=NULL WHERE id=?",
        (page_id,),
    )
    conn.commit()


def ingest_workflow_result_data(conn, pages: list[dict], model: str = "sonnet") -> dict:
    """Ingest a whole Workflow run's result array
    ([{page_id, page, cleanup, items, failed, failure_reason}, ...]).
    Skips any page that already has items ingested (idempotent -- safe
    to re-run against a partially-ingested batch, e.g. after a crash
    mid-loop). A page marked failed (its cleanup or items agent call
    errored even after ocr_llm_issue.js's own one-shot retry) is not
    ingested at all -- there's nothing reliable to ingest -- and
    instead has its failure recorded via _record_page_failure, so a
    page that keeps failing across separate runs auto-escalates to
    'damaged' and stops being dispatched (see render-issue)."""
    ingested, skipped, failed = [], [], []
    for p in pages:
        page_id = p["page_id"]
        existing = conn.execute(
            "SELECT count(*) AS n FROM items i JOIN pages pg "
            "ON i.year=pg.year AND i.month=pg.month AND i.day=pg.day "
            "AND i.page=pg.page WHERE pg.id=?", (page_id,),
        ).fetchone()["n"]
        if existing:
            skipped.append(p["page"])
            continue
        if p.get("failed"):
            status = _record_page_failure(conn, page_id, p.get("failure_reason") or "unspecified")
            failed.append({"page": p["page"], "status": status, "reason": p.get("failure_reason")})
            continue
        n_blocks = ingest_cleanup_data(conn, page_id, p.get("cleanup") or [], model)
        n_items = ingest_items_data(conn, page_id, p.get("items") or [], model)
        n_recovered = recover_orphaned_blocks(conn, page_id)
        _record_page_success(conn, page_id)
        entry = {"page": p["page"], "blocks": n_blocks, "items": n_items}
        if n_recovered:
            entry["recovered_blocks"] = n_recovered
        ingested.append(entry)
    return {"ingested": ingested, "skipped_already_done": skipped, "failed": failed}


def ingest_run_usage(conn, year: int, month: int, day: int, run_dir: str,
                      total_tokens: int, agent_count: int, duration_ms: int,
                      ended_at: str, started_at: str | None = None,
                      notes: str | None = None) -> str:
    """Write one ocr_llm_runs row (the harness-reported aggregate,
    trusted exact) plus the per-page/per-kind breakdown recovered from
    the run's transcripts (best-effort -- see schema.sql's comment on
    why this doesn't reconcile exactly to total_tokens). `total_tokens`,
    `agent_count`, `duration_ms`, `ended_at` come from the Workflow
    completion notification's own <usage> block -- pass them through
    as reported, don't recompute them."""
    page_id_by_num = {
        r["page"]: r["id"] for r in conn.execute(
            "SELECT page, id FROM pages WHERE year=? AND month=? AND day=?",
            (year, month, day))
    }
    per_agent = _wf_usage.extract_agent_usage(run_dir)
    pages_covered = sorted({u["page"] for u in per_agent if u["page"] is not None})

    run_id = _db.new_uuid()
    now = _db.now_iso()
    conn.execute(
        """INSERT INTO ocr_llm_runs (
            id, year, month, day, pages_json, agent_count, total_tokens,
            total_tool_calls, duration_ms, started_at, ended_at, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, year, month, day, json.dumps(pages_covered), agent_count,
         total_tokens, sum(u["tool_calls"] for u in per_agent), duration_ms,
         started_at, ended_at, notes),
    )

    for u in per_agent:
        page_id = page_id_by_num.get(u["page"])
        if page_id is None:
            continue
        conn.execute(
            """INSERT INTO page_llm_calls (
                id, run_id, page_id, kind, model, tokens_in, tokens_out,
                tool_calls, duration_ms, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_db.new_uuid(), run_id, page_id, u["kind"], u["model"],
             u["tokens_in"], u["tokens_out"], u["tool_calls"],
             u["duration_ms"], now),
        )
    conn.commit()
    return run_id


def ingest_manual_call_usage(conn, page_id: str, kind: str, model: str,
                              tokens_in: int, tokens_out: int, tool_calls: int,
                              duration_ms: int) -> str:
    """Record usage for a single manual Agent-tool dispatch (not part
    of a Workflow run) -- e.g. the general-purpose-fallback dispatches
    used when a new .claude/agents/*.md type isn't in the session's
    registry yet. run_id stays NULL."""
    call_id = _db.new_uuid()
    conn.execute(
        """INSERT INTO page_llm_calls (
            id, run_id, page_id, kind, model, tokens_in, tokens_out,
            tool_calls, duration_ms, created_at
        ) VALUES (?,NULL,?,?,?,?,?,?,?,?)""",
        (call_id, page_id, kind, model, tokens_in, tokens_out,
         tool_calls, duration_ms, _db.now_iso()),
    )
    conn.commit()
    return call_id


def verify_block_coverage(conn, year: int, month: int, day: int) -> list[dict]:
    """Per page: every page_ocr_blocks row should be claimed by
    exactly one item_ocr_block_spans row. Returns only pages with a
    gap -- an item-markup pass can legitimately drop blocks (see the
    2001-01-03 p9 case, 4 blocks never assigned to any item), and this
    is how that gets caught rather than assumed away."""
    pages = conn.execute(
        "SELECT id, page FROM pages WHERE year=? AND month=? AND day=? ORDER BY page",
        (year, month, day),
    ).fetchall()
    gaps = []
    for pg in pages:
        all_blocks = {r["block_idx"]: r["id"] for r in conn.execute(
            "SELECT block_idx, id FROM page_ocr_blocks WHERE page_id=?", (pg["id"],))}
        if not all_blocks:
            continue
        covered = {r[0] for r in conn.execute(
            "SELECT DISTINCT page_ocr_block_id FROM item_ocr_block_spans "
            "WHERE page_ocr_block_id IN ({})".format(",".join("?" * len(all_blocks))),
            list(all_blocks.values()),
        )}
        missing = sorted(idx for idx, bid in all_blocks.items() if bid not in covered)
        if missing:
            gaps.append({"page": pg["page"], "page_id": pg["id"],
                         "missing_block_idx": missing, "total_blocks": len(all_blocks)})
    return gaps


def _cmd_ingest_workflow_result(args):
    with open(args.result_json) as f:
        data = json.load(f)
    # accept either the bare result array or a TaskOutput-style
    # {"result": [...]} wrapper -- both have shown up on disk this
    # session depending on how the file was saved.
    pages = data["result"] if isinstance(data, dict) and "result" in data else data
    conn = _db.open_connection()
    try:
        summary = ingest_workflow_result_data(conn, pages, args.model)
        for row in summary["ingested"]:
            line = f"  page {row['page']}: ingested {row['blocks']} blocks, {row['items']} items"
            if row.get("recovered_blocks"):
                line += f" (+{row['recovered_blocks']} recovered into a catch-all item)"
            print(line)
        if summary["skipped_already_done"]:
            print(f"  skipped (already done): pages {summary['skipped_already_done']}")
        if summary["failed"]:
            for f in summary["failed"]:
                print(f"  page {f['page']}: FAILED ({f['status']}) -- {f['reason']}")
            print(f"  {len(summary['failed'])} page(s) failed -- not ingested, nothing "
                  f"fabricated. Re-run render-issue to retry (pages marked 'damaged' "
                  f"need --include-damaged, see its help text).")

        if args.run_dir:
            if not args.date or args.total_tokens is None or args.agent_count is None or args.duration_ms is None:
                print("  --run-dir given but --date/--total-tokens/--agent-count/"
                      "--duration-ms missing -- skipping usage ingest")
            elif not pages:
                print("  no usage recorded: empty result array")
            else:
                year, month, day = (int(x) for x in args.date.split("-"))
                run_id = ingest_run_usage(
                    conn, year, month, day, args.run_dir,
                    total_tokens=args.total_tokens, agent_count=args.agent_count,
                    duration_ms=args.duration_ms, ended_at=_db.now_iso())
                print(f"  usage recorded: ocr_llm_runs.id={run_id}")
    finally:
        conn.close()


def _cmd_verify_coverage(args):
    year, month, day = (int(x) for x in args.date.split("-"))
    conn = _db.open_connection()
    try:
        gaps = verify_block_coverage(conn, year, month, day)
        if not gaps:
            print(f"{args.date}: full block coverage on every rendered page")
            return
        for g in gaps:
            print(f"  page {g['page']}: {len(g['missing_block_idx'])}/{g['total_blocks']} "
                  f"blocks unclaimed -- idx {g['missing_block_idx']}")
    finally:
        conn.close()


def _cmd_ingest_cleanup(args):
    conn = _db.open_connection()
    try:
        n = ingest_cleanup_result(conn, args.page_id, args.result_json, args.model)
        print(f"updated {n} blocks")
    finally:
        conn.close()


def _cmd_ingest_items(args):
    conn = _db.open_connection()
    try:
        n = ingest_items_result(conn, args.page_id, args.result_json, args.model)
        print(f"inserted {n} items")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_render = sub.add_parser("render", help="Render + OCR one page, write DB rows + LLM tickets")
    p_render.add_argument("date", help="YYYY-MM-DD")
    p_render.add_argument("--page", type=int, required=True)
    p_render.set_defaults(func=_cmd_render)

    p_render_issue = sub.add_parser(
        "render-issue",
        help="Render + OCR every page of an issue, write a Workflow-ready args file")
    p_render_issue.add_argument("date", help="YYYY-MM-DD")
    p_render_issue.add_argument(
        "--force", action="store_true",
        help="Render even if routing.py says this date belongs to the column-cut route")
    p_render_issue.add_argument(
        "--workers", type=int, default=4,
        help="Parallel render+OCR worker threads (default 4; pages are "
             "independent, so this is a straight wall-clock win up to core count)")
    p_render_issue.add_argument(
        "--include-damaged", action="store_true",
        help="Include pages marked 'damaged' (repeated LLM-stage failures) "
             "in the dispatch args instead of skipping them -- use after "
             "deciding by hand that a retry is worth it")
    p_render_issue.set_defaults(func=_cmd_render_issue)

    p_ingest_wf = sub.add_parser(
        "ingest-workflow-result",
        help="Ingest a whole ocr_llm_issue.js Workflow run's result JSON")
    p_ingest_wf.add_argument("result_json")
    p_ingest_wf.add_argument("--model", default="sonnet")
    p_ingest_wf.add_argument(
        "--run-dir",
        help="Workflow run's transcript dir, to also record usage telemetry "
             "(requires --date, --total-tokens, --agent-count, --duration-ms "
             "from the completion notification's own <usage> block)")
    p_ingest_wf.add_argument("--date", help="YYYY-MM-DD, required with --run-dir")
    p_ingest_wf.add_argument("--total-tokens", type=int)
    p_ingest_wf.add_argument("--agent-count", type=int)
    p_ingest_wf.add_argument("--duration-ms", type=int)
    p_ingest_wf.set_defaults(func=_cmd_ingest_workflow_result)

    p_verify = sub.add_parser(
        "verify-coverage",
        help="Check every page_ocr_blocks row is claimed by exactly one item")
    p_verify.add_argument("date", help="YYYY-MM-DD")
    p_verify.set_defaults(func=_cmd_verify_coverage)

    p_cleanup = sub.add_parser("ingest-cleanup", help="Ingest a cleanup-pass result JSON")
    p_cleanup.add_argument("page_id")
    p_cleanup.add_argument("result_json")
    p_cleanup.add_argument("--model", default="sonnet")
    p_cleanup.set_defaults(func=_cmd_ingest_cleanup)

    p_items = sub.add_parser("ingest-items", help="Ingest an item-markup result JSON")
    p_items.add_argument("page_id")
    p_items.add_argument("result_json")
    p_items.add_argument("--model", default="sonnet")
    p_items.set_defaults(func=_cmd_ingest_items)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
