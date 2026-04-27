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
import contextlib
import datetime as _dt
import json
import os
import sqlite3
import sys
import time
import traceback
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


# ── recompute-layers ─────────────────────────────────────────────────

# The complete set of post-detection layers. Kept here as the single
# source of truth so `--layers` validation and the per-layer key map
# below can't drift out of sync.
_LAYER_KEYS = {
    # Each layer maps to the page_analysis.json keys it produces.
    # When a layer is recomputed, only these keys are spliced; every
    # other key in the file is preserved byte-for-byte.
    "headlines": ("headlines", "headline_chart", "gutter_fills"),
    "body_text": ("body_text", "body_text_charts", "h_rules", "large_type"),
}


def _validate_layers(arg_layers):
    """Parse and validate the --layers argument. Returns (list, None)
    on success or (None, message) on failure. Default (no flag) is the
    full set; an explicit empty value is rejected — that's a footgun."""
    valid = list(_LAYER_KEYS.keys())
    if arg_layers is None:
        return list(valid), None
    requested = [s.strip() for s in arg_layers.split(",") if s.strip()]
    if not requested:
        return None, "--layers given but empty"
    bad = [r for r in requested if r not in _LAYER_KEYS]
    if bad:
        return None, f"unknown layer(s) {bad}; valid: {valid}"
    # Dedupe while preserving order so the result envelope's
    # `layers_run` reflects the user's intent.
    return list(dict.fromkeys(requested)), None


def _locate_cached_pdf(year, month, day, page):
    """Look in the same /tmp/issue_<date>/<file>.pdf location that
    download_issue populates. Returns the path if a valid PDF is
    cached, else None.

    Refuse-with-not_found is intentional: `recompute-layers` is for
    fixing one thing on an issue that's already been cut up. If the
    PDF is missing, the right move is to run `process_issue` first,
    not to silently re-download. Add a `--allow-download` flag if a
    real need surfaces."""
    issue = f"{year:04d}-{month:02d}-{day:02d}"
    fname = f"{issue}-{page:02d}.pdf"
    candidates = [
        f"/tmp/issue_{issue}/{fname}",
        os.path.join(os.path.expanduser("~"), f"issue_{issue}", fname),
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    if f.read(5) == b"%PDF-":
                        return p
            except OSError:
                continue
    return None


def _do_recompute(args, layers, pdf_path, page_dir, analysis_path,
                  meta_path, ads):
    """Run the requested detectors and splice their output into the
    page_analysis.json file. Returns the result dict for the envelope.

    Detector exceptions propagate up — caller wraps them in a
    `pipeline_error` envelope. The shared-functions principle here is
    load-bearing: this function reproduces process_issue.py:559-602
    exactly. If the kwargs to detect_headlines/detect_body_text change
    there, this must change in the same commit."""
    # Local imports keep the CLI startup path light; the heavy ML/PDF
    # imports only fire when `recompute-layers` is actually invoked.
    from detect_ads import get_ad_exclusion_zones
    from page_profile import profile_page

    # Existing analysis is the splice target. We never throw away keys
    # we didn't ask to recompute.
    with open(analysis_path) as f:
        analysis = json.load(f)

    # Reproduce ctx.ad_zones from the DB ads, same filter that
    # build_context applies (cols >= 2, confidence within tier).
    ad_zones = get_ad_exclusion_zones(ads)

    # Re-derive r2 by re-running the page profile. Top/bottom of r2
    # aren't persisted (page_geometry stores horizontal extents only),
    # so this 1-2s recompute is the only honest source.
    prof = profile_page(pdf_path)
    r2 = prof.get("r2", {})

    files_written = set()
    timings = {}
    counts = {}

    if "headlines" in layers:
        # boundary_pcts == raw clustered detections, byte-equivalent to
        # the list process_issue passes (see process_issue.py:556).
        det_boundaries = analysis.get("detected_boundaries", [])
        boundary_pcts = [b["pct"] for b in det_boundaries]
        # Clear all three keys first; the conditional-set block below
        # then mirrors process_issue.py:564-568 exactly. This is the
        # byte-equivalence contract: a fresh run starts from an empty
        # dict, so the recompute must end with whatever-keys-a-fresh-
        # run-would-have-set, no more, no less. Note that
        # `headline_chart` is sometimes set to None (when hl_analysis
        # is truthy but the chart value within is None) — that None
        # must round-trip into the JSON, so don't substitute a
        # truthy-only filter here.
        for k in _LAYER_KEYS["headlines"]:
            analysis.pop(k, None)
        t0 = time.time()
        if len(boundary_pcts) >= 3:
            from detect_headlines import detect_headlines
            headlines, hl_analysis = detect_headlines(
                pdf_path, boundary_pcts,
                ad_zones=ad_zones,
                r2_top_pct=r2.get("top"),
                r2_bottom_pct=r2.get("bottom"),
            )
            if headlines:
                analysis["headlines"] = headlines
            if hl_analysis:
                analysis["headline_chart"] = hl_analysis.get("headline_chart")
                analysis["gutter_fills"] = hl_analysis.get("gutter_fills")
        timings["headlines"] = round(time.time() - t0, 2)
        counts["headlines"] = len(analysis.get("headlines") or [])

    if "body_text" in layers:
        from detect_body_text import detect_body_text
        with open(meta_path) as f:
            meta = json.load(f)
        meta_cols = [
            {"index": c["index"],
             "left_vw": c["left_vw"],
             "right_vw": c["right_vw"]}
            for c in meta.get("columns", [])
        ]
        # gutter_fills_for_lt: if headlines just ran, use its fresh
        # output; else use the pre-existing gutter_fills in analysis
        # (so a body-text-only recompute behaves the same as the
        # body_text leg of a fresh run, where gutter_fills came from
        # the in-process headlines call).
        gutter_fills_for_lt = analysis.get("gutter_fills")
        # Same byte-equivalence pattern as headlines: clear, then mirror
        # process_issue.py:590-601 conditional sets.
        for k in _LAYER_KEYS["body_text"]:
            analysis.pop(k, None)
        t0 = time.time()
        body_regions, body_charts, blur_img, h_rules, large_type = \
            detect_body_text(
                pdf_path, meta_cols,
                r2_top_pct=r2.get("top"),
                r2_bottom_pct=r2.get("bottom"),
                gutter_fills=gutter_fills_for_lt,
                ad_zones=ad_zones,
            )
        if body_regions:
            analysis["body_text"] = body_regions
        if body_charts:
            analysis["body_text_charts"] = body_charts
        if h_rules:
            analysis["h_rules"] = h_rules
        if large_type:
            analysis["large_type"] = large_type
        if blur_img is not None:
            from PIL import Image as _PILImg
            blur_path = os.path.join(page_dir, "body_blur.png")
            _PILImg.fromarray(blur_img).save(blur_path)
            files_written.add(os.path.relpath(blur_path, os.getcwd()))
        timings["body_text"] = round(time.time() - t0, 2)
        counts["body_text"] = {
            "body_text": len(analysis.get("body_text") or []),
            "h_rules": len(analysis.get("h_rules") or []),
            "large_type": len(analysis.get("large_type") or []),
        }

    # Write the spliced analysis back. Compact format matches the
    # process_issue write at line 612 (no indent kwarg).
    with open(analysis_path, "w") as f:
        json.dump(analysis, f)
    files_written.add(os.path.relpath(analysis_path, os.getcwd()))

    issue = f"{args.year:04d}-{args.month:02d}-{args.day:02d}"
    return {
        "issue": issue,
        "page": args.page,
        "layers_run": layers,
        "counts": counts,
        "elapsed_s": timings,
        "files_written": sorted(files_written),
    }


def cmd_recompute_layers(args) -> int:
    cmd = "recompute-layers"
    ok, err = _validate_date_page(args.year, args.month, args.day, args.page)
    if not ok:
        return emit_error(cmd, "validation_error", err)
    layers, err = _validate_layers(args.layers)
    if err:
        return emit_error(cmd, "validation_error", err)

    issue = f"{args.year:04d}-{args.month:02d}-{args.day:02d}"
    page_dir = os.path.join(args.output_root, issue, f"p{args.page}")
    analysis_path = os.path.join(page_dir, "page_analysis.json")
    meta_path = os.path.join(page_dir, "page_meta.json")
    if not os.path.exists(analysis_path):
        return emit_error(
            cmd, "not_found",
            f"page_analysis.json missing at {analysis_path}; "
            f"run process_issue {args.year} {args.month} {args.day} first",
        )
    if "body_text" in layers and not os.path.exists(meta_path):
        return emit_error(
            cmd, "not_found",
            f"page_meta.json missing at {meta_path}; required for "
            f"body_text recompute (provides column boundaries)",
        )
    pdf_path = _locate_cached_pdf(args.year, args.month, args.day, args.page)
    if pdf_path is None:
        return emit_error(
            cmd, "not_found",
            f"PDF not cached for {issue} p{args.page}; "
            f"expected at /tmp/issue_{issue}/{issue}-{args.page:02d}.pdf — "
            f"run process_issue to populate the cache",
        )

    with closing(sqlite3.connect(args.db)) as conn:
        layout = _fetch_layout(conn, args.year, args.month, args.day, args.page)
        if layout is None:
            return emit_error(
                cmd, "not_found",
                f"no page_layouts row for {issue} p{args.page}",
            )
        ads = _fetch_ads(conn, args.year, args.month, args.day, args.page)

    # Detector progress prints land on stderr so stdout stays a clean
    # JSON channel for the LLM consumer. Wrap only the heavy work, not
    # the envelope emit — the emit must reach the real stdout.
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = _do_recompute(args, layers, pdf_path, page_dir,
                                   analysis_path, meta_path, ads)
    except Exception as e:
        tb = traceback.format_exc()
        return emit_error(
            cmd, "pipeline_error",
            f"{type(e).__name__}: {e}",
            traceback=tb,
        )
    return emit_ok(cmd, result)


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

    rec = sub.add_parser(
        "recompute-layers",
        help="Re-run post-detection layers (headlines, body_text) on "
             "one page and splice results into page_analysis.json. "
             "No DB writes; PDF must already be cached.",
    )
    rec.add_argument("year", type=int)
    rec.add_argument("month", type=int)
    rec.add_argument("day", type=int)
    rec.add_argument("page", type=int)
    rec.add_argument(
        "--layers", default=None,
        help="Comma-separated layer set: headlines,body_text "
             "(default: both). Unknown values rejected — no silent typos.",
    )
    rec.set_defaults(func=cmd_recompute_layers)

    return p


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
