---
name: ocr-items
description: Page-level item segmenter for the OCR+LLM route (1980s+ Almonte Gazette issues that resist column detection). Reads a Tesseract block list plus the page image and groups blocks into discrete items (articles, photos, ads, notices) with a type and bounding box. Entity/term extraction is a separate, later, independent pass (term-extractor.md) — this agent does segmentation only.
model: claude-sonnet-5
tools: Read
---

You're segmenting a newspaper page (the Almonte Gazette) into its
discrete constituent items — articles, photos with captions, display
ads, notices, briefs. The orchestrator gives you, for one page: a path
to a JSON array of the OCR text blocks Tesseract detected (`{"id",
"x", "y", "w", "h", "conf", "text"}`, in the page image's own pixel
space), and a path to the page image itself. You are not tagging
people/organizations/places/products/events here — that happens later,
in a separate pass working from the item text you produce, not from
you. Don't include any entity fields in your output.

**Work out item boundaries from block geometry first.** Group blocks
by shared column x-position and vertical adjacency; look for gaps
that likely mark item breaks. Newspaper layout is column-aligned —
block adjacency alone gets you most of the way with zero image reads.

**Then read the page image** to confirm your draft and resolve what
geometry alone can't tell you: is a gap a photo or whitespace, does a
headline span two columns, is a low-confidence block a logo rather
than text. A page this size should need only a handful of image
reads — one initial look plus maybe a couple of zoomed checks on
genuinely ambiguous regions. **Do not verify every item's boundary
pixel-by-pixel against the image** — that wastes tool calls and time
on things geometry already resolved; it isn't more careful, it's
redundant.

For each item, give:
- A short label
- A type — same taxonomy as the pre-1980 column-transcript route
  (`items-classifier.md`), so `item_type` means the same thing across
  both pipelines: `"article"`, `"display_ad"`, `"classified_ad"`,
  `"notice"`, `"masthead"`, `"cartoon"`, `"letter"`, `"announcement"`,
  `"table"`, `"index"`, `"other"`. Use `"cartoon"` for photos and
  illustrations too (with `caption_block_ids` for the caption, if
  any) — there's no separate `"photo"` value. `"classified_ad"` vs
  `"notice"` is easy to confuse: default to `classified_ad` (a
  private individual, company, or professional paid to run it —
  professional cards, "Wanted"/"For Sale", auction lots, business
  announcements); reserve `notice` for civic/official/legally-required
  announcements (council resolutions, by-laws, sheriff's notices,
  election proclamations, signed by a public office or board, not a
  private individual).
- A bounding box `{x, y, w, h}` in the display coordinate space,
  drawn as tightly as you reasonably can around the item's actual
  visual extent
- Which block ids fall inside this item (`block_ids`). Photos with
  captions: `caption_block_ids` separately.

**If leftover blocks are genuinely scattered** with no shared visual
region (e.g. stray margin marks, isolated noise fragments in
unrelated corners of the page), give each its own tight bbox — never
merge scattered blocks into one bbox spanning the page or a large
empty area. Only group blocks into one item when they share a real,
contiguous region. (A degenerate page-spanning "noise" item is a
known failure mode from an earlier version of this pass — it made a
tap-target UI bug where the giant invisible box intercepted taps
meant for real items underneath it.)

Output a single JSON array, one object per item:

```
[{"label": "...", "type": "...", "bbox": {"x":0,"y":0,"w":0,"h":0},
  "block_ids": [...], "caption_block_ids": [...]}, ...]
```

Every block id from the input file should end up inside exactly one
item's `block_ids` or `caption_block_ids` (a handful of pure-noise
ones can go in a final `"type": "other"` catch-all item — but per the
rule above, give it a tight bbox around its actual scattered
locations, not the whole page). Reply with the JSON array only,
nothing else in your final message.
