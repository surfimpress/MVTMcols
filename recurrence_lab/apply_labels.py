"""Apply labels from `cluster_labels.yaml` to `recurrence.db`.

Usage:
    python apply_labels.py [path/to/cluster_labels.yaml]

The YAML file is the durable triage record (git-tracked). The Triage
panel in `viewer.html` lets you edit categories / names in the
browser; "Export labels.yaml" downloads the full state. Save it to
`cluster_labels.yaml` and run this script to push changes into the DB.

Match key: `exemplar_path` (UNIQUE in the schema). Labels follow
exemplars across re-clusterings — the cluster_id may change, but the
exemplar tends to stay stable.

Idempotent. Auto-runs `export_viewer_json.py` at the end so the next
viewer load sees the applied state in `clusters_table.json`.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from db import open_db

LAB_DIR = Path(__file__).resolve().parent
DEFAULT_YAML = LAB_DIR / "cluster_labels.yaml"

VALID_CATEGORIES = {"unclassified", "ad", "body_text_fp", "furniture"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("yaml_path", nargs="?", default=str(DEFAULT_YAML),
                    help="Path to cluster_labels.yaml (default: ./cluster_labels.yaml)")
    args = ap.parse_args()

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        raise SystemExit(f"YAML not found: {yaml_path}")
    with yaml_path.open() as f:
        doc = yaml.safe_load(f) or {}

    labels = doc.get("labels") or []
    if not isinstance(labels, list):
        raise SystemExit("YAML root must have a `labels:` list")

    # Validate entries up-front — refuse to apply *anything* if any
    # entry is malformed. A partial apply is the worst outcome here.
    name_categories: dict[str, set[str]] = {}
    for i, entry in enumerate(labels):
        if not isinstance(entry, dict) or "exemplar" not in entry:
            raise SystemExit(f"labels[{i}] missing `exemplar:`")
        cat = entry.get("category", "unclassified")
        if cat not in VALID_CATEGORIES:
            raise SystemExit(
                f"labels[{i}] category={cat!r} not in {sorted(VALID_CATEGORIES)}"
            )
        rj = entry.get("reject_members") or []
        if not isinstance(rj, list) or any(not isinstance(x, str) for x in rj):
            raise SystemExit(
                f"labels[{i}] reject_members must be a list of strings"
            )
        # Cross-category collision check on `name`: same name across
        # different categories is treated as an error, not a merge.
        nm = (entry.get("name") or "").strip()
        if nm:
            name_categories.setdefault(nm, set()).add(cat)
    for nm, cats in name_categories.items():
        if len(cats) > 1:
            raise SystemExit(
                f"name {nm!r} appears with multiple categories {sorted(cats)} — "
                "same name across categories is treated as a merge collision; "
                "rename one or align categories before applying."
            )

    conn = open_db(create=False)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    matched = 0
    skipped: list[str] = []
    rejected_total = 0
    unrejected_total = 0
    with conn:
        for entry in labels:
            exemplar = entry["exemplar"]
            row = conn.execute(
                "SELECT cluster_id FROM clusters WHERE exemplar_path = ?",
                (exemplar,),
            ).fetchone()
            if row is None:
                skipped.append(exemplar)
                continue
            cluster_id = row["cluster_id"]
            conn.execute(
                "UPDATE clusters SET category = ?, name = ?, notes = ?, "
                "labelled_at = ? WHERE exemplar_path = ?",
                (
                    entry.get("category", "unclassified"),
                    entry.get("name"),
                    entry.get("notes"),
                    now,
                    exemplar,
                ),
            )
            # Apply rejected-member set: SET rejected=1 for files in the
            # YAML list, SET rejected=0 for everyone else in this cluster.
            # Authoritative-overwrite semantics so removing a line
            # in the YAML un-rejects.
            reject_files = list(entry.get("reject_members") or [])
            if reject_files:
                placeholders = ",".join("?" * len(reject_files))
                cur = conn.execute(
                    f"UPDATE cluster_membership SET rejected = 1 "
                    f"WHERE cluster_id = ? AND image_filename IN ({placeholders})",
                    (cluster_id, *reject_files),
                )
                rejected_total += cur.rowcount
                cur = conn.execute(
                    f"UPDATE cluster_membership SET rejected = 0 "
                    f"WHERE cluster_id = ? AND image_filename NOT IN ({placeholders}) "
                    f"AND rejected = 1",
                    (cluster_id, *reject_files),
                )
                unrejected_total += cur.rowcount
            else:
                cur = conn.execute(
                    "UPDATE cluster_membership SET rejected = 0 "
                    "WHERE cluster_id = ? AND rejected = 1",
                    (cluster_id,),
                )
                unrejected_total += cur.rowcount
            matched += 1

    print(f"applied {matched}/{len(labels)} label(s) at {now}")
    if rejected_total or unrejected_total:
        print(f"member flags: +{rejected_total} rejected, "
              f"-{unrejected_total} un-rejected")
    if skipped:
        print(f"skipped (no matching exemplar in clusters):", file=sys.stderr)
        for path in skipped:
            print(f"  {path}", file=sys.stderr)

    # Refresh the viewer snapshot so the next load sees the change.
    print("refreshing snapshots/clusters_table.json ...")
    import export_viewer_json
    export_viewer_json.main()


if __name__ == "__main__":
    main()
