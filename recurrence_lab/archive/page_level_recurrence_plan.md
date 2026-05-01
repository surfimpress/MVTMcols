# Page-level recurrence in `recurrence_lab/`

## Context

The cut-out-ad spike has worked beyond expectation. With N=1,930 ads (1947 only) × DINOv2 ViT-S/14 (CLS, L2-normalised) clustered at cosine ≥ 0.98, we already get:

- **A real positive signal** — e.g. `1947-03-19/ads/p7/1947-03-19-07_ad5.png` is correctly grouped with its other appearances, giving us a way to trace an ad through the year.
- **A real negative signal** — a 46-strong cluster anchored on `1947-09-18/ads/p2/1947-09-18-02_ad5.png` is plainly false-positive body text being mis-detected as ads (recurring section headers / standing text-blocks across editorial pages).

Today both signals act only on items that already exist in `detected_ads` — i.e. crops the OpenCV ad pipeline already chose to extract. The user's question: can we apply the same recurrence mechanism to the **original full pages** so it can (1) find ads detect_ads missed, (2) flag FP-cluster matches more reliably, and (3) discover any other recurring page-level element we never thought to look for.

Goals confirmed (multi-select): all three. Scope: **lab-only** (no writes to `data/mvtm.db`), **three years** — 1946, 1947, 1948 — chosen so cross-year archetype persistence is testable. Storage / compute strategy: **stream half-res features through a per-page pass, write a quarter-res cache to disk for cheap re-querying.** Cluster lifecycle (hot/warm/cold) is **recorded from day one but does not gate matching yet** — gating switches on later when the archetype catalogue is large enough that brute force costs.

The work stays inside `recurrence_lab/`. The spike's "does not import from or write to the main pipeline" rule still applies for *writes*; the one read-only exception this plan proposes is to import `pct_to_px` / `px_to_pct` from `/Users/peter/Projects/MVTM/coordinates.py` rather than re-deriving formulas inline, per the project CLAUDE.md "point of truth" rule. Justification: `coordinates.py` is a passive utility with no side-effects.

## Design — two tracks

### Track A — cheap, retroactive (goal 2 only)

Re-use the existing ad-crop embeddings (after Phase 0 expands them to 1946–1948); produce a per-row cluster assignment for every row in `detected_ads` that's already in `ads_index.json`.

This gives us goal (2) — FP suppression — at near-zero cost, because the 46 members of the FP body-text cluster are already enumerated in `clusters.json`. We just need to surface that as an actionable list.

**Output**: rows in `recurrence_lab/recurrence.db` (see "Storage" below) — one row per ad in `cluster_membership`, one row per cluster in `clusters` with a `category` column (default `unclassified`). Plus a thin JSON snapshot for the static viewer.

**Cost**: seconds. No new DINO inference; assignment is a single matmul on cached embeddings.

**Limitation**: cannot find anything detect_ads never proposed. Doesn't address goal (1) or (3).

### Track B — streaming page features + quarter-res permanent cache (goals 1 + 3)

For each page, in a single pass: extract a half-res DINOv2 patch grid in RAM, run every active cluster query against it (positive and FP exemplars both), persist any matches to `recurrence.db`, downsample the grid to a quarter-res cache, write that to disk, free the half-res RAM, move on.

**Per-page envelope**:
- Half-res in RAM: 621 × 877 → 44 × 62 patches × 384-d × 4 bytes = **~4.2 MB transient**
- Quarter-res on disk: 22 × 31 × 384 × 4 = **~1 MB permanent**

**Three-year scope** (1946 + 1947 + 1948, ≈ 52 issues × 8 pages × 3 = ~1,250 pages):
- Permanent quarter-res cache: **~1.25 GB** on disk, gitignored
- Streaming half-res forward pass: **~75 s GPU on MPS** for the first sweep
- Re-query against quarter-res cache (no GPU): seconds, no disk regrowth
- Goal-3 discovery passes: rebuild half-res *transiently* for a slice (one issue or one month), cluster, persist results, drop. Cost is bounded by the slice size.

Why quarter-res is enough as the permanent layer: the patch-grid is being used to *identify* archetype hits, not to render them. DINO features at quarter-res still discriminate identity reliably; we only need full half-res when we want fine-grained spatial localisation, which we get fresh during the streaming pass.

**The opt-in escape hatch**: `page_features.py --keep-features <issue_dir>:<page>` retains the half-res grid for specific pages we want to debug or re-query at higher resolution. Default = stream and discard.

**Query primitive** (one function, used three ways):

```python
def find_recurrences_on_page(page_features, query_emb, query_aspect, sim_thresh):
    # query_emb is the cluster's exemplar CLS embedding (already in embeddings.npz)
    # query_aspect is the bbox aspect of the exemplar ad on its source page
    # Slide a window of matching aspect across page_features, mean-pool patch features
    # inside the window, L2-normalise, dot-product with query_emb.
    # Non-max suppress overlapping high-sim hits.
    # Return list of (x_pct, y_pct, w_pct, h_pct, similarity).
```

The aspect comes from `detected_ads.{w_pct, h_pct}` of the exemplar; combined with each page's known dimensions (1241 × 1754) it pins the window shape in pixel-grid terms.

**Three uses of the same primitive**:

1. **Goal 1 — missed-ad recall**: queries = exemplars of clusters where `category='ad'`. Run against every page; emit `proposed_adds` rows listing high-sim hits whose bbox doesn't overlap any existing `detected_ads` row by ≥ 30%.
2. **Goal 2 — FP / furniture suppression**: queries = exemplars of clusters where `category IN ('body_text_fp', 'furniture')`. Hits that overlap existing inventory by ≥ 50% become `proposed_removes` rows. (More principled than Track A because it doesn't depend on the row already being in `ads_index.json`, and `furniture` catches mastheads that mistakenly entered the inventory too.)
   `unclassified` clusters log to `appearances` but emit no proposals — safe default until you've labelled them.
3. **Goal 3 — novel discovery**: rebuild half-res features *transiently* for a slice (e.g. all of one month), pool patch features into one bag, run mini-batch k-means or HDBSCAN on an L2-normalised mean per pseudo-region, persist clusters whose members span ≥ 3 issues into `discovered_clusters` + `discovered_members`, drop the transient features. Re-runs against a different slice are cheap because the GPU forward pass is fast at half-res.

**Cost**: ~75 s GPU for the streaming sweep across 1946–1948 (~1,250 pages) + matching is in-pass so no separate query cost. Re-queries against the quarter-res cache: seconds. Goal 3 per-slice rebuild: ~25 s for one year + ~5–15 min clustering.

**Storage**: ~1.25 GB on disk for the quarter-res cache across 1946–1948, in `recurrence_lab/page_features/{issue_dir}/p{N}.q4.npy` (gitignored). Restartable per page via `(issue_dir, page, mtime)` — embed.py's existing key shape.

## Recommended sequencing

Build in this order, committing between phases so you can stop at any point with a working incremental win:

**Phase 0 (prereq)**: extend ad-side embeddings to 1946 and 1948 — `python embed.py --years 1946 1947 1948`, then `python cluster.py`. embed.py is already incremental; this is just a re-run with a wider year list. Updates `embeddings.npz`, `ads_index.json`, `clusters.json` to the wider corpus before any page-level work begins.

**Phase 1 (Track A)**: `cluster_membership.py` — populate `clusters` and `cluster_membership` tables in `recurrence.db`. CLI `--mark-fp <id>` flips the FP flag. `export_viewer_json.py` writes a snapshot for the viewer's cluster-table panel. Lands a usable FP-suppression list before any Track B compute is spent.

**Phase 2 (Track B streaming)**: `page_features.py` — for each page in 1946–1948, half-res DINO forward in RAM, in-pass match against every cluster exemplar (writes to `proposed_adds` / `proposed_removes` / `appearances`), downsample to quarter-res, write the q4 cache, drop the half-res. Restartable on the `(issue_dir, page, mtime)` key. Optional `--keep-features` retains specific half-res pages for debugging.

**Phase 3 (lifecycle + viewer)**: SQL view `cluster_lifecycle` over the `appearances` table classifies each cluster as hot/warm/cold using a parameterised window. Viewer panel surfaces buckets so triage is bucket-aware. **Matching is not gated by bucket** — every archetype runs every page. Gating is a future toggle once the catalogue grows.

**Phase 4 (optional, Track B discovery)**: `discover_page_clusters.py` — pick a slice, transiently rebuild half-res features for it, cluster, persist `discovered_clusters` + `discovered_members`, drop the transient features. Run only when phases 1–3 are stable.

## Identification & triage

Each cluster carries a free-text `name` and a `category` from a 4-value enum:

| Category | Match-policy role |
|---|---|
| `ad` | Use exemplar as a **positive** query → contributes to `proposed_adds`. |
| `body_text_fp` | Use exemplar as a **negative** query against existing `detected_ads` → contributes to `proposed_removes`. |
| `furniture` (masthead, banner, recurring section header) | Do not propose as adds. If a hit overlaps existing inventory by ≥ 50%, propose a remove (it's not an ad, even if it was caught as one). |
| `unclassified` (default) | Record `appearances` rows but emit no proposals. Safe default for fresh clusters. |

`unclassified` is the default; every new cluster starts there. The catalogue is allowed to be partly-triaged forever — long-tail clusters that don't matter can stay unclassified without breaking anything.

**Stable labels across re-clusterings**: labels bind to `clusters.exemplar_path`, not `cluster_id`. When `cluster.py` re-runs (e.g. after Phase 0 widens the corpus to 1946–1948) and IDs renumber, the labels follow the exemplar to whatever new ID it lands in. `apply_labels.py` performs the join.

### UI: hybrid viewer (localStorage edits, YAML export)

A new "Triage" panel in `viewer.html`. For each cluster (sorted by size descending):

- A contact sheet of all members
- A `category` dropdown (4 values)
- A `name` text input

Behaviour:
- On load: viewer fetches `snapshots/clusters_table.json`, which already carries the current `category` and `name` from the DB. localStorage state is overlaid on top to preserve in-flight edits.
- On change: every dropdown/input change writes to localStorage immediately. No save button for the in-flight state.
- On "Export labels.yaml" click: downloads a YAML file based on the full localStorage view, keyed by `exemplar_path`.

Sample `cluster_labels.yaml`:
```yaml
labels:
  - exemplar: columns/1947-09-18/ads/p2/1947-09-18-02_ad5.png
    category: body_text_fp
    name: "1947 editorial standing text"
  - exemplar: columns/1947-03-19/ads/p7/1947-03-19-07_ad5.png
    category: ad
    name: "Almonte Pharmacy weekly"
```

Apply step (run from terminal after exporting):
- `python apply_labels.py recurrence_lab/cluster_labels.yaml` — joins each label to its cluster row by `exemplar_path` and writes `category` + `name`. Then re-runs `export_viewer_json.py` so the viewer reflects applied state on next load.

**Known limitation of localStorage**: edits live in one browser. If you triage from a second machine before exporting, the two states diverge. Mitigation:
1. Always export before switching machines.
2. The viewer surfaces a "last applied" timestamp pulled from `clusters_table.json`; if your localStorage edits predate that timestamp you'll know to discard local state.

If this friction proves real in practice, escalating to the live-Flask-editor design is a small follow-up — the schema and apply path are unchanged.

## Storage — `recurrence_lab/recurrence.db` (authoritative) + JSON snapshots (viewer)

The lab README already anticipates this: *"If we want to persist clusters durably (link an ad to its cluster_id, query 'how many issues did this ad run?'), create `recurrence_lab/recurrence.db` with its own schema."* Now is that moment. Reasoning:

- **Track A alone** produces ~1930 membership rows — fine as JSON.
- **Track B** could produce tens of thousands of `proposed_adds` / `proposed_removes` / `discovered_*` rows. Loading 50+ MB of JSON into the browser is ugly; SQL queries against a `.db` are better for triage.
- **Idempotent re-runs**: each phase can `INSERT OR REPLACE` by primary key. JSON re-runs need full file rewrites and are noisy in diffs.
- **Mutator handoff later**: when (if) we promote results to a mutator that writes `data/mvtm.db`, the schemas line up — `proposed_adds` already has `(image_filename, x_pct, y_pct, w_pct, h_pct)` shaped like `detected_ads`.
- **Viewer stays static**: a small `export_viewer_json.py` step writes `recurrence_lab/snapshots/*.json` (pre-filtered, capped) for the browser. The viewer itself never touches `.db`.

Everything is still **lab-only** — `recurrence.db` is a new, isolated file under `recurrence_lab/`. The main `data/mvtm.db` is only ever read.

### Schema (initial — extend per phase)

```sql
-- Phase 1
CREATE TABLE clusters (
    cluster_id    INTEGER PRIMARY KEY,
    size          INTEGER NOT NULL,
    n_issues      INTEGER NOT NULL,
    first_date    TEXT NOT NULL,        -- YYYY-MM-DD
    last_date     TEXT NOT NULL,
    exemplar_path TEXT NOT NULL UNIQUE, -- columns/.../ad.png — durable label key
    category      TEXT NOT NULL DEFAULT 'unclassified'
        CHECK (category IN ('unclassified', 'ad', 'body_text_fp', 'furniture')),
    name          TEXT,                 -- free text, e.g. "Almonte Pharmacy"
    notes         TEXT
);
CREATE INDEX idx_clusters_category ON clusters(category);
CREATE INDEX idx_clusters_exemplar ON clusters(exemplar_path);

CREATE TABLE cluster_membership (
    image_filename TEXT PRIMARY KEY,    -- joins to detected_ads.image_filename
    issue_dir      TEXT NOT NULL,
    page           INTEGER NOT NULL,
    cluster_id     INTEGER NOT NULL REFERENCES clusters(cluster_id),
    similarity     REAL NOT NULL        -- cosine to cluster centroid
);
CREATE INDEX idx_membership_cluster ON cluster_membership(cluster_id);

-- Phase 2 (streaming pass; cache is the persistent quarter-res grid)
CREATE TABLE page_features (
    issue_dir         TEXT NOT NULL,
    page              INTEGER NOT NULL,
    mtime             REAL NOT NULL,    -- of page_raw.png; restart key
    cache_scale       REAL NOT NULL,    -- 0.25 = quarter-res
    grid_h            INTEGER NOT NULL,
    grid_w            INTEGER NOT NULL,
    cache_path        TEXT NOT NULL,    -- page_features/<issue>/p<n>.q4.npy
    half_res_kept     INTEGER NOT NULL DEFAULT 0,  -- 1 if --keep-features was used
    half_res_path     TEXT,             -- nullable; only set when half_res_kept=1
    PRIMARY KEY (issue_dir, page)
);

-- Phase 2/3 — every cluster hit during the streaming pass lands here
CREATE TABLE appearances (
    cluster_id   INTEGER NOT NULL REFERENCES clusters(cluster_id),
    issue_dir    TEXT NOT NULL,
    page         INTEGER NOT NULL,
    x_pct        REAL NOT NULL,
    y_pct        REAL NOT NULL,
    w_pct        REAL NOT NULL,
    h_pct        REAL NOT NULL,
    similarity   REAL NOT NULL,
    observed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, issue_dir, page, x_pct, y_pct)
);
CREATE INDEX idx_appearances_cluster ON appearances(cluster_id);
CREATE INDEX idx_appearances_issue ON appearances(issue_dir);

-- Hot / warm / cold derived view (windows tunable, defaults 4 / 12 issues)
CREATE VIEW cluster_lifecycle AS
WITH issue_idx AS (
    SELECT issue_dir,
           ROW_NUMBER() OVER (ORDER BY issue_dir) AS idx
    FROM (SELECT DISTINCT issue_dir FROM appearances)
),
last_seen AS (
    SELECT a.cluster_id, MAX(ii.idx) AS last_idx
    FROM appearances a JOIN issue_idx ii ON a.issue_dir = ii.issue_dir
    GROUP BY a.cluster_id
),
latest AS (SELECT MAX(idx) AS now_idx FROM issue_idx)
SELECT
    c.cluster_id,
    c.size,
    c.category,
    c.name,
    ls.last_idx,
    (latest.now_idx - COALESCE(ls.last_idx, 0)) AS issues_since_last_hit,
    CASE
        WHEN ls.last_idx IS NULL                              THEN 'unseen'
        WHEN (latest.now_idx - ls.last_idx) <= 4              THEN 'hot'
        WHEN (latest.now_idx - ls.last_idx) <= 12             THEN 'warm'
        ELSE                                                       'cold'
    END AS bucket
FROM clusters c
LEFT JOIN last_seen ls ON c.cluster_id = ls.cluster_id
CROSS JOIN latest;

-- Phase 3
CREATE TABLE proposed_adds (
    uuid              TEXT PRIMARY KEY,
    issue_dir         TEXT NOT NULL,
    page              INTEGER NOT NULL,
    x_pct             REAL NOT NULL,
    y_pct             REAL NOT NULL,
    w_pct             REAL NOT NULL,
    h_pct             REAL NOT NULL,
    source_cluster_id INTEGER REFERENCES clusters(cluster_id),
    similarity        REAL NOT NULL,
    accepted          INTEGER NOT NULL DEFAULT 0   -- triage flag
);
CREATE INDEX idx_adds_page ON proposed_adds(issue_dir, page);

CREATE TABLE proposed_removes (
    target_image_filename TEXT PRIMARY KEY,    -- existing detected_ads row to remove
    source_cluster_id     INTEGER NOT NULL REFERENCES clusters(cluster_id),
    similarity            REAL NOT NULL,
    overlap_iou           REAL NOT NULL,
    accepted              INTEGER NOT NULL DEFAULT 0
);

-- Phase 4 (only if pursued)
CREATE TABLE discovered_clusters (
    disc_cluster_id INTEGER PRIMARY KEY,
    n_members       INTEGER NOT NULL,
    n_issues        INTEGER NOT NULL,
    exemplar_issue  TEXT NOT NULL,
    exemplar_page   INTEGER NOT NULL,
    x_pct           REAL NOT NULL,
    y_pct           REAL NOT NULL,
    w_pct           REAL NOT NULL,
    h_pct           REAL NOT NULL
);

CREATE TABLE discovered_members (
    disc_cluster_id INTEGER NOT NULL REFERENCES discovered_clusters(disc_cluster_id),
    issue_dir       TEXT NOT NULL,
    page            INTEGER NOT NULL,
    x_pct           REAL NOT NULL,
    y_pct           REAL NOT NULL,
    w_pct           REAL NOT NULL,
    h_pct           REAL NOT NULL,
    similarity      REAL NOT NULL
);
CREATE INDEX idx_disc_members ON discovered_members(disc_cluster_id);
```

`recurrence.db` and the `page_features/` cache are added to `recurrence_lab/.gitignore`.

## Files to add (lab-only)

All under `/Users/peter/Projects/MVTM/recurrence_lab/`:

| File | Phase | Purpose |
|---|---|---|
| `db.py` | 1 | Connection helper: opens `recurrence.db` (writes), opens `../data/mvtm.db` read-only for joins to `detected_ads`. Imports `pct_to_px`, `px_to_pct` from `../coordinates.py` via a `sys.path` insert. Also owns the `CREATE TABLE` DDL above (idempotent on every run). |
| `cluster_membership.py` | 1 | Walk `clusters.json` → upsert into `clusters` and `cluster_membership` in `recurrence.db`. New clusters land with `category='unclassified'`, `name=NULL`. Existing rows preserve their applied labels (matched by `exemplar_path`). |
| `apply_labels.py` | 1 | Read `cluster_labels.yaml`, join each label to its cluster by `exemplar_path`, update `clusters.category` and `clusters.name`. Idempotent. Auto-runs `export_viewer_json.py` afterwards. |
| `cluster_labels.yaml` | 1 | Hand-curated (via the viewer's "Export labels.yaml" button). Git-tracked. The triage record. |
| `page_features.py` | 2 | The streaming pass. For each unprocessed page: half-res DINO forward → in-RAM match against every cluster exemplar (writes `appearances`, `proposed_adds`, `proposed_removes`) → downsample to q4 → write `page_features/{issue_dir}/p{N}.q4.npy` and index row → drop half-res. Restart key: `(issue_dir, page, mtime)`. Flag `--keep-features <issue_dir>:<page>` retains the half-res `.npy` alongside. |
| `find_recurrences.py` | 3 | Re-query against the quarter-res cache when archetypes are added/edited or thresholds change. Reads `page_features` cache_path rows; no GPU. Same NMS at IoU ≥ 0.3. Updates the same tables. Idempotent. |
| `discover_page_clusters.py` | 4 | Pick a slice (issue range / month). Transiently rebuild half-res features in RAM, pool patch features, mini-batch k-means or HDBSCAN, filter by issue-span ≥ 3. Upsert into `discovered_clusters` + `discovered_members`. Drop transient features at end. |
| `export_viewer_json.py` | 1, 3, 4 | Read `recurrence.db`, write trimmed/capped JSON snapshots into `recurrence_lab/snapshots/` for the static viewer (e.g. `clusters_table.json`, `proposed_adds.json`, top-100 by similarity). Run after each phase. |
| `viewer.html` (modify) | 1, 3, 4 | Add panels: "Triage" (Phase 1 — category dropdown + name input per cluster, localStorage-persisted, "Export labels.yaml" button), "Proposed page-level matches" (Phase 3), "Discovered recurrences" (Phase 4). Reads only `snapshots/*.json` — no SQL in the browser. |

**No file under `/Users/peter/Projects/MVTM/` outside the lab is modified or written to.** No DB writes. No detection-pipeline edits. No `instructions/detection_methods_review.md` update yet — this is a spike; once a phase is promoted to a real production detector the doc gets updated in the same commit (per project CLAUDE.md).

## Critical existing artefacts to reuse (do not re-derive)

- `/Users/peter/Projects/MVTM/coordinates.py:1` — `pct_to_px`, `px_to_pct`, `pct_to_px_float`, `clamp_pct`. **Use these for any pct↔px conversion.** Do not write `int(x_pct / 100 * 1241)` inline.
- `/Users/peter/Projects/MVTM/recurrence_lab/embed.py:103` — image preprocessing pipeline (PIL → AutoImageProcessor → DINOv2). Track B should call the same processor with a different input size, not reinvent it.
- `/Users/peter/Projects/MVTM/recurrence_lab/embed.py:75` — incremental `(issue_dir, file, mtime)` keying. Track B's feature store should use the identical key shape.
- `/Users/peter/Projects/MVTM/recurrence_lab/cluster.py:84` — block-wise cosine matmul for similarity. Reuse pattern in Track B retrieval to avoid materialising full N×M matrices.
- `data/mvtm.db` table `detected_ads` — `image_filename` is the join key from `ads_index.json` to bbox in `(x_pct, y_pct, w_pct, h_pct)`. Read-only.
- `columns/{issue_dir}/p{N}/page_raw.png` — fixed 1241 × 1754 px source image for every page.

## Verification

End-to-end checks per phase (palette: blue / orange / black, never red/green):

**Phase 1 (Track A + triage)**:
- `sqlite3 recurrence.db "SELECT cluster_id, size, category, name FROM clusters ORDER BY size DESC LIMIT 5"` — eyeball top clusters; on a fresh DB all rows show `unclassified` / `NULL`.
- Open the new "Triage" panel in viewer.html (loaded from `snapshots/clusters_table.json`). Set the 46-strong cluster's category to `body_text_fp` and name it. Set the 1947-03-19 p7 ad5 cluster's category to `ad`. Click "Export labels.yaml". Run `python apply_labels.py recurrence_lab/cluster_labels.yaml`.
- Re-query: same SELECT now shows the two clusters with their applied labels. Reload the viewer; localStorage and applied state agree.
- Sanity: `SELECT SUM(size) FROM clusters` equals the row count of `cluster_membership` and the `n_ads` in `clusters.json`.

**Phase 2 (streaming pass)**:
- Pick a known page (`1947-09-18/p2/page_raw.png`). After the streaming pass, verify: (a) `page_features/1947-09-18/p2.q4.npy` exists and has shape `(22, 31, 384)` with per-row L2 ≈ 1.0; (b) appearances rows exist for any cluster that should fire on that page; (c) the half-res file does NOT exist on disk (streaming dropped it).
- Re-run `page_features.py` over the same input — confirm zero new GPU work (mtime restart key works) and the cache rows are unchanged.
- With `--keep-features 1947-09-18:2`, re-run for that one page; verify the half-res file is now persisted alongside.
- Render the q4 grid as a coarse heatmap (per-patch L2 distance to the page mean) overlaid on `page_raw.png` with **blue/orange** scaling. Eyeball that high-variance regions correspond to dense ink, not whitespace.

**Phase 3 (lifecycle + retrieval)**:
- Use the 1947-03-19 p7 ad5 cluster as a known-good query: `SELECT * FROM proposed_adds WHERE source_cluster_id = ?` should contain bboxes matching existing inventory at IoU ≈ 0.9 — i.e. retrieval recovers what we already have. Internal-consistency check.
- Use the 46-strong FP cluster as the query: `SELECT * FROM proposed_removes WHERE source_cluster_id = ?` should overlap exactly the 46 inventory rows, ± a few.
- For at least 5 high-confidence `proposed_adds` rows that don't overlap existing inventory, render a **blue** rectangle (per palette rule) on `page_raw.png` and visually confirm it sits on a real ad — ground in pixels, not in JSON counts.
- `SELECT bucket, COUNT(*) FROM cluster_lifecycle GROUP BY bucket` — eyeball the distribution. Hot should be the smallest, cold the largest if our 1946–1948 corpus has the cross-year run-off pattern we expect; if every cluster is "hot" we've mis-tuned the windows or the appearances table is empty.
- Pick one cluster known to span all three years; check its `cluster_lifecycle` row resolves to `hot`. Pick one known-1947-only cluster; expect `cold` once we've added 1948 issues.

**Phase 4 (discovery, only if pursued)**:
- Top-N discovered clusters: render a contact sheet of bbox crops per cluster. Eyeball that members are visually identical, not just "similar-ish". If the top clusters are dominated by whitespace or column rules, the patch-pool granularity is wrong — tune before persisting outputs.
