# Tidy-pass agent briefing

Briefing for a subagent doing post-detection cleanup on column boundaries
and ad bboxes via the `mvtm` CLI. Distilled from working through the
1940-02-20 p10 SUPERIOR + DOMINION cases on 2026-04-28.

This document is the durable instruction set. The verbal briefing in a
specific session can be terser if it points here.

---

## Hard rules (do not violate, ever)

### 1. One frame of reference: page-pct, top-left = (0, 0)

All coordinates in this pipeline are **percent of page width / height,
measured from the top-left corner of the PDF page**. Stay in that frame
end to end. Don't drop into image-pixel coordinates as a "shortcut" for
arithmetic and convert back — that's where direction errors come from.
The DB schema stores page-pct, the CLI mutators take page-pct, the
viewer displays page-pct. Use it throughout.

If you find yourself thinking "let me work in pixels here and convert
later", stop. Use `coordinates.py` if a conversion is genuinely needed.

### 2. Replace before delete

For any tidy operation that involves a swap (merging two ads, replacing
one detection with another bbox, anything where N rows become M):

1. Adjust the survivor row to the correct new bbox.
2. Render and visually verify the new bbox aligns with the page.
3. Get human confirmation (or, in autonomous mode, double-check against
   a second criterion).
4. *Only then* delete the redundant rows.

Never delete first. If step 2 looks wrong, `mvtm undo` rolls back step 1
and the redundant rows are still there as a fallback. Reverse the order
and there is no fallback.

### 3. Render the proposal *before* committing

Workflow when proposing any bbox change:

1. Compute the proposed bbox in page-pct.
2. Draw it overlaid on `columns/{issue}/p{page}/page_raw.png` using
   PIL — a thick coloured rectangle.
3. Save the overlay to `columns/{issue}/p{page}/tidy_proposal*.png` so
   it's accessible at the public viewer URL.
4. Read the proposal image yourself (and have the human read it, if
   in interactive mode). Verify all four edges align with the visual
   frame.
5. Only after the proposal looks right do you call the mutator.

Rendering after the mutator is *also* required, but it's not a
substitute for rendering before.

---

## How to measure (the method)

### Use the pre-cut column files on disk

For every processed page, `columns/{issue}/p{page}/` contains
`{date}-{page}_col1.png` through `_col{N}.png`. These are full
page-height strips already cut by the pipeline. **Use them.** Don't
re-render the page from the PDF — those files exist for exactly this
reason.

File naming is 1-indexed: `_col1.png` is the leftmost column (column
index 0 in the DB).

### Determine column span by inspecting column files

For each column strip in turn, look: does it contain (part of) the ad
in question? You get a yes/no list: e.g. col1=yes, col2=yes, col3=no.
That gives you the column span *exactly*. No estimation, no overlap
math.

The x bounds of the ad are then the corresponding entries in
`page_layouts.boundary_positions`: `bps[first_col_idx]` to
`bps[last_col_idx + 1]`.

### Read y-extents off a numbered ruler

To measure the y-extent of an ad:

1. Open the column strip that contains the ad (any one of the
   spanning columns; they all have the same y axis).
2. Draw horizontal ruler lines at every 1% (and half-percent ticks at
   0.5%, and bold lines at 5% / 10% with bigger labels).
3. **Label every line with its number.** Not just the 5% lines.
   You'll need 0.5% precision for sharp ad edges, and an unlabeled
   tick is a guess.
4. Save as `tidy_ruled_{range}.png` in the page directory.
5. Read the y-pct of the ad's top edge and bottom edge directly off
   the labeled ticks.

A reference render script is in `instructions/_examples/draw_ruler.py`
(write one if it doesn't exist; PIL `ImageDraw.line` + `ImageDraw.text`
plus a system font). Use a high-contrast colour scheme: 5%/10% =
red on yellow background, 1% = blue, 0.5% = green tick. Yellow
background under the labels keeps them readable over text.

### Measure each edge independently

For stacked ads (e.g. SUPERIOR above DOMINION on 1940-02-20 p10), the
*bottom* of A and the *top* of B are not the same y. There's almost
always a small gap (~0.5–1.0% page height) between two adjacent ad
frames. Read each edge separately. Don't assume they share a value
because they're "close to each other" — that conflation cost an
iteration on this page.

### What precision to use

The pre-cut column strips at typical scan DPI support reading to
**0.5% precision** comfortably. Round half-percent values to one
decimal (e.g. `64.5`, not `64`). Don't round to integer or to nearest
5% — that's lossy enough to land outside the visual frame.

### x-bounds: snap to column boundaries

Even when the visual ad frame sits a hair inside the column boundary
(thin white margin), set x and x_end to the boundary values. The
project convention is column-grid-aligned ad bboxes, since downstream
clipping operates on column pitch. The user has confirmed this for
multi-column ads on 2026-04-28.

---

## Mutator order recap

For a "merge two stacked detections into one outer-frame bbox" tidy:

```
1. Adjust survivor to outer-frame bbox          (replace step)
2. mvtm view --overlay ads ...                   (verify)
3. ─── human confirmation ───
4. delete-ad on the redundant inner panels       (delete step)
5. mvtm view --overlay ads ...                   (post-state check)
6. mvtm regenerate-page <date> <p>                (resync artefacts)
```

For a "shrink an over-extended ad" tidy:

```
1. Adjust to correct bbox                        (replace step)
2. mvtm view --overlay ads ...                   (verify)
3. ─── human confirmation ───
4. mvtm regenerate-page <date> <p>                (resync artefacts)
```

No delete needed in the shrink case.

For an "add a missed ad" tidy (detector returned nothing for a region
that visibly contains an ad):

```
1. Measure bbox in page-pct using ruler crops    (col file + ruler_crop.py)
2. Render proposal overlay on page_raw.png       (proposal step)
3. ─── human confirmation ───
4. mvtm add-ad --year ... --x-pct ... --w-pct ... (insert step)
5. mvtm regenerate-page <date> <p>                (resync artefacts)
```

`add-ad` infers `--cols` from current page boundaries (override with
`--cols N` if the inference looks wrong). The new row is `hand_edited=1`
so re-detect won't sweep it back out. `image_filename` is auto-assigned
to the next free index for that page+kind (`_ad{N}` for multi-col,
`_sc_ad{N}` for single-col); deletes do NOT reshuffle existing
indices, so a fresh add picks up `max+1` even if intermediate indices
are missing. `mvtm undo` reverses an add-ad by deleting the inserted
row by uuid.

If the detector produced a partial match (right region but wrong
extents), prefer `adjust-ad` over `delete-ad` + `add-ad` — adjust
preserves uuid and image_filename so downstream references stay
valid.

### Why `regenerate-page` is the closing step

Mutators (`adjust-ad`, `delete-ad`, `move-boundary`, etc.) only update
the SQLite rows. The on-disk derivatives — the per-ad PNG crops
(`{date}-{p}_ad{N}.png`, `_sc_ad{N}.png`) and the per-column strip
PNGs (`_col{N}.png`) — were cut by the original detection pipeline
and don't update with each mutator. After a batch of edits, the
filesystem state has drifted from the DB.

`mvtm regenerate-page <year> <month> <day> <page>` re-cuts those
derivatives from the cached PDF using the current DB state, and
refreshes `viewer_data.json` so the public viewer reflects the new
state. Scope flags: `--scope ads` (PNG crops only), `--scope columns`
(strips only), `--scope both` (default), `--scope viewer` (just the
JSON refresh, no PNG re-cut).

Run it once at the end of a tidy session for each modified page —
not after every individual mutator. Re-rendering at 450 DPI is the
expensive step; batch it.

### Where ad PNGs actually live (and why this matters)

There are **two** plausible directories an ad PNG could live in:

- `columns/{issue}/p{page}/{date}-{page}_ad{N}.png` — per-page dir
- `columns/{issue}/ads/p{page}/{date}-{page}_ad{N}.png` — per-issue
  ads dir

**The viewer reads from the second one.** That's where
`process_issue.py` writes ads (`output_dir/ads/p{N}/`), and that's
what `viewer_data.json` references. As of 2026-04-28,
`regenerate-page --scope ads` writes there too, and additionally:

- removes orphan ad PNGs in `ads/p{N}/` whose filenames no longer
  match a DB row (covers the deleted-inner-panel case after a
  concentric-box merge)
- removes stale ad PNGs that earlier (buggy) regenerate-page runs
  dropped into the per-page `p{N}/` directory; columns, page_raw,
  overlays in `p{N}/` are left alone

**If the viewer doesn't reflect a tidy edit**, the most likely cause
*used to be* that the new PNG never reached `ads/p{N}/`. That's now
fixed at the CLI level — but if you ever see a tidy edit not showing
up, list both directories before forming a hypothesis. The data is
the answer.

### Per-ad PNG filename conventions

- Multi-column ads: `{date}-{page}_ad{N}.png` where N is 1-indexed by
  insertion order (preserved across deletions — if `_ad3` is deleted,
  surviving `_ad4` keeps that suffix; `_ad3` is not reused).
- Single-column ads: `{date}-{page}_sc_ad{N}.png`, same indexing rule
  but a separate counter from multi-col.
- The DB column `detected_ads.image_filename` is the source of truth
  for the suffix. `regenerate-page` re-cuts in DB-id order then moves
  outputs to match the stored `image_filename`, so deletions don't
  cause filename re-shuffling. **Never assume sequential indices** in
  filename suffixes after a tidy session.

---

## Visibility in the viewer

`_update_viewer_data` filters out low-confidence ads that look like
body-text false positives. As of 2026-04-28, this filter respects
`hand_edited = 1` — any ad that has been adjusted via the CLI is
trusted and shown in the viewer regardless of confidence. So a tidy
edit on a low-confidence ad WILL show up in the viewer after running
`_update_viewer_data`.

If the user reports an edit didn't appear in the viewer, check:
1. Did `_update_viewer_data` run? It's not automatic on mutator calls.
2. Is `hand_edited = 1` on the row? `mvtm show` will tell you.
3. Did the viewer's JS cache get cleared? Hard refresh.

---

## What this briefing supersedes

An earlier informal briefing (in conversation) said "use headlines or
detection coordinates as the starting point for outer-frame bboxes."
That was wrong. The starting point is the **visual frame**, measured
on the page image. Detection coordinates are the *thing being fixed* —
trusting them is self-defeating.

It also implied integer-percent precision was sufficient because the
user gave "66%" as an example. The example was illustrative; the actual
precision is whatever the column strip supports (0.5% in practice).

---

## Update history

- **2026-04-28** — File created. Captures the SUPERIOR + DOMINION tidy
  workflow on 1940-02-20 p10, the half-percent measurement discipline,
  the pre-cut column files convention, and the `hand_edited`
  filter-bypass commit.
- **2026-04-28** — Added "Where ad PNGs actually live" and "Per-ad
  PNG filename conventions" sections after a `regenerate-page` bug
  where ad PNGs were written to the per-page directory instead of the
  per-issue `ads/p{N}/` directory the viewer actually reads from. CLI
  fix: 279384e on main.
- **2026-04-28** — Added "add a missed ad" workflow now that
  `mvtm add-ad` exists; documented index-not-reshuffled rule and the
  adjust-ad-vs-add-ad partial-match preference. Same commit also
  fixed an orphan-cleanup bug in `regenerate-page` (zero-padding
  mismatch in the page prefix prevented stale ad PNGs from being
  removed for any page < 10).
