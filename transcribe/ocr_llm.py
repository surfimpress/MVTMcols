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
import json
import os
import re
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image

import coordinates as _coords
from pdf_utils import get_full_pixmap

from . import db as _db
from . import download as _dl
from . import entity_candidates as _entity_candidates
from . import ingest_item_result as _ingest_items
from . import routing as _routing

WORK_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "ocr_llm")
TESSDATA_DIR = os.path.join(_db.REPO_ROOT, "transcribe", "work", "tessdata_best")
TESSDATA_URL = (
    "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata"
)
OCR_ENGINE = "tesseract 5.5.3"
RENDER_DPI = 300
DISPLAY_MAX_W = 1400
CONF_TRIAGE_THRESHOLD = 85

HOCR_NS = {"x": "http://www.w3.org/1999/xhtml"}
_BBOX_RE = re.compile(r"bbox (\d+) (\d+) (\d+) (\d+)")
_WCONF_RE = re.compile(r"x_wconf (\d+)")

# LLM item type -> items.item_type. item_type is free TEXT (no CHECK
# constraint) and the taxonomy already grows organically -- 'photo',
# 'index', 'promo' are new values introduced by this route, not
# remapped onto the pre-1980 vocabulary.
ITEM_TYPE_PASSTHROUGH = {
    "article", "photo", "ad", "notice", "masthead", "index", "promo", "other",
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


def render_page(pdf_path: str, date_str: str, page: int) -> dict:
    """Render the page's PDF at RENDER_DPI (full-res, OCR's own
    coordinate space) plus a downscaled display copy (what the
    item-markup LLM pass sees). Returns paths + pixel dimensions for
    both rasters."""
    out_dir = _page_work_dir(date_str, page)
    pix = get_full_pixmap(pdf_path, 0, RENDER_DPI)
    img = Image.frombytes("RGB", (pix.w, pix.h), pix.samples)

    full_png = os.path.join(out_dir, "page_full.png")
    img.save(full_png)

    scale = DISPLAY_MAX_W / pix.w
    display_h = round(pix.h * scale)
    display_img = img.resize((DISPLAY_MAX_W, display_h), Image.LANCZOS)
    display_png = os.path.join(out_dir, "page_display.png")
    display_img.save(display_png)

    return {
        "out_dir": out_dir,
        "full_png": full_png,
        "display_png": display_png,
        "page_px_w": pix.w,
        "page_px_h": pix.h,
        "display_w": DISPLAY_MAX_W,
        "display_h": display_h,
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
                        tessdata_dir: str) -> str:
    """Run Tesseract with Sauvola local-adaptive thresholding (fixes
    grey-sidebar/fold-shadow blackout on greyscale scans -- see
    comparison_tesseract_2001-01-03.html) and tessdata_best. Returns
    the .hocr file path."""
    subprocess.run(
        [
            "tesseract", image_path, output_base,
            "--dpi", str(RENDER_DPI),
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
            created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            page_id, year, month, day, page, pdf_path, render["full_png"],
            RENDER_DPI, OCR_ENGINE, "tessdata_best", "sauvola",
            hocr_path, len(words), hocr_mean_conf, layout_class,
            render["display_png"], render["display_w"], render["display_h"],
            now,
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

ITEMS_PROMPT_TEMPLATE = """Almonte Gazette, {date} page {page}. Blocks: {blocks_path} (page image space, {display_w}x{display_h} px). Page image: {display_png}. Entity candidates: {candidates_path}."""


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

    candidates = _entity_candidates.build_candidate_lists(conn, page_row["year"])
    candidates_path = os.path.join(out_dir, "entity_candidates.json")
    with open(candidates_path, "w") as f:
        json.dump(candidates, f)

    prompt = ITEMS_PROMPT_TEMPLATE.format(
        date=date_str, page=page_row["page"],
        display_png=page_row["display_image_path"],
        display_w=disp_w, display_h=disp_h, blocks_path=blocks_path,
        candidates_path=candidates_path,
    )
    ticket_path = os.path.join(out_dir, "items_ticket.json")
    with open(ticket_path, "w") as f:
        json.dump({
            "page_id": page_id, "kind": "items", "agent_type": "ocr-items",
            "blocks_path": blocks_path, "display_png": page_row["display_image_path"],
            "candidates_path": candidates_path,
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

        # Entity mentions -- reuses ingest_item_result's upsert_entity via
        # _insert_mentions rather than re-deriving dedup logic here. The
        # LLM-supplied candidate "id" (if any) is only a hint that shaped
        # its own generation; the actual dedup decision is always the
        # normalised-key lookup inside upsert_entity, same as the pre-1980
        # route -- an LLM-misattributed id is never trusted directly.
        mention_date = (f"{page_row['year']:04d}-{page_row['month']:02d}-"
                        f"{page_row['day']:02d}")
        for entity_table, junction_table, fk_col, name_keys in (
            ("people", "item_people_mentions", "person_id", ("name", "full_name")),
            ("organizations", "item_organizations_mentions", "organization_id", ("name",)),
            ("places", "item_places_mentions", "place_id", ("name",)),
            ("products", "item_products_mentions", "product_id", ("name",)),
            ("events", "item_events_mentions", "event_id", ("name",)),
        ):
            _ingest_items._insert_mentions(
                conn, item_id=item_id,
                mentions=it.get(entity_table) or [],
                entity_table=entity_table,
                junction_table=junction_table,
                junction_fk_col=fk_col,
                name_keys=name_keys,
                mention_date=mention_date,
            )
        n += 1
    conn.commit()
    return n


def ingest_items_result(conn, page_id: str, result_path: str,
                         model: str = "sonnet") -> int:
    with open(result_path) as f:
        items = json.load(f)
    return ingest_items_data(conn, page_id, items, model)


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
        hocr_path = run_tesseract_hocr(render["full_png"], hocr_base, tessdata_dir)
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


def _cmd_render_issue(args):
    year, month, day = (int(x) for x in args.date.split("-"))
    conn = _db.open_connection(attach_mvtm=True)
    try:
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

        results = []
        for page in pages:
            r = _render_one_page(conn, year, month, day, page)
            status = "already rendered" if r["already_rendered"] else "rendered"
            print(f"  page {page}: {status}, page_id={r['page_id']}")
            results.append(r)

        out_dir = _issue_work_dir(args.date)
        args_path = os.path.join(out_dir, "workflow_args.json")
        with open(args_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\n{len(results)} pages ready. Workflow args written to:\n{args_path}")
        print("Next: invoke Workflow with scriptPath="
              "'transcribe/workflows/ocr_llm_issue.js' and this file's "
              "contents as args, then save its result and run "
              "'ingest-workflow-result'.")
    finally:
        conn.close()


def ingest_workflow_result_data(conn, pages: list[dict], model: str = "sonnet") -> dict:
    """Ingest a whole Workflow run's result array
    ([{page_id, page, cleanup, items}, ...]). Skips any page that
    already has items ingested (idempotent -- safe to re-run against
    a partially-ingested batch, e.g. after a crash mid-loop)."""
    ingested, skipped = [], []
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
        n_blocks = ingest_cleanup_data(conn, page_id, p.get("cleanup") or [], model)
        n_items = ingest_items_data(conn, page_id, p.get("items") or [], model)
        ingested.append({"page": p["page"], "blocks": n_blocks, "items": n_items})
    return {"ingested": ingested, "skipped_already_done": skipped}


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
            print(f"  page {row['page']}: ingested {row['blocks']} blocks, {row['items']} items")
        if summary["skipped_already_done"]:
            print(f"  skipped (already done): pages {summary['skipped_already_done']}")
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
    p_render_issue.set_defaults(func=_cmd_render_issue)

    p_ingest_wf = sub.add_parser(
        "ingest-workflow-result",
        help="Ingest a whole ocr_llm_issue.js Workflow run's result JSON")
    p_ingest_wf.add_argument("result_json")
    p_ingest_wf.add_argument("--model", default="sonnet")
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
