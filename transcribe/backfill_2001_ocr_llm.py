"""
One-off historical backfill, not a repeatable tool: loads the
2001-01-03 pages 1-3 Tesseract+LLM economization test (see
comparison_tesseract_2001-01-03.html, 2026-08-08 session) into the
new OCR+LLM schema (pages, page_ocr_blocks, items, items_ocr_ext,
item_ocr_block_spans -- schema.sql version 4).

Source data lived in /tmp for that session and is not guaranteed to
still exist -- this script is kept as a record of exactly how that
first issue's data was mapped into the schema (block-id indexing,
display-px vs full-page-px coordinate spaces, the raw-text-leak fix
for untriaged high-"confidence" noise blocks), for reference when
building the real repeatable ingestion path for 1980s+ issues.

Run from the repo root: python3 -m transcribe.backfill_2001_ocr_llm
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

import coordinates as _coords
from . import db as _db

DB_PATH = _db.TRANSCRIBE_DB_PATH
NOW = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

TYPE_MAP = {
    'article': 'article',
    'photo': 'photo',
    'ad': 'display_ad',
    'notice': 'notice',
    'masthead': 'masthead',
    'index': 'index',
    'promo': 'promo',
    'other': 'other',
}

# per-page source facts, gathered/verified earlier in this session
PAGES = [
    {
        'page': 1,
        'pdf_path': '/tmp/2001-01-03-01.pdf',
        'hocr_path': '/tmp/2001_p1_sauvola.hocr',
        'interactive_data': '/tmp/2001_sauvola_interactive_data.json',
        'cleaned': '/tmp/2001_cleaned_blocks.json',
        'items': '/tmp/2001_items.json',
        'display_png': '/tmp/2001_display.png',
        'display_w': 1399, 'display_h': 2209,
        'page_px_w': 5460, 'page_px_h': 8616,
    },
    {
        'page': 2,
        'pdf_path': '/tmp/2001-01-03-02.pdf',
        'hocr_path': '/tmp/2001_p2_sauvola.hocr',
        'interactive_data': '/tmp/2001_p2_interactive_data.json',
        'cleaned': '/tmp/2001_p2_cleaned.json',
        'items': '/tmp/2001_p2_items.json',
        'display_png': '/tmp/2001_p2_display.png',
        'display_w': 1400, 'display_h': 2191,
        'page_px_w': 5520, 'page_px_h': 8640,
        'cleanup_tokens': (32592, 37744),
    },
    {
        'page': 3,
        'pdf_path': '/tmp/2001-01-03-03.pdf',
        'hocr_path': '/tmp/2001_p3_sauvola.hocr',
        'interactive_data': '/tmp/2001_p3_interactive_data.json',
        'cleaned': '/tmp/2001_p3_cleaned.json',
        'items': '/tmp/2001_p3_items.json',
        'display_png': '/tmp/2001_p3_display.png',
        'display_w': 1400, 'display_h': 2210,
        'page_px_w': 5472, 'page_px_h': 8640,
        'cleanup_tokens': (31902, 42914),
    },
]

YEAR, MONTH, DAY = 2001, 1, 3


def load(path):
    with open(path) as f:
        return json.load(f)


def new_id():
    return str(uuid.uuid4())


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')

    pages_created = 0
    blocks_created = 0
    items_created = 0
    spans_created = 0

    for p in PAGES:
        page_no = p['page']
        disp_w, disp_h = p['display_w'], p['display_h']
        page_px_w, page_px_h = p['page_px_w'], p['page_px_h']

        raw = load(p['interactive_data'])
        raw_blocks = raw['blocks']

        cleaned_by_id = {}
        if p['cleaned']:
            cleaned_by_id = {c['id']: c for c in load(p['cleaned'])}

        # hOCR mean confidence / word count from the raw word list
        words = raw.get('words')
        if words is None:
            # page 1's interactive_data only stored blocks; recompute
            # word count/mean confidence isn't available without the
            # word list -- fall back to block-level average.
            confs = [b['avg_conf'] for b in raw_blocks]
        else:
            confs = [w['conf'] for w in words]
        hocr_word_count = len(words) if words is not None else None
        hocr_mean_conf = round(sum(confs) / len(confs), 2) if confs else None

        page_id = new_id()
        conn.execute(
            """INSERT INTO pages (
                id, year, month, day, page, pdf_path, page_raw_path,
                render_dpi, ocr_engine, ocr_trained_data, thresholding_method,
                hocr_path, hocr_word_count, hocr_mean_confidence, layout_class,
                created_at, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                page_id, YEAR, MONTH, DAY, page_no, p['pdf_path'], p['display_png'],
                300, 'tesseract 5.5.3', 'tessdata_best', 'sauvola',
                p['hocr_path'], hocr_word_count, hocr_mean_conf, 'modular',
                NOW, 'Backfilled from 2026-08-08 OCR+LLM economization test '
                     '(comparison_tesseract_2001-01-03.html).',
            ),
        )
        pages_created += 1

        # page_ocr_blocks -- one row per Tesseract block, real pixel bbox
        block_id_by_idx = {}
        for idx, b in enumerate(raw_blocks):
            x0, y0, x1, y1 = b['bbox']
            c = cleaned_by_id.get(idx)
            block_row_id = new_id()
            block_id_by_idx[idx] = block_row_id
            conn.execute(
                """INSERT INTO page_ocr_blocks (
                    id, page_id, block_idx, bbox_left_pct, bbox_top_pct,
                    bbox_right_pct, bbox_bottom_pct, conf, n_words,
                    raw_text, cleaned_text, cleanup_status, triaged,
                    model, tokens_in, tokens_out, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    block_row_id, page_id, idx,
                    _coords.px_to_pct(x0, page_px_w), _coords.px_to_pct(y0, page_px_h),
                    _coords.px_to_pct(x1, page_px_w), _coords.px_to_pct(y1, page_px_h),
                    b['avg_conf'], b['n_words'],
                    b['text'], c['cleaned'] if c else None,
                    c['status'] if c else None, 1 if c else 0,
                    'sonnet' if c else None,
                    None, None,  # per-block token split isn't recoverable from the batched cleanup call
                    NOW,
                ),
            )
            blocks_created += 1

        # items -- bbox given by the LLM in *display* pixel space, must
        # convert display px -> pct using the display image's own dims,
        # not the full-res OCR page dims (different raster).
        # NOTE: the raw items JSON never had transcript/caption text
        # written back into it -- the HTML-build scripts computed that
        # in-memory only. Reconstruct it here the same way they did:
        # cleaned text where a block was triaged (skip if status is
        # 'noise'), raw OCR text otherwise (untriaged = trusted as-is).
        def block_line(bid):
            c = cleaned_by_id.get(bid)
            if c is None:
                return raw_blocks[bid]['text']
            if c['status'] == 'noise':
                return None
            return c['cleaned']

        items = load(p['items'])
        for it in items:
            b = it['bbox']
            item_type = TYPE_MAP.get(it.get('type'), 'other')
            item_id = new_id()
            body_lines = [block_line(bid) for bid in (it.get('block_ids') or [])]
            full_text = '\n'.join(l for l in body_lines if l)
            cap_lines = [block_line(bid) for bid in (it.get('caption_block_ids') or [])]
            caption = '\n'.join(l for l in cap_lines if l)
            if caption:
                full_text = (full_text + '\n\nCaption: ' + caption).strip()
            conn.execute(
                """INSERT INTO items (
                    id, item_type, year, month, day, page,
                    bbox_left_pct, bbox_top_pct, bbox_right_pct, bbox_bottom_pct,
                    headline, full_text, model, created_at, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, item_type, YEAR, MONTH, DAY, page_no,
                    _coords.px_to_pct(b['x'], disp_w), _coords.px_to_pct(b['y'], disp_h),
                    _coords.px_to_pct(b['x'] + b['w'], disp_w), _coords.px_to_pct(b['y'] + b['h'], disp_h),
                    it.get('label'), full_text, 'sonnet', NOW,
                    'Backfilled from 2026-08-08 OCR+LLM economization test.',
                ),
            )
            items_created += 1

            conn.execute(
                """INSERT INTO items_ocr_ext (item_id, media_paths_json, created_at)
                   VALUES (?,?,?)""",
                (item_id, json.dumps({'page_display_png': p['display_png']}), NOW),
            )

            seq = 0
            for bid in it.get('block_ids', []) or []:
                if bid not in block_id_by_idx:
                    continue
                conn.execute(
                    """INSERT INTO item_ocr_block_spans
                       (item_id, page_ocr_block_id, role, sequence)
                       VALUES (?,?,?,?)""",
                    (item_id, block_id_by_idx[bid], 'body', seq),
                )
                seq += 1
                spans_created += 1
            for bid in it.get('caption_block_ids', []) or []:
                if bid not in block_id_by_idx:
                    continue
                conn.execute(
                    """INSERT INTO item_ocr_block_spans
                       (item_id, page_ocr_block_id, role, sequence)
                       VALUES (?,?,?,?)""",
                    (item_id, block_id_by_idx[bid], 'caption', seq),
                )
                seq += 1
                spans_created += 1

    conn.commit()
    conn.close()
    print(f"pages={pages_created} blocks={blocks_created} "
          f"items={items_created} spans={spans_created}")


if __name__ == '__main__':
    main()
