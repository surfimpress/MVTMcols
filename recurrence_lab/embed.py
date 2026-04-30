"""Embed every ad crop in the configured year(s) with DINOv2.

Output:
    ads_index.json — one entry per ad, in row order matching embeddings.npz
    embeddings.npz — float32 (N, D), L2-normalised; key "embs"

Incremental: if both files exist, ads already present (matched by
issue_dir + filename + mtime) are kept and only new files are embedded.

Reads from ../columns/ as read-only. Writes only inside this folder.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image
from tqdm import tqdm
from transformers import AutoImageProcessor, AutoModel

LAB_DIR = Path(__file__).resolve().parent
COLUMNS_DIR = LAB_DIR.parent / "columns"
INDEX_PATH = LAB_DIR / "ads_index.json"
EMB_PATH = LAB_DIR / "embeddings.npz"

MODEL_NAME = "facebook/dinov2-small"  # 384-d, ~22M params — fast on CPU/MPS


def discover_ads(years: list[int]) -> list[dict]:
    """Walk columns/{YYYY-MM-DD}/ads/p{N}/*.png and emit metadata rows.

    Returns a list of dicts with stable keys; row order is sorted by
    (issue_dir, page, filename) so the index is deterministic between
    runs."""
    rows = []
    issue_dirs = sorted(COLUMNS_DIR.glob("*-*-*"))
    for issue_dir in issue_dirs:
        if not issue_dir.is_dir():
            continue
        try:
            y, m, d = issue_dir.name.split("-")
            year = int(y)
        except ValueError:
            continue
        if year not in years:
            continue
        ads_root = issue_dir / "ads"
        if not ads_root.is_dir():
            continue
        for page_dir in sorted(ads_root.glob("p*")):
            try:
                page = int(page_dir.name[1:])
            except ValueError:
                continue
            for png in sorted(page_dir.glob("*.png")):
                rows.append({
                    "issue_dir": issue_dir.name,
                    "year": year,
                    "month": int(m),
                    "day": int(d),
                    "page": page,
                    "file": png.name,
                    "path": str(png.relative_to(LAB_DIR.parent)),
                    "mtime": png.stat().st_mtime,
                })
    return rows


def load_existing() -> tuple[list[dict], np.ndarray | None]:
    if not INDEX_PATH.exists() or not EMB_PATH.exists():
        return [], None
    with INDEX_PATH.open() as f:
        idx = json.load(f)
    embs = np.load(EMB_PATH)["embs"]
    if len(idx) != embs.shape[0]:
        # Index/embedding desync — start over rather than guess.
        print(f"warn: index has {len(idx)} rows but embeddings has "
              f"{embs.shape[0]}; rebuilding from scratch", file=sys.stderr)
        return [], None
    return idx, embs


def pick_device(requested: str) -> torch.device:
    if requested == "mps":
        if torch.backends.mps.is_available():
            return torch.device("mps")
        print("mps not available, falling back to cpu", file=sys.stderr)
        return torch.device("cpu")
    if requested == "cuda":
        if torch.cuda.is_available():
            return torch.device("cuda")
        print("cuda not available, falling back to cpu", file=sys.stderr)
        return torch.device("cpu")
    return torch.device("cpu")


def embed_batch(images: list[Image.Image], processor, model, device) -> np.ndarray:
    """Run a batch through DINOv2 and return L2-normalised CLS embeddings."""
    inputs = processor(images=images, return_tensors="pt").to(device)
    with torch.no_grad():
        out = model(**inputs)
    # DINOv2 last_hidden_state[:, 0] is the CLS token — the standard
    # whole-image embedding for retrieval. pooler_output is also fine
    # but CLS is more commonly used in the literature.
    cls = out.last_hidden_state[:, 0]  # (B, D)
    cls = torch.nn.functional.normalize(cls, dim=-1)
    return cls.cpu().float().numpy()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, nargs="+", required=True,
                    help="Years to include, e.g. --years 1947 1948")
    ap.add_argument("--device", default="mps", choices=("mps", "cpu", "cuda"))
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0,
                    help="Max ads to embed (debug; 0 = no limit)")
    args = ap.parse_args()

    print(f"corpus: {COLUMNS_DIR}")
    print(f"years: {args.years}")
    rows = discover_ads(args.years)
    print(f"discovered: {len(rows)} ad crops")

    existing_idx, existing_embs = load_existing()
    have_key = {(r["issue_dir"], r["file"], r["mtime"]): i
                for i, r in enumerate(existing_idx)}

    # Decide which rows are new vs already-embedded.
    keep_idx = []  # indices into existing_embs to retain
    keep_meta = []
    todo = []
    for r in rows:
        key = (r["issue_dir"], r["file"], r["mtime"])
        if key in have_key:
            keep_idx.append(have_key[key])
            keep_meta.append(existing_idx[have_key[key]])
        else:
            todo.append(r)
    print(f"already embedded: {len(keep_idx)}; new: {len(todo)}")

    if args.limit and len(todo) > args.limit:
        todo = todo[:args.limit]
        print(f"--limit applied: {len(todo)} new rows will be embedded")

    if not todo and existing_embs is not None and len(keep_idx) == len(existing_idx):
        print("nothing to do.")
        return

    device = pick_device(args.device)
    print(f"loading {MODEL_NAME} on {device}...")
    processor = AutoImageProcessor.from_pretrained(MODEL_NAME)
    model = AutoModel.from_pretrained(MODEL_NAME).to(device).eval()

    new_embs = []
    t0 = time.time()
    for i in tqdm(range(0, len(todo), args.batch_size), desc="embed"):
        batch = todo[i:i + args.batch_size]
        imgs = []
        keep = []
        for r in batch:
            try:
                img = Image.open(LAB_DIR.parent / r["path"]).convert("RGB")
                imgs.append(img)
                keep.append(r)
            except Exception as e:
                print(f"skip {r['path']}: {e}", file=sys.stderr)
        if not imgs:
            continue
        embs = embed_batch(imgs, processor, model, device)
        new_embs.append(embs)
        # Replace batch with the rows that actually embedded
        todo[i:i + args.batch_size] = keep
    elapsed = time.time() - t0
    print(f"embedded {sum(e.shape[0] for e in new_embs)} ads in {elapsed:.1f}s")

    # Stitch existing-kept + newly-embedded into a single ordered set.
    parts_meta = list(keep_meta)
    parts_embs = [existing_embs[keep_idx]] if existing_embs is not None and keep_idx else []
    if new_embs:
        parts_embs.append(np.vstack(new_embs))
        parts_meta.extend([r for r in todo])
    final_embs = np.vstack(parts_embs).astype(np.float32) if parts_embs else np.zeros((0, 384), dtype=np.float32)

    # Save atomically: tmp file + rename. Pass a file object to
    # savez_compressed because the path-string overload silently
    # appends `.npz` to whatever you give it, which breaks the rename.
    tmp_idx = INDEX_PATH.with_suffix(".json.tmp")
    tmp_emb = EMB_PATH.with_suffix(".npz.tmp")
    with tmp_idx.open("w") as f:
        json.dump(parts_meta, f)
    with open(tmp_emb, "wb") as f:
        np.savez_compressed(f, embs=final_embs)
    os.replace(tmp_idx, INDEX_PATH)
    os.replace(tmp_emb, EMB_PATH)

    print(f"wrote {len(parts_meta)} rows -> {INDEX_PATH}")
    print(f"wrote embeddings shape={final_embs.shape} -> {EMB_PATH}")


if __name__ == "__main__":
    main()
