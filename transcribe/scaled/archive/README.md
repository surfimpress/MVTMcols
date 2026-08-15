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
