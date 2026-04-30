"""Populate `clusters` + `cluster_membership` from `clusters.json`.

Every re-run of `cluster.py` produces a fresh `clusters.json` with new
cluster_ids. This script syncs that snapshot into `recurrence.db` while
**preserving applied labels** (`category`, `name`, `notes`,
`labelled_at`) by joining to existing rows on `exemplar_path`.

If an exemplar still appears in the new cluster_set, its labels follow
it to the new cluster_id. If an exemplar drops out (unlikely — exemplar
choice is centroid-based and stable across small perturbations), the
label is dropped silently. That's acceptable for a spike: the YAML
export is the durable record of triage.

Idempotent. Run after every `cluster.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

from db import open_db

LAB_DIR = Path(__file__).resolve().parent
CLUSTERS_JSON = LAB_DIR / "clusters.json"


def main() -> None:
    if not CLUSTERS_JSON.exists():
        raise SystemExit(
            f"{CLUSTERS_JSON} not found — run `python cluster.py` first."
        )
    with CLUSTERS_JSON.open() as f:
        snapshot = json.load(f)

    clusters = snapshot["clusters"]
    print(f"loaded clusters.json: n_ads={snapshot['n_ads']} "
          f"n_clusters={snapshot['n_clusters']} "
          f"n_recurring={snapshot['n_recurring']} "
          f"threshold={snapshot['threshold']}")

    conn = open_db()

    # 1a. Snapshot any existing labels keyed by exemplar_path.
    existing: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT exemplar_path, category, name, notes, labelled_at "
        "FROM clusters WHERE category != 'unclassified' OR name IS NOT NULL"
    ):
        existing[row["exemplar_path"]] = {
            "category": row["category"],
            "name": row["name"],
            "notes": row["notes"],
            "labelled_at": row["labelled_at"],
        }
    if existing:
        print(f"preserving {len(existing)} applied label(s) by exemplar_path")

    # 1b. Snapshot rejected member flags keyed by image_filename. Filenames
    # are globally unique across the corpus, so the flag follows the ad
    # to whatever new cluster it lands in after re-clustering.
    rejected: set[str] = {
        row["image_filename"]
        for row in conn.execute(
            "SELECT image_filename FROM cluster_membership WHERE rejected = 1"
        )
    }
    if rejected:
        print(f"preserving {len(rejected)} rejected-member flag(s) by image_filename")

    # 2. Wipe both tables. Cluster IDs renumber on every cluster.py run,
    # so a clean re-insert is simpler and safer than UPSERT-by-id.
    with conn:
        conn.execute("DELETE FROM cluster_membership")
        conn.execute("DELETE FROM clusters")

    # 3. Re-insert clusters (carrying preserved labels) + memberships.
    cluster_rows = []
    member_rows = []
    for c in clusters:
        exemplar_path = c["exemplar"]["path"]
        label = existing.get(exemplar_path, {})
        cluster_rows.append((
            c["cluster_id"],
            c["size"],
            c["n_issues"],
            c["first_date"],
            c["last_date"],
            exemplar_path,
            label.get("category", "unclassified"),
            label.get("name"),
            label.get("notes"),
            label.get("labelled_at"),
        ))
        for m in c["members"]:
            sim = m.get("similarity", m.get("sim", 1.0))
            member_rows.append((
                m["file"],
                m["issue_dir"],
                m["page"],
                c["cluster_id"],
                float(sim),
                1 if m["file"] in rejected else 0,
            ))

    with conn:
        conn.executemany(
            "INSERT INTO clusters "
            "(cluster_id, size, n_issues, first_date, last_date, "
            " exemplar_path, category, name, notes, labelled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            cluster_rows,
        )
        conn.executemany(
            "INSERT INTO cluster_membership "
            "(image_filename, issue_dir, page, cluster_id, similarity, rejected) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            member_rows,
        )

    print(f"wrote {len(cluster_rows)} clusters, {len(member_rows)} members "
          f"to {conn.execute('PRAGMA database_list').fetchone()['file']}")

    # Sanity: SUM(size) should equal cluster_membership row count.
    sum_size = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS s FROM clusters"
    ).fetchone()["s"]
    n_members = conn.execute(
        "SELECT COUNT(*) AS n FROM cluster_membership"
    ).fetchone()["n"]
    if sum_size != n_members:
        raise SystemExit(
            f"sanity-check failed: SUM(clusters.size)={sum_size} but "
            f"cluster_membership row count={n_members}"
        )
    print(f"sanity ok: SUM(size) == cluster_membership rows ({n_members})")


if __name__ == "__main__":
    main()
