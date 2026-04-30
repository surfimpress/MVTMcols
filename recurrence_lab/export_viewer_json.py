"""Write trimmed JSON snapshots from `recurrence.db` for the static viewer.

The viewer (`viewer.html`) is a static page; it never opens the DB
directly. This script flattens the relevant join into a small,
already-filtered JSON file.

Phase 1 output: `snapshots/clusters_table.json`
  — recurring clusters (size > 1) with exemplar, member list, and
    current applied category/name from the DB. The viewer's Triage
    panel reads this on load and overlays localStorage edits.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from db import open_db

LAB_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = LAB_DIR / "snapshots"


def export_clusters_table(min_size: int = 2) -> dict:
    """Return the JSON-serialisable structure for clusters_table.json."""
    conn = open_db(create=False)

    cluster_rows = list(conn.execute(
        "SELECT cluster_id, size, n_issues, first_date, last_date, "
        "       exemplar_path, category, name, notes, labelled_at "
        "FROM clusters "
        "WHERE size >= ? "
        "ORDER BY size DESC, first_date ASC",
        (min_size,),
    ))

    member_rows = list(conn.execute(
        "SELECT m.cluster_id, m.image_filename, m.issue_dir, m.page, "
        "       m.similarity, m.rejected "
        "FROM cluster_membership m "
        "JOIN clusters c ON c.cluster_id = m.cluster_id "
        "WHERE c.size >= ? "
        "ORDER BY m.cluster_id, m.similarity DESC",
        (min_size,),
    ))

    members_by_cid: dict[int, list] = defaultdict(list)
    rejected_by_cid: dict[int, int] = defaultdict(int)
    for r in member_rows:
        members_by_cid[r["cluster_id"]].append({
            "image_filename": r["image_filename"],
            "issue_dir": r["issue_dir"],
            "page": r["page"],
            # Path is reconstructed for the viewer; the full filesystem
            # path lives in clusters.json, but for the snapshot we keep
            # only what the viewer needs for thumbs.
            "path": f"columns/{r['issue_dir']}/ads/p{r['page']}/{r['image_filename']}",
            "similarity": round(r["similarity"], 4),
            "rejected": int(r["rejected"]),
        })
        if r["rejected"]:
            rejected_by_cid[r["cluster_id"]] += 1

    clusters_out = []
    for c in cluster_rows:
        clusters_out.append({
            "cluster_id": c["cluster_id"],
            "size": c["size"],
            "n_issues": c["n_issues"],
            "first_date": c["first_date"],
            "last_date": c["last_date"],
            "exemplar_path": c["exemplar_path"],
            "category": c["category"],
            "name": c["name"],
            "notes": c["notes"],
            "labelled_at": c["labelled_at"],
            "rejected_count": rejected_by_cid[c["cluster_id"]],
            "members": members_by_cid[c["cluster_id"]],
        })

    # Headline counts across the full cluster table (not just size >= min)
    totals = conn.execute(
        "SELECT COUNT(*) AS n_clusters, COALESCE(SUM(size), 0) AS n_ads "
        "FROM clusters"
    ).fetchone()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "min_size": min_size,
        "n_clusters_total": totals["n_clusters"],
        "n_ads_total": totals["n_ads"],
        "n_recurring": len(clusters_out),
        "clusters": clusters_out,
    }


def main() -> None:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    payload = export_clusters_table(min_size=2)
    out_path = SNAPSHOT_DIR / "clusters_table.json"
    tmp = out_path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=1)
    tmp.replace(out_path)
    size_kb = out_path.stat().st_size / 1024
    print(f"wrote {out_path} ({size_kb:.1f} KB)")
    print(f"  recurring clusters: {payload['n_recurring']}")
    print(f"  total ads: {payload['n_ads_total']}")
    print(f"  total clusters (incl. singletons): {payload['n_clusters_total']}")


if __name__ == "__main__":
    main()
