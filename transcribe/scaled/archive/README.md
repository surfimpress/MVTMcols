# Archived work from the `scaled` experiment

Four separate archives, seven modules. **None is imported by anything
in the live path.**

NOTE: this directory has no `__init__.py`, so `detect_bands`,
`detect_boxes_pairing` and `detect_columns` cannot be imported as modules
(`cannot import name '_support'`). `refine_columns.py` does import, which
is what CLAUDE.md's "kept runnable" refers to -- it does not generalise to
the others. They are kept as readable source, not as runnable code.

---

# Archived: the confidence-scoring approach

**Dead end, kept for the record. Do not build on these.**

`detect_columns.py` and `detect_bands.py` tried to *discover* layout by
combining several weak signals and then scoring how much to trust the
result — corroboration, rule support, regularity, completeness, all
folded into a confidence number with an LLM-escalation gate.

## Why it was abandoned

It was the wrong shape for the problem. These pages were assembled on a
fixed physical grid (see `instructions/typesetting_practice.md`), so the
layout is a handful of constants to be *fitted*, not an unknown signal to
be discovered and then hedged about. The scoring machinery was elaborate
compensation for asking the wrong question.

It also failed repeatedly in an instructive way — every failure was a
metric that flattered itself, and each was caught only by rendering the
page:

- `detect_columns` scored a visibly-wrong page **0.853** because the
  score measured precision with no notion of recall. Fixed with a
  multiplicative completeness term (0.853 → 0.426).
- `detect_bands` then scored a 62%-tall band containing several
  differently-structured articles **0.917**, because coverage and
  regularity don't penalise an internally heterogeneous band — the same
  failure one level up.
- The grid fitter inherited the habit: a page whose grid was *visually
  verified as correct* scored only 0.20 against raw edges.

The pattern is the point: a score invented alongside the detector tends
to certify the detector. `post1980_layout_observations.md` records this
project hitting it before.

## What replaced it

`transcribe/scaled/detect_grid.py` — fit the four numbers the page was
set on (margin, column width, gutter, column count) and treat anything
off the lattice as noise.

## Numbers, for reference

| | escalation | median score |
|---|---|---|
| `detect_columns` (full-height) | 97.8% | 0.29 |
| `detect_bands` | 31.5% | 0.72 |

Both used a 0.60 gate. Neither number meant what it appeared to mean.

---

# Archived: pass 2 — per-edge refinement (`refine_columns.py`)

**Set aside 2026-08-15, not a dead end. We may return to it.**

Unlike the confidence-scoring archive above, this is a working piece of
code that was measured against its alternative and lost. The user's call:
*"Reviewing the pass 1 and pass 2 versions, in almost every case, pass
one is the better version."*

## What it did

Subsumed stray fragment blocks into their parents, re-fitted on the
cleaned blocks, then pulled each column edge independently to the
outermost nearby edge — left edges leftward, right edges rightward,
lefts resolved first so a right edge could be bounded by the actual next
left minus a minimum gutter.

## Why it lost

Pass 1 fits **two global parameters** (pitch, offset) and derives one
column width. Pass 2 replaced that with **2n free parameters**, one per
edge, each leaning toward whatever sat furthest out nearby — which on
these pages includes display-ad interiors set to their own grid.

Measured over 89 fitted pages:

| | within-page gutter variation |
|---|---|
| pass 1 | **0.00%** — constant by construction |
| pass 2 | median 0.42%, mean 0.48%, max 1.24% |
| pass 2 | varies >0.30% within the page on **54/89 pages (61%)** |

A gutter is one physical measure, about 1 pica, set once on the
pasteboard or master page. It **cannot** vary down a page. So pass 2's
variation was the fit following noise, not following the page.

## The motivation is still real

Pass 2 existed because a rigid lattice cannot follow the scan's own
scale drift across the page — measured at roughly 1.3% by the right-hand
edge on a page that fits well on the left. **That problem is unsolved.**

If it is revisited, the lesson is that the correction must stay
**parametric** — one global scale or skew term fitted across the whole
page, keeping the gutter constant — rather than per-edge. Per-edge
freedom is precisely what let ad interiors pull the answer around.


---

# Archived: three generations of box detection

**Superseded by the corner derivation. Kept for the journey.**

`detect_boxes_pairing.py` built rectangles by PAIRING rules -- top/bottom
horizontals bridged between a matched pair of verticals. It needed six
tuned thresholds (aspect ratio, thin dimension, gap tolerance, twin
collapse, double-rule merge, gutter drop) because it asked "is this a
valid rectangle?", and the union of two stacked ads answers yes.

`corner_quadrilaterals.py` was the first corner-based generation: it
enumerated quadruples of corners. Order-dependent, and it inherited the
same union problem.

`percent_box_filters.py` holds that generation's two page-percent
filters, `drop_gutters` and `merge_double_rules`. Both applied one
threshold to both axes, which is anisotropic -- `drop_gutters`'s "ratio"
scores a SQUARE region at 1.406. See scaled_pipeline.md 5z.7.

All three are replaced by ONE predicate in
`experiments/ad_rectangles.py`: a rectangle is an item when no other
corner interrupts its sides.
