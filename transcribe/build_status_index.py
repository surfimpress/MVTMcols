"""Write columns/transcribe_status.json for the main viewer to read.

The main viewer (viewer.html) renders one card per processed issue,
sourced from columns/index.json. This module produces a sibling index
that says, for each issue with a transcribe.db footprint:

    {
      "1912-12-27": {
        "pass1": true,        # all expected columns + ads transcribed
        "pass2": true,        # items extracted on every page
        "manifest_dir": true, # preview/iiif/<issue>/ exists with manifests
      }
    }

The viewer overlays "circled 1" / "circled 2" badges on the issue
cards based on this. Issues that haven't been touched at all are
omitted; the viewer treats absence as "no transcription".

Run independently of the cutting pipeline:

    python3 -m transcribe.build_status_index

Cheap (small queries on transcribe.db + a few directory checks). Can
be re-run anytime; outputs are atomic.

Why a separate file: viewer.html already loads index.json from the
cutting pipeline. We deliberately keep transcribe state out of that
file (the cutting/transcribe boundary is the design) and overlay it
client-side instead.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MVTM_DB = ROOT / "data" / "mvtm.db"
TXC_DB = ROOT / "transcribe" / "data" / "transcribe.db"
COLUMNS_DIR = ROOT / "columns"
PREVIEW_DIR = ROOT / "preview" / "iiif"
OUT_PATH = COLUMNS_DIR / "transcribe_status.json"


def _atomic_write_json(path: Path, payload) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def _expected_columns_per_issue(con_mvtm) -> dict:
    """Returns {(y,m,d): expected_column_count} from page_layouts.

    Each row's `boundary_positions` is a JSON list with len = ncols+1.
    Sum across the issue's pages.
    """
    out: dict = {}
    for y, m, d, bp_json in con_mvtm.execute(
        "SELECT year, month, day, boundary_positions FROM page_layouts"
    ):
        try:
            bp = json.loads(bp_json) if bp_json else []
        except Exception:
            bp = []
        ncols = max(0, len(bp) - 1)
        key = (y, m, d)
        out[key] = out.get(key, 0) + ncols
    return out


def _expected_pages_per_issue(con_mvtm) -> dict:
    out: dict = {}
    for y, m, d in con_mvtm.execute(
        "SELECT year, month, day FROM page_layouts"
    ):
        key = (y, m, d)
        out[key] = out.get(key, 0) + 1
    return out


def _expected_ads_per_issue(con_mvtm) -> dict:
    out: dict = {}
    for y, m, d, n in con_mvtm.execute(
        "SELECT year, month, day, COUNT(*) FROM detected_ads "
        "GROUP BY year, month, day"
    ):
        out[(y, m, d)] = n
    return out


def _done_columns_per_issue(con_txc) -> dict:
    """Distinct (page,col_idx) per issue with status='done'."""
    out: dict = {}
    for y, m, d, n in con_txc.execute(
        "SELECT year, month, day, COUNT(DISTINCT page || ':' || col_idx) "
        "FROM column_transcripts WHERE status='done' "
        "GROUP BY year, month, day"
    ):
        out[(y, m, d)] = n
    return out


def _done_ads_per_issue(con_txc) -> dict:
    out: dict = {}
    for y, m, d, n in con_txc.execute(
        "SELECT year, month, day, COUNT(DISTINCT ad_uuid) "
        "FROM ad_transcripts WHERE status='done' "
        "GROUP BY year, month, day"
    ):
        out[(y, m, d)] = n
    return out


def _items_pages_per_issue(con_txc) -> dict:
    """Distinct pages-with-items per issue."""
    out: dict = {}
    for y, m, d, n in con_txc.execute(
        "SELECT year, month, day, COUNT(DISTINCT page) FROM items "
        "GROUP BY year, month, day"
    ):
        out[(y, m, d)] = n
    return out


def _manifest_dir_for(year: int, month: int, day: int) -> bool:
    issue_dir = PREVIEW_DIR / f"{year}-{month:02d}-{day:02d}"
    return (issue_dir / "manifest_pass1.json").exists() or \
           (issue_dir / "manifest_pass2.json").exists()


def build() -> dict:
    if not MVTM_DB.exists():
        raise FileNotFoundError(f"mvtm.db not found at {MVTM_DB}")
    if not TXC_DB.exists():
        return {"issues": {}}

    with closing(sqlite3.connect(f"file:{MVTM_DB}?mode=ro", uri=True)) as cm, \
         closing(sqlite3.connect(f"file:{TXC_DB}?mode=ro", uri=True)) as ct:
        exp_cols = _expected_columns_per_issue(cm)
        exp_pages = _expected_pages_per_issue(cm)
        exp_ads = _expected_ads_per_issue(cm)
        done_cols = _done_columns_per_issue(ct)
        done_ads = _done_ads_per_issue(ct)
        items_pages = _items_pages_per_issue(ct)

    # Universe of issues to consider: anything touched by transcribe.
    keys = set(done_cols) | set(done_ads) | set(items_pages)

    issues: dict = {}
    for key in sorted(keys):
        y, m, d = key
        dir_name = f"{y}-{m:02d}-{d:02d}"
        ec = exp_cols.get(key, 0)
        ea = exp_ads.get(key, 0)
        dc = done_cols.get(key, 0)
        da = done_ads.get(key, 0)
        ep = exp_pages.get(key, 0)
        ip = items_pages.get(key, 0)
        # pass-1 done: all expected columns transcribed AND all expected
        # ads transcribed. ec==0 (no columns yet detected) means we
        # don't know what's expected — skip the badge.
        pass1 = ec > 0 and dc >= ec and (ea == 0 or da >= ea)
        # pass-2 done: items exist on every page of the issue.
        pass2 = ep > 0 and ip >= ep
        issues[dir_name] = {
            "pass1": pass1,
            "pass2": pass2,
            "n_cols_done": dc, "n_cols_expected": ec,
            "n_ads_done": da, "n_ads_expected": ea,
            "n_pages_with_items": ip, "n_pages_expected": ep,
            "manifest_dir": _manifest_dir_for(y, m, d),
        }
    return {"issues": issues}


def main() -> int:
    payload = build()
    COLUMNS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(OUT_PATH, payload)
    n = len(payload["issues"])
    n_p1 = sum(1 for v in payload["issues"].values() if v["pass1"])
    n_p2 = sum(1 for v in payload["issues"].values() if v["pass2"])
    print(f"wrote {OUT_PATH.relative_to(ROOT)}: "
          f"{n} issues touched · {n_p1} pass-1 done · {n_p2} pass-2 done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
