"""LLM-facing CLI surface for the Almonte Gazette pipeline.

`mvtm` is the umbrella command an LLM agent uses to inspect and (later)
correct the cut-up archive. The per-stage CLIs (`split_page.py`,
`detect_ads.py`, ...) remain as human-facing diagnostic tools; this file
is the consolidated surface that emits a uniform JSON envelope on
stdout for machine consumption.

Envelope shape (frozen across all subcommands):

    {
      "ok": true | false,
      "command": "<subcommand name>",
      "transaction_id": null | <int>,
      "result": { ... per-command payload ... },
      "errors": [{"code": "...", "message": "..."}, ...]
    }

`transaction_id` is null for read-only commands (this skeleton has only
read-only commands). It will be the FK into `cli_history` once mutators
land.

Output discipline: JSON envelope to stdout, human-facing logs to
stderr. LLM consumers read stdout; tee'd progress prints don't pollute
the parsed channel.

See: /Users/peter/.claude/plans/cli-walking-skeleton.md
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys
from contextlib import closing


# ── Envelope helpers ─────────────────────────────────────────────────

def _emit(envelope: dict) -> None:
    """Write the envelope to stdout as a single JSON object.

    Compact separators — the consumer is the LLM (or `python3 -m
    json.tool` for humans). Keeping it on one line makes it trivially
    line-delimited if a future caller streams multiple envelopes.
    """
    json.dump(envelope, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def emit_ok(command: str, result: dict, transaction_id=None) -> int:
    """Emit a success envelope. Returns process exit code (0)."""
    _emit({
        "ok": True,
        "command": command,
        "transaction_id": transaction_id,
        "result": result,
        "errors": [],
    })
    return 0


def emit_error(command: str, code: str, message: str, exit_code: int = 2,
               **extra) -> int:
    """Emit a failure envelope. Returns process exit code.

    `code` is one of the four frozen error codes from the design doc:
    validation_error, not_found, pipeline_error, would_clobber_hand_edit.
    The skeleton only emits the first two.

    Extra kwargs are merged into the error object so callers can attach
    e.g. tracebacks (pipeline_error) or row keys (would_clobber_hand_edit)
    without changing this signature.
    """
    err = {"code": code, "message": message}
    err.update(extra)
    _emit({
        "ok": False,
        "command": command,
        "transaction_id": None,
        "result": {},
        "errors": [err],
    })
    return exit_code


# ── show ─────────────────────────────────────────────────────────────

# Chart-heavy keys deliberately stripped from `show` output. The LLM
# has no use for per-pixel sawtooth profiles; rendering charts is the
# viewer's job. Add `--include-charts` if a future need surfaces.
_ANALYSIS_DROP_KEYS = (
    "profile_chart", "composite_profile", "strip_profiles",
    "headline_chart", "body_text_charts",
)
# Per-headline embedded chart fields — same rationale.
_HEADLINE_DROP_KEYS = ("row_chart", "col_charts")


def _validate_date_page(year: int, month: int, day: int, page: int):
    """Returns (ok, error_message). Page range hard-coded 1..8 — every
    issue in the corpus is an 8-page broadsheet."""
    try:
        _dt.date(year, month, day)
    except ValueError as e:
        return False, f"invalid date {year}-{month}-{day}: {e}"
    if not (1 <= page <= 8):
        return False, f"page {page} out of range 1..8"
    return True, None


def _row_to_dict(cursor, row):
    """sqlite3 row_factory style helper. Returns dict keyed by column name."""
    return {c[0]: row[i] for i, c in enumerate(cursor.description)}


def _fetch_layout(conn, year, month, day, page):
    cur = conn.execute(
        "SELECT * FROM page_layouts WHERE year=? AND month=? AND day=? AND page=?",
        (year, month, day, page),
    )
    r = cur.fetchone()
    if r is None:
        return None
    d = _row_to_dict(cur, r)
    return {
        "num_columns": d["num_columns"],
        "boundaries_pct": json.loads(d["boundary_positions"]),
        "widths_pct": json.loads(d["column_widths"]),
        "quality_flags": json.loads(d["quality_flags"] or "[]"),
        "confidence": d["confidence"],
        "hand_edited": False,  # column lands in commit 3
    }


def _fetch_geometry(conn, year, month, day, page):
    cur = conn.execute(
        "SELECT * FROM page_geometry WHERE year=? AND month=? AND day=? AND page=?",
        (year, month, day, page),
    )
    r = cur.fetchone()
    if r is None:
        return None
    d = _row_to_dict(cur, r)
    # The DB stores horizontal extents only — top/bottom are computed
    # at detect-time from the page profile and not persisted. Expose
    # the actual stored shape rather than inventing fields.
    return {
        "r2_left_pct": d["r2_left"],
        "r2_right_pct": d["r2_right"],
        "r3_left_pct": d["r3_left"],
        "r3_right_pct": d["r3_right"],
        "text_left_pct": d["text_left"],
        "text_right_pct": d["text_right"],
        "binding_side": d["binding_side"],
        "hand_edited": False,  # column lands in commit 3
    }


def _fetch_ads(conn, year, month, day, page):
    cur = conn.execute(
        "SELECT uuid, x_pct, y_pct, w_pct, h_pct, x_end_pct, y_end_pct, "
        "rect_ratio, aspect, cols, confidence, image_filename "
        "FROM detected_ads WHERE year=? AND month=? AND day=? AND page=? "
        "ORDER BY y_pct, x_pct",
        (year, month, day, page),
    )
    out = []
    for r in cur.fetchall():
        d = _row_to_dict(cur, r)
        d["hand_edited"] = False  # column lands in commit 3
        out.append(d)
    return out


def _read_analysis(page_dir):
    """Read page_analysis.json and strip chart-heavy keys.

    Returns a dict (possibly empty) of the layer keys we expose.
    Missing file is not an error — it just means the post-detection
    layers haven't been computed for this page.
    """
    path = os.path.join(page_dir, "page_analysis.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        a = json.load(f)
    for k in _ANALYSIS_DROP_KEYS:
        a.pop(k, None)
    if "headlines" in a:
        a["headlines"] = [
            {k: v for k, v in h.items() if k not in _HEADLINE_DROP_KEYS}
            for h in a["headlines"]
        ]
    return a


def _list_files(page_dir, ads_dir, ads):
    """Filesystem pointers for the page. All paths repo-relative.

    Ad images live in a sibling `<issue>/ads/p<N>/` directory, not
    inside the per-page dir — that's where process_issue writes them.
    """
    cwd = os.getcwd()
    files = {}
    for name in ("page_raw.png", "body_blur.png"):
        p = os.path.join(page_dir, name)
        if os.path.exists(p):
            files[name.replace(".png", "")] = os.path.relpath(p, cwd)
    if os.path.isdir(page_dir):
        cols = sorted(
            os.path.relpath(os.path.join(page_dir, f), cwd)
            for f in os.listdir(page_dir)
            if "_col" in f and f.endswith(".png")
        )
        if cols:
            files["columns"] = cols
    ad_imgs = []
    if os.path.isdir(ads_dir):
        for ad in ads:
            fn = ad.get("image_filename")
            if fn:
                p = os.path.join(ads_dir, fn)
                if os.path.exists(p):
                    ad_imgs.append(os.path.relpath(p, cwd))
    if ad_imgs:
        files["ad_images"] = ad_imgs
    return files


def cmd_show(args) -> int:
    ok, err = _validate_date_page(args.year, args.month, args.day, args.page)
    if not ok:
        return emit_error("show", "validation_error", err)

    issue = f"{args.year:04d}-{args.month:02d}-{args.day:02d}"
    page_dir = os.path.join(args.output_root, issue, f"p{args.page}")
    ads_dir = os.path.join(args.output_root, issue, "ads", f"p{args.page}")

    with closing(sqlite3.connect(args.db)) as conn:
        layout = _fetch_layout(conn, args.year, args.month, args.day, args.page)
        if layout is None:
            return emit_error(
                "show", "not_found",
                f"no page_layouts row for {issue} p{args.page}",
            )
        geometry = _fetch_geometry(conn, args.year, args.month, args.day, args.page)
        ads = _fetch_ads(conn, args.year, args.month, args.day, args.page)

    analysis = _read_analysis(page_dir)
    files = _list_files(page_dir, ads_dir, ads)

    result = {
        "issue": issue,
        "page": args.page,
        "layout": layout,
        "geometry": geometry,  # may be None if no row
        "ads": ads,
        "headlines": analysis.get("headlines", []),
        "gutter_fills": analysis.get("gutter_fills", []),
        "body_text": analysis.get("body_text", []),
        "h_rules": analysis.get("h_rules", []),
        "large_type": analysis.get("large_type", []),
        "files": files,
    }
    return emit_ok("show", result)


# ── Argparse plumbing ────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mvtm",
        description="LLM-facing CLI for the Almonte Gazette pipeline. "
                    "All commands emit a JSON envelope on stdout. Pipe "
                    "through `python3 -m json.tool` for human reading.",
    )
    p.add_argument("--db", default="data/mvtm.db",
                   help="SQLite DB path (default: data/mvtm.db)")
    p.add_argument("--output-root", default="columns",
                   help="Per-issue output dir root (default: columns)")
    sub = p.add_subparsers(dest="cmd", required=True)

    show = sub.add_parser(
        "show",
        help="Read-only inspection of one page: layout, geometry, "
             "ads, post-detection layers, file pointers.",
    )
    show.add_argument("year", type=int)
    show.add_argument("month", type=int)
    show.add_argument("day", type=int)
    show.add_argument("page", type=int)
    show.set_defaults(func=cmd_show)

    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
