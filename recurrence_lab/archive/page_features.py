"""Page-level recurrence matching (Phase 2, Track B — streaming pass).

Spike implementation. **First iteration is smoke-test only**: process a
single page, run every labelled cluster's exemplar embedding against
the page's half-res DINOv2 patch grid, print hits, optionally render
an overlay. No DB writes, no quarter-res cache. Once we trust the
matching primitive on a known page we add persistence + the corpus
sweep.

Usage:
    python page_features.py --smoke 1947-09-18:2 --render
    python page_features.py --smoke 1948-12-30:2 --render --threshold 0.50

The `--smoke` argument is `<issue_dir>:<page>`. The script writes a
verification overlay PNG to `/tmp/page_features_<issue>_p<N>.png` if
`--render` is given (blue = ad / furniture hits, orange = body_text_fp
hits — colour-blind-safe palette per project convention).

What the matching primitive does
--------------------------------
1. Load the page (1241x1754) and resize to half-res, snapped to the
   DINOv2 patch boundary (14 px). Page becomes ~616x868, patch grid
   ~44x62 x 384-d.
2. For each labelled cluster (category in {ad, body_text_fp, furniture}):
   - Window shape comes from the exemplar PNG's pixel dimensions
     mapped onto the patch grid.
   - Slide the window across the grid (stride configurable, default 1).
   - Mean-pool patch features inside each window, L2-normalise,
     dot-product with the exemplar's CLS embedding from embeddings.npz.
3. Greedy NMS at IoU >= 0.3 within each cluster's hits.

The plan (`~/.claude/plans/stateless-frolicking-moth.md`) covers the
full Phase 2 design — quarter-res cache, restart key, appearances /
proposed_adds / proposed_removes persistence. We add those once smoke
passes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw
from transformers import AutoModel

import torchvision.transforms as T

from db import open_db

LAB_DIR = Path(__file__).resolve().parent
COLUMNS_DIR = LAB_DIR.parent / "columns"
INDEX_PATH = LAB_DIR / "ads_index.json"
EMB_PATH = LAB_DIR / "embeddings.npz"

MODEL_NAME = "facebook/dinov2-small"   # matches embed.py — same 384-d space
PATCH = 14                             # DINOv2 patch size in px
HALF_SCALE = 0.5
NMS_IOU = 0.3

# DINOv2 / ImageNet normalisation. Matches what AutoImageProcessor uses
# for facebook/dinov2-small; we hand-build the transform here because
# we need a non-square non-224 input that the processor would resize.
NORM_MEAN = [0.485, 0.456, 0.406]
NORM_STD = [0.229, 0.224, 0.225]


def pick_device(req: str) -> torch.device:
    if req == "mps" and torch.backends.mps.is_available():
        return torch.device("mps")
    if req == "cuda" and torch.cuda.is_available():
        return torch.device("cuda")
    if req != "cpu":
        print(f"{req} not available; using cpu", file=sys.stderr)
    return torch.device("cpu")


def scaled_dims(page_w: int, page_h: int, scale: float) -> tuple[int, int]:
    """Resize dims at a given scale, snapped down to a multiple of PATCH
    so the model's patch grid is integer.
        scale=0.5: 1241x1754 → 616x868   → 44x62 patches  (cheap, coarse)
        scale=1.0: 1241x1754 → 1232x1750 → 88x125 patches (4x cost, finer)"""
    new_w = int(page_w * scale) // PATCH * PATCH
    new_h = int(page_h * scale) // PATCH * PATCH
    return new_w, new_h


def page_to_patch_grid(img: Image.Image, model: AutoModel, device: torch.device,
                       scale: float = HALF_SCALE) -> np.ndarray:
    """Forward pass at the requested scale. Returns L2-normalised
    (grid_h, grid_w, D)."""
    new_w, new_h = scaled_dims(img.width, img.height, scale)
    img_resized = img.resize((new_w, new_h), Image.BILINEAR)
    transform = T.Compose([
        T.ToTensor(),
        T.Normalize(mean=NORM_MEAN, std=NORM_STD),
    ])
    x = transform(img_resized).unsqueeze(0).to(device)
    with torch.no_grad():
        # interpolate_pos_encoding=True lets DINOv2 handle non-default
        # input sizes by interpolating its learned position embeddings.
        out = model(x, interpolate_pos_encoding=True)
    grid_h = new_h // PATCH
    grid_w = new_w // PATCH
    # last_hidden_state: (1, 1 + grid_h*grid_w, D); first token is CLS.
    # NB we deliberately do NOT L2-normalise each patch here. The
    # candidate pipeline is mean-pool-then-normalise (in slide_match),
    # which mirrors the query-side mean_pool_exemplar_embedding so query
    # and candidate go through identical aggregations.
    patches = out.last_hidden_state[0, 1:].reshape(grid_h, grid_w, -1)
    return patches.cpu().float().numpy()


def mean_pool_exemplar_embedding(crop_path: Path, target_grid_h: int,
                                 target_grid_w: int, model: AutoModel,
                                 device: torch.device) -> np.ndarray:
    """Compute a mean-pool query embedding from a cropped exemplar PNG.

    The crop is resized to exactly the same physical scale as the
    candidate window on the page side: target_grid_h * PATCH px tall by
    target_grid_w * PATCH px wide. After the forward pass we drop the
    CLS token, mean-pool the patch features, and L2-normalise.

    This makes the query and candidate both: (a) come from a forward
    pass over a region of similar content, (b) be aggregated identically
    via mean-pool over an integer patch grid of the same shape. Removes
    the CLS-vs-mean-pool asymmetry that gave OBrien Cinema's CLS query
    its 'matches anywhere' character."""
    img = Image.open(crop_path).convert("RGB")
    new_w = target_grid_w * PATCH
    new_h = target_grid_h * PATCH
    img_resized = img.resize((new_w, new_h), Image.BILINEAR)
    transform = T.Compose([
        T.ToTensor(),
        T.Normalize(mean=NORM_MEAN, std=NORM_STD),
    ])
    x = transform(img_resized).unsqueeze(0).to(device)
    with torch.no_grad():
        out = model(x, interpolate_pos_encoding=True)
    # last_hidden_state[0, 0] is CLS (skip); 1: are the patch features.
    patches = out.last_hidden_state[0, 1:]   # (target_grid_h * target_grid_w, D)
    pooled = patches.mean(dim=0)
    pooled = torch.nn.functional.normalize(pooled, dim=-1)
    return pooled.cpu().float().numpy()


def slide_match(grid: np.ndarray, query: np.ndarray, win_h: int, win_w: int,
                stride: int, threshold: float,
                valid_gx: list[int] | None = None,
                valid_gy: list[int] | None = None
                ) -> list[tuple[int, int, float]]:
    """Slide a (win_h, win_w) window across the patch grid, mean-pool
    patches inside, L2-normalise, dot with `query`. Return all
    (gy, gx, sim) above `threshold`. Uses a 2-D integral image so each
    window mean is O(D) regardless of window size.

    `valid_gx`: restrict x positions tested (column-aligned matching:
    ads start at column boundaries, not at arbitrary x).
    `valid_gy`: restrict y positions tested (e.g. fix y to the ink-top
    of the page so we test 'is this ad at the top of column N?')."""
    grid_h, grid_w, D = grid.shape
    if win_h > grid_h or win_w > grid_w:
        return []
    # Integral image of patch features: cumsum[y,x] = sum of patches in
    # the rectangle [0:y, 0:x). Window sum is then a 4-corner subtract.
    cumsum = np.zeros((grid_h + 1, grid_w + 1, D), dtype=np.float32)
    cumsum[1:, 1:] = np.cumsum(np.cumsum(grid, axis=0), axis=1)
    win_area = float(win_h * win_w)
    hits: list[tuple[int, int, float]] = []
    if valid_gx is None:
        gx_list = list(range(0, grid_w - win_w + 1, stride))
    else:
        gx_list = [gx for gx in valid_gx if 0 <= gx <= grid_w - win_w]
    if valid_gy is None:
        gy_list = list(range(0, grid_h - win_h + 1, stride))
    else:
        gy_list = [gy for gy in valid_gy if 0 <= gy <= grid_h - win_h]
    for gy in gy_list:
        for gx in gx_list:
            s = (cumsum[gy + win_h, gx + win_w]
                 - cumsum[gy, gx + win_w]
                 - cumsum[gy + win_h, gx]
                 + cumsum[gy, gx])
            mean = s / win_area
            n = float(np.linalg.norm(mean))
            if n < 1e-9:
                continue
            sim = float(np.dot(mean / n, query))
            if sim >= threshold:
                hits.append((gy, gx, sim))
    return hits


def nms(hits: list[tuple[int, int, float]], win_h: int, win_w: int,
        iou: float = NMS_IOU) -> list[tuple[int, int, float]]:
    """Greedy NMS: walk hits high-sim → low, drop any overlapping the
    kept set by IoU > `iou`. Window size is fixed across hits, so the
    union simplifies to 2*A - inter."""
    hits = sorted(hits, key=lambda h: -h[2])
    kept: list[tuple[int, int, float]] = []
    win_area = win_h * win_w
    for gy, gx, sim in hits:
        clash = False
        for ky, kx, _ in kept:
            ih = max(0, min(gy + win_h, ky + win_h) - max(gy, ky))
            iw = max(0, min(gx + win_w, kx + win_w) - max(gx, kx))
            inter = ih * iw
            if inter and inter / (2 * win_area - inter) > iou:
                clash = True
                break
        if not clash:
            kept.append((gy, gx, sim))
    return kept


def load_catalogue(grid_w: int, grid_h: int) -> list[dict]:
    """Read labelled clusters from recurrence.db, pair each with its
    exemplar embedding from embeddings.npz, and derive an integer
    (win_h, win_w) in patch-grid units from `detected_ads.{w_pct, h_pct}`
    in the main MVTM DB (read-only).

    NB the crop PNGs in `columns/<issue>/ads/p<N>/*.png` are stored at
    a higher resolution than `page_raw.png` — using their pixel size to
    derive a window scale silently mis-sizes every query (Star Theatre's
    bbox is 20% wide on the page but the crop PNG is 62% wide of
    page_raw, off by ~3x). pct is the canonical, dimension-agnostic
    quantity per the project's `coordinates.py` rule."""
    import sqlite3
    with INDEX_PATH.open() as f:
        ads_idx = json.load(f)
    path_to_row = {r["path"]: i for i, r in enumerate(ads_idx)}
    embs = np.load(EMB_PATH)["embs"].astype(np.float32)

    main_db = LAB_DIR.parent / "data" / "mvtm.db"
    main = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    main.row_factory = sqlite3.Row

    conn = open_db(create=False)
    rows = list(conn.execute(
        "SELECT cluster_id, exemplar_path, category, name, size FROM clusters "
        "WHERE category IN ('ad', 'body_text_fp', 'furniture') "
        "ORDER BY size DESC"
    ))
    catalogue: list[dict] = []
    skipped_no_emb = 0
    skipped_no_bbox = 0
    skipped_too_big = 0
    for r in rows:
        path = r["exemplar_path"]
        if path not in path_to_row:
            skipped_no_emb += 1
            continue
        fn = path.split("/")[-1]
        bbox = main.execute(
            "SELECT w_pct, h_pct FROM detected_ads WHERE image_filename=?",
            (fn,)
        ).fetchone()
        if bbox is None:
            skipped_no_bbox += 1
            continue
        # pct → grid units. Clamp to >= 1 patch.
        win_w = max(1, int(round(bbox["w_pct"] / 100.0 * grid_w)))
        win_h = max(1, int(round(bbox["h_pct"] / 100.0 * grid_h)))
        if win_w > grid_w or win_h > grid_h:
            skipped_too_big += 1
            continue
        catalogue.append({
            "cluster_id": r["cluster_id"],
            "exemplar_path": path,
            "category": r["category"],
            "name": r["name"],
            "size": r["size"],
            "emb": embs[path_to_row[path]],
            "win_h": win_h,
            "win_w": win_w,
            "w_pct": bbox["w_pct"],
            "h_pct": bbox["h_pct"],
        })
    if skipped_no_emb or skipped_no_bbox or skipped_too_big:
        print(f"catalogue skips: missing_emb={skipped_no_emb} "
              f"missing_bbox={skipped_no_bbox} window>grid={skipped_too_big}",
              file=sys.stderr)
    return catalogue


def page_columns(issue_dir: str, page: int) -> tuple[list[float], list[float]] | None:
    """Read this page's column geometry from the main DB. Returns
    (left_edges_pct, column_widths_pct) where left_edges_pct is the
    valid set of left-aligned start positions (boundary_positions
    minus the final rightmost edge — that one is the rightmost column's
    right edge, not a valid start). Returns None if no row exists."""
    import sqlite3
    y, m, d = issue_dir.split("-")
    main_db = LAB_DIR.parent / "data" / "mvtm.db"
    conn = sqlite3.connect(f"file:{main_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT boundary_positions, column_widths FROM page_layouts "
        "WHERE year=? AND month=? AND day=? AND page=?",
        (int(y), int(m), int(d), page),
    ).fetchone()
    if row is None or not row["boundary_positions"]:
        return None
    boundaries = json.loads(row["boundary_positions"])
    widths = json.loads(row["column_widths"]) if row["column_widths"] else []
    if len(boundaries) < 2:
        return None
    return list(boundaries[:-1]), list(widths)


def render_overlay(img: Image.Image, hits_per_cluster: list[dict],
                   page_w: int, page_h: int, grid_w: int, grid_h: int,
                   out_path: Path) -> None:
    """Draw blue rects for ad/furniture hits, orange for body_text_fp.
    Width-3 outline + a small label so overlapping hits are still legible."""
    overlay = img.copy().convert("RGB")
    draw = ImageDraw.Draw(overlay)
    for entry in hits_per_cluster:
        cl = entry["cluster"]
        col = "#2266dd" if cl["category"] in ("ad", "furniture") else "#b35900"
        for gy, gx, sim in entry["hits"]:
            x0 = int(round(gx / grid_w * page_w))
            y0 = int(round(gy / grid_h * page_h))
            x1 = int(round((gx + cl["win_w"]) / grid_w * page_w))
            y1 = int(round((gy + cl["win_h"]) / grid_h * page_h))
            draw.rectangle([x0, y0, x1, y1], outline=col, width=3)
            label = f"{cl['name'] or cl['category']} {sim:.2f}"
            draw.text((x0 + 4, y0 + 4), label, fill=col)
    overlay.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", required=True,
                    help="Single page: <issue_dir>:<page>, e.g. 1947-09-18:2")
    ap.add_argument("--device", default="mps", choices=("mps", "cpu", "cuda"))
    ap.add_argument("--threshold", type=float, default=0.50,
                    help="Cosine sim threshold for a candidate match (pre-NMS)")
    ap.add_argument("--stride", type=int, default=1,
                    help="Sliding-window stride in patch units (1 = dense)")
    ap.add_argument("--render", action="store_true",
                    help="Write verification overlay PNG to /tmp")
    ap.add_argument("--top", type=int, default=30,
                    help="Print only the top-N hits across all clusters")
    ap.add_argument("--only-name", default=None,
                    help="Only run clusters whose name matches this string "
                         "(case-insensitive substring). Useful for "
                         "single-cluster overlays.")
    ap.add_argument("--only-category", default=None,
                    choices=("ad", "body_text_fp", "furniture"),
                    help="Only run clusters of this category.")
    ap.add_argument("--column-aligned", action="store_true",
                    help="Restrict window x positions to column left edges "
                         "from page_layouts.boundary_positions. Ads sit on "
                         "column boundaries; this prunes implausible x.")
    ap.add_argument("--y-pct", type=float, default=None,
                    help="Restrict window y positions to the single given "
                         "pct (top of window). Useful when we know the "
                         "page's ink-top: only test 'is this ad at the top "
                         "of a column?'. Combine with --column-aligned for "
                         "a row of corner-aligned candidates.")
    ap.add_argument("--n-cols", type=int, default=None,
                    help="Override window WIDTH for all active clusters to "
                         "N columns wide using the smoke page's "
                         "column_widths. Works around scale variation "
                         "between scans: 'two columns wide' is anchored to "
                         "the page's geometry; pct of bbox is not.")
    ap.add_argument("--scale", type=float, default=HALF_SCALE,
                    help="Page resize scale before DINO forward. 0.5 (half) "
                         "is cheap and coarse; 1.0 (full) is 4x the GPU "
                         "cost but gives a 88x125 patch grid (1.13%% x in, "
                         "0.81%% y in patch density), better matching the "
                         "patch density of cropped exemplars.")
    ap.add_argument("--mean-pool-query", action="store_true",
                    help="Replace each cluster's CLS-token query embedding "
                         "(from embeddings.npz) with a mean-pool over the "
                         "exemplar crop's own patches at the same grid "
                         "shape as the candidate window. Restores "
                         "aggregation symmetry between query and "
                         "candidate.")
    args = ap.parse_args()

    issue_dir, page_str = args.smoke.split(":")
    page = int(page_str)
    page_path = COLUMNS_DIR / issue_dir / f"p{page}" / "page_raw.png"
    if not page_path.exists():
        raise SystemExit(f"page not found: {page_path}")

    img = Image.open(page_path).convert("RGB")
    page_w, page_h = img.size
    print(f"page: {page_path.relative_to(LAB_DIR.parent)} ({page_w}x{page_h})")

    new_w, new_h = scaled_dims(page_w, page_h, args.scale)
    grid_w = new_w // PATCH
    grid_h = new_h // PATCH
    print(f"scaled input ({args.scale:.2f}x): {new_w}x{new_h}; "
          f"patch grid: {grid_h}x{grid_w}")

    catalogue = load_catalogue(grid_w, grid_h)
    if args.only_category:
        catalogue = [c for c in catalogue if c["category"] == args.only_category]
    if args.only_name:
        needle = args.only_name.lower()
        catalogue = [c for c in catalogue
                     if c["name"] and needle in c["name"].lower()]
    print(f"catalogue: {len(catalogue)} labelled clusters loaded")
    by_cat: dict[str, int] = {}
    for cl in catalogue:
        by_cat[cl["category"]] = by_cat.get(cl["category"], 0) + 1
    for k in sorted(by_cat):
        print(f"  {k:14} {by_cat[k]:3d}")

    device = pick_device(args.device)
    print(f"loading {MODEL_NAME} on {device} ...")
    model = AutoModel.from_pretrained(MODEL_NAME).to(device).eval()

    import time
    t0 = time.time()
    grid = page_to_patch_grid(img, model, device, scale=args.scale)
    t_fwd = time.time() - t0
    print(f"forward pass: {t_fwd:.2f}s; grid shape={grid.shape}")

    if args.mean_pool_query:
        t0 = time.time()
        for cl in catalogue:
            crop_path = LAB_DIR.parent / cl["exemplar_path"]
            cl["emb"] = mean_pool_exemplar_embedding(
                crop_path, cl["win_h"], cl["win_w"], model, device,
            )
        print(f"mean-pool query embeddings: {time.time()-t0:.2f}s for "
              f"{len(catalogue)} exemplars")

    valid_gy_template: list[int] | None = None
    if args.y_pct is not None:
        gy = int(round(args.y_pct / 100.0 * grid_h))
        valid_gy_template = [gy]
        print(f"y-pct constraint: gy={gy} ({args.y_pct:.1f}% of page)")

    cols_data = page_columns(issue_dir, page)

    valid_gx_template: list[int] | None = None
    if args.column_aligned:
        if cols_data is None:
            print(f"--column-aligned requested but no page_layouts row for "
                  f"{issue_dir} p{page}; falling back to dense slide",
                  file=sys.stderr)
        else:
            edges_pct, _ = cols_data
            valid_gx_template = [int(round(e / 100.0 * grid_w))
                                 for e in edges_pct]
            print(f"column-aligned: {len(valid_gx_template)} candidate left "
                  f"edges at gx={valid_gx_template} "
                  f"(pct={[round(e,1) for e in edges_pct]})")

    if args.n_cols is not None:
        if cols_data is None:
            print(f"--n-cols requested but no page_layouts row; ignored",
                  file=sys.stderr)
        else:
            _, widths_pct = cols_data
            if not widths_pct:
                print("--n-cols requested but column_widths empty; ignored",
                      file=sys.stderr)
            else:
                # Standard column = the modal/typical width. Use mean
                # since column_widths are already uniform per page.
                std_col_pct = sum(widths_pct) / len(widths_pct)
                override_w_pct = args.n_cols * std_col_pct
                override_win_w = max(1, int(round(override_w_pct / 100.0
                                                  * grid_w)))
                print(f"--n-cols={args.n_cols}: standard col width "
                      f"{std_col_pct:.2f}%, override w_pct="
                      f"{override_w_pct:.2f}%, win_w={override_win_w} patches")
                for cl in catalogue:
                    cl["win_w"] = override_win_w
                    cl["w_pct"] = override_w_pct

    t0 = time.time()
    all_hits: list[tuple[float, dict, int, int]] = []  # (sim, cluster, gy, gx)
    rendered: list[dict] = []
    for cl in catalogue:
        raw = slide_match(grid, cl["emb"], cl["win_h"], cl["win_w"],
                          stride=args.stride, threshold=args.threshold,
                          valid_gx=valid_gx_template,
                          valid_gy=valid_gy_template)
        if not raw:
            continue
        kept = nms(raw, cl["win_h"], cl["win_w"])
        if not kept:
            continue
        for gy, gx, sim in kept:
            all_hits.append((sim, cl, gy, gx))
        rendered.append({"cluster": cl, "hits": kept})
    t_match = time.time() - t0
    print(f"matching: {t_match:.2f}s across {len(catalogue)} clusters; "
          f"{len(all_hits)} NMS-passed hits")

    all_hits.sort(key=lambda x: -x[0])
    print(f"\ntop {min(args.top, len(all_hits))} hits:")
    for sim, cl, gy, gx in all_hits[:args.top]:
        x_pct = round(gx / grid_w * 100, 1)
        y_pct = round(gy / grid_h * 100, 1)
        w_pct = round(cl["win_w"] / grid_w * 100, 1)
        h_pct = round(cl["win_h"] / grid_h * 100, 1)
        nm = (cl["name"] or "")[:36]
        print(f"  sim={sim:.3f}  [{cl['category']:12}] {nm!r:38} "
              f"@({x_pct:5.1f}%,{y_pct:5.1f}%) {w_pct:.1f}%x{h_pct:.1f}%")

    if args.render:
        suffix_parts = []
        if args.only_category:
            suffix_parts.append(args.only_category)
        if args.only_name:
            slug = "".join(c if c.isalnum() else "_"
                           for c in args.only_name.lower())[:40]
            suffix_parts.append(slug)
        if args.column_aligned:
            suffix_parts.append("colaligned")
        if args.y_pct is not None:
            suffix_parts.append(f"y{args.y_pct:.1f}".replace(".", "p"))
        if args.n_cols is not None:
            suffix_parts.append(f"{args.n_cols}col")
        if args.scale != HALF_SCALE:
            suffix_parts.append(f"s{args.scale:.2f}".replace(".", "p"))
        if args.mean_pool_query:
            suffix_parts.append("mpq")
        suffix = ("_" + "_".join(suffix_parts)) if suffix_parts else ""
        out = Path(f"/tmp/page_features_{issue_dir}_p{page}{suffix}.png")
        render_overlay(img, rendered, page_w, page_h, grid_w, grid_h, out)
        print(f"\nrendered overlay → {out}")


if __name__ == "__main__":
    main()
