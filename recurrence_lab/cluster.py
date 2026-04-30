"""Cluster ad embeddings on cosine similarity.

Loads embeddings.npz + ads_index.json, computes pairwise similarity
(efficient because embeddings are L2-normalised — cosine == dot
product), and groups ads via single-linkage at a similarity threshold.

Output:
    clusters.json — list of clusters sorted by size desc:
        {
          "cluster_id": int,
          "size": int,
          "first_date": "YYYY-MM-DD",
          "last_date":  "YYYY-MM-DD",
          "n_issues":   int,
          "exemplar":   {issue_dir, page, file, path},
          "members":    [{issue_dir, page, file, path, year, month, day}, ...]
        }

Singletons (clusters of size 1) are emitted last and can be filtered
in the viewer.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

LAB_DIR = Path(__file__).resolve().parent
INDEX_PATH = LAB_DIR / "ads_index.json"
EMB_PATH = LAB_DIR / "embeddings.npz"
OUT_PATH = LAB_DIR / "clusters.json"


def load() -> tuple[list[dict], np.ndarray]:
    if not INDEX_PATH.exists() or not EMB_PATH.exists():
        sys.exit(f"missing {INDEX_PATH} or {EMB_PATH}; run embed.py first")
    with INDEX_PATH.open() as f:
        idx = json.load(f)
    embs = np.load(EMB_PATH)["embs"]
    if len(idx) != embs.shape[0]:
        sys.exit(f"index/embedding desync: {len(idx)} vs {embs.shape[0]}")
    return idx, embs


def union_find_clusters(embs: np.ndarray, threshold: float,
                        block: int = 512) -> np.ndarray:
    """Single-linkage clustering by cosine similarity ≥ threshold.

    Computes similarity in row blocks to avoid materialising the full
    N×N matrix when N is large. For each pair (i, j) with i < j and
    sim ≥ threshold, union their components.
    """
    n = embs.shape[0]
    parent = np.arange(n, dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Union by index (lower wins) — keeps cluster ids deterministic
        # and biased toward earliest-discovered representatives.
        if ra < rb:
            parent[rb] = ra
        else:
            parent[ra] = rb

    t0 = time.time()
    pairs_found = 0
    for i in range(0, n, block):
        i_end = min(n, i + block)
        block_embs = embs[i:i_end]                       # (b, D)
        # similarity vs all rows j >= i — upper triangle only
        sim = block_embs @ embs[i:].T                    # (b, n - i)
        # Mask self and below-diagonal (we only walk j > local row)
        for r in range(sim.shape[0]):
            row_global = i + r
            local_offset = r + 1   # j must be > row_global, i.e., col_local > r
            row = sim[r, local_offset:]
            hits = np.where(row >= threshold)[0]
            for h in hits:
                j_global = row_global + 1 + h
                union(row_global, j_global)
                pairs_found += 1
    elapsed = time.time() - t0
    print(f"pairs ≥ {threshold}: {pairs_found} (in {elapsed:.1f}s)")

    # Final pass: flatten parent array to representative of each tree
    return np.array([find(i) for i in range(n)], dtype=np.int64)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=0.98,
                    help="Cosine similarity threshold for cluster membership. "
                         "0.98 is the empirical baseline across 1946-1948: "
                         "tight enough that single-linkage transitivity does "
                         "not chain disparate ads through the body-text FP "
                         "amalgam. Lowering this towards 0.85 collapses ~80% "
                         "of the corpus into one mega-cluster.")
    ap.add_argument("--min-size", type=int, default=1,
                    help="Drop clusters smaller than this from output")
    args = ap.parse_args()

    idx, embs = load()
    print(f"loaded {len(idx)} ads, dim={embs.shape[1]}")

    labels = union_find_clusters(embs, args.threshold)
    by_cluster: dict[int, list[int]] = defaultdict(list)
    for i, c in enumerate(labels):
        by_cluster[int(c)].append(i)

    clusters_out = []
    # Sort clusters by size desc, then by earliest member's issue_dir
    cluster_keys = sorted(by_cluster.items(),
                          key=lambda kv: (-len(kv[1]), idx[kv[1][0]]["issue_dir"]))

    next_id = 0
    for _, members_idx in cluster_keys:
        if len(members_idx) < args.min_size:
            continue
        members = [idx[i] for i in members_idx]
        # Exemplar: member closest to the cluster centroid.
        # `sims` (cosine to centroid) is also persisted per member so
        # downstream consumers (cluster_membership.py → recurrence.db)
        # don't have to re-derive from embeddings.npz.
        if len(members_idx) == 1:
            exemplar = members[0]
            sims = np.array([1.0])
        else:
            cluster_embs = embs[members_idx]
            centroid = cluster_embs.mean(axis=0)
            centroid = centroid / (np.linalg.norm(centroid) + 1e-12)
            sims = cluster_embs @ centroid
            exemplar = members[int(np.argmax(sims))]

        dates = sorted({(m["year"], m["month"], m["day"]) for m in members})
        first = dates[0]
        last = dates[-1]
        fmt = lambda t: f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}"
        clusters_out.append({
            "cluster_id": next_id,
            "size": len(members),
            "n_issues": len(dates),
            "first_date": fmt(first),
            "last_date": fmt(last),
            "exemplar": {k: exemplar[k] for k in ("issue_dir", "page", "file", "path")},
            "members": [
                {**{k: m[k] for k in ("issue_dir", "year", "month", "day", "page", "file", "path")},
                 "similarity": round(float(sims[i]), 4)}
                for i, m in enumerate(members)
            ],
        })
        next_id += 1

    summary = {
        "n_ads": len(idx),
        "n_clusters": len(clusters_out),
        "n_recurring": sum(1 for c in clusters_out if c["size"] > 1),
        "threshold": args.threshold,
        "clusters": clusters_out,
    }

    tmp = OUT_PATH.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(summary, f, indent=1)
    tmp.replace(OUT_PATH)
    print(f"wrote {OUT_PATH}")
    print(f"  n_ads={summary['n_ads']}  n_clusters={summary['n_clusters']}  "
          f"n_recurring={summary['n_recurring']}")
    if clusters_out:
        biggest = clusters_out[0]
        print(f"  biggest cluster: size={biggest['size']}, "
              f"{biggest['first_date']} → {biggest['last_date']}, "
              f"exemplar {biggest['exemplar']['path']}")


if __name__ == "__main__":
    main()
