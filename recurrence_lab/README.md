# recurrence_lab — repeated-element discovery via DINOv2 embeddings

Standalone spike. **Does not import from or write to the main MVTM
pipeline**. Reads ad crops from `../columns/{issue}/ads/p{N}/*.png` as
a read-only input. All outputs land in this folder.

## What it does

1. Walk a configurable slice of the corpus (default: year 1947).
2. Embed every ad crop with DINOv2 (ViT-S/14, 384-d).
3. Cluster on cosine similarity to find ads that recur across issues.
4. Persist clusters + applied triage in `recurrence.db`.
5. Triage in-browser via `viewer.html`; export `cluster_labels.yaml`
   and apply with `apply_labels.py`.

## Setup (one-time)

    cd recurrence_lab
    bash setup_venv.sh

This creates `./venv/` and installs torch + transformers + scikit-learn
+ Pillow + PyYAML inside it. The main app's Python env is untouched.

## Run

    source venv/bin/activate
    python embed.py --years 1946 1947 1948         # → embeddings.npz, ads_index.json
    python cluster.py                              # → clusters.json (default threshold 0.98)
    python cluster_membership.py                   # → recurrence.db (clusters + cluster_membership)
    python export_viewer_json.py                   # → snapshots/clusters_table.json
    python -m http.server 8765                     # → http://localhost:8765/viewer.html

`embed.py` is incremental: re-running with a wider `--years` list will
embed only what's new (keyed by issue_dir + filename + mtime).

`cluster.py`'s default threshold is **0.98**. Single-linkage union-find
chains aggressively at lower thresholds — at 0.85 the 1946–48 corpus
collapsed into one mega-cluster of ~5,800 members.

## Triage round-trip

The viewer's "Triage" panel exposes a category dropdown
(`unclassified`, `ad`, `body_text_fp`, `furniture`), a free-text name,
and per-thumb ✕ buttons to reject members that don't fit the cluster.
Edits are stored in `localStorage` until exported.

1. Triage in `viewer.html`. Click ✕ on stragglers; set category and
   name. The dirty-edit count appears in the header.
2. Click **Export labels.yaml**. Save the download as
   `recurrence_lab/cluster_labels.yaml` (overwriting the previous).
3. `python apply_labels.py` joins each entry to the DB by
   `exemplar_path`, writes category/name/notes, and applies
   `reject_members` with authoritative-overwrite semantics (anything
   not listed gets un-rejected). Then refreshes the snapshot.

**Merging clusters via name**: give two clusters the same `name` (and
same `category`) and they're treated as one logical cluster — the
viewer shows a "merged ×N → total ×T" badge, and the
`merged_clusters` SQL view in `recurrence.db` aggregates them. Same
name across different categories is a hard error in `apply_labels.py`.

**Stable across re-clusterings**: every `cluster.py` run renumbers
cluster_ids. Labels and rejected-member flags survive the renumber
because they're keyed on `exemplar_path` (UNIQUE) and `image_filename`
(globally unique) respectively.

## Data layout

| File | Producer | Content |
|---|---|---|
| `ads_index.json` | embed.py | one row per ad: issue_dir, page, filename, year/month/day |
| `embeddings.npz` | embed.py | `embs`: float32 (N, 384), L2-normalised |
| `clusters.json` | cluster.py | clusters with member indices into ads_index, exemplar, date range |
| `recurrence.db` | cluster_membership.py / apply_labels.py | authoritative store; gitignored |
| `cluster_labels.yaml` | viewer.html (export) | git-tracked triage record |
| `snapshots/*.json` | export_viewer_json.py | trimmed snapshots for the static viewer |
| `viewer.html` | (static) | browse + triage clusters in a browser |

## Knobs

- `--years` (embed) — comma- or space-separated list of years to ingest.
- `--threshold` (cluster) — cosine similarity. Default 0.98. Above 0.99
  drops most genuine recurrences; below 0.95 starts chaining unrelated ads.
- `--device` (embed) — `mps` (default on Apple Silicon), `cpu`, or `cuda`.

## Status

Spike. Phase 1 (cropped-ad clustering + triage round-trip) is what's
live: clusters, membership, applied labels, viewer. No commitment to
productionise yet.

Page-level matching (proposed Phases 2–4 of the original plan — running
DINOv2 over full pages to find missed ads / FP-flag known clusters /
discover novel recurrences) was explored on 2026-04-30 and **dropped**.
Per-window DINOv2 features couldn't separate similar-shape ads cleanly
enough (Star Theatre 0.978 vs Almonte Garage 0.953 on the same column-
aligned candidate). See `archive/README.md` for the full empirical
record and what might be worth trying next (likely a retrieval-trained
model, e.g. CLIP/SigLIP) if the question is ever revisited.
