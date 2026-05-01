# Archive — explored-and-dropped directions

Things tried in this lab that didn't work out, kept for reference so the
same path isn't re-walked from scratch.

## `page_level_recurrence_plan.md` + `page_features.py`

**Status: abandoned 2026-04-30. Phase 1 (cropped-ad clustering) kept;
Phases 2–4 (page-level matching) dropped.**

The plan proposed using DINOv2 patch features over full pages to (1)
find ads `detect_ads.py` missed, (2) flag false-positive ad-cluster
matches, and (3) discover novel recurring page elements. Two
architectures were considered: a streaming full-page forward, and
per-window standalone forwards.

Both fail at the level of discrimination required, for different reasons:

### Streaming full-page forward (the plan's primary design)

A single DINOv2 forward over the half-res page produces a 44×62 patch
grid. Mean-pooling within a candidate window gives a query-comparable
embedding — that was the design's premise.

It doesn't hold. Direct test on a known-positive page:

| comparison | sim |
|---|---|
| crop mean-pool vs same region cropped from page (standalone forward) | 0.967 |
| crop mean-pool vs same window inside full-page forward | 0.038 |
| standalone region vs window-inside-full-page (identical pixels) | 0.061 |

ViT attention contextualises every patch across the whole image, so a
patch from a 1241×1754 page is not the same kind of vector as a patch
from a standalone 280×252 ad crop. The streaming-pass design is dead
on arrival with DINOv2 features.

### Per-window standalone forwards

Drop the streaming idea; run DINOv2 separately on each candidate window.
This restores match-side symmetry. Localisation works:

- Star Theatre exemplar vs every column-aligned 2-col window on
  1947-04-24:p5: top hit 0.978 at the correct column 0.
- OBrien Cinema exemplar vs every column-aligned 3-col window on
  1947-04-17:p8: top 7 hits all at the correct column.

But discrimination doesn't. On the same Star Theatre page, every
similar-shape (~21% wide) cluster's exemplar also peaked at column 0
(because that's where the only 2-col ad is):

| cluster | mean-pool top sim | CLS top sim |
|---|---|---|
| Star Theatre (true positive) | 0.982 | 0.978 |
| Almonte Garage | 0.959 | 0.953 |
| Combas Furniture | 0.944 | 0.932 |
| Karls Grocery | 0.918 | 0.895 |
| Davis Boyce | 0.915 | 0.919 |

The discriminator margin between true positive (Star Theatre) and the
worst false positive (Almonte Garage, 21.5×15.7 vs Star Theatre's
19.8×14.5) was **0.025** under CLS and **0.022** under mean-pool. A
flat threshold at 0.95 admits Almonte Garage as a false positive on
Star Theatre's page; raising to 0.97 risks losing real matches on
other pages where the absolute sim sits lower.

CLS doesn't fix this — switching aggregator changes the numbers within
~0.01. The constraint is the **model**: DINOv2-small at this granularity
encodes "two-column display ad with bold header at top, body text
below" before it encodes the specific ad's identity. Mean-pool of a
2-col-wide window on the page is dominated by that shape signal, and
every 2-col-wide query exemplar matches that shape.

### What might work, and isn't done here

- A retrieval-trained model (CLIP, SigLIP) instead of DINOv2 — these
  are trained for instance discrimination, not generic visual feature
  quality. Plausible but unverified; not pursued because Phase 1 ad-
  clustering already gives the FP-suppression value originally
  motivating Phase 2 goal (2). Goals (1) and (3) (find missed ads,
  discover novel recurrences) would need this work and aren't free.
- A larger DINO variant (ViT-L/14, ViT-G/14) — same architecture
  family, more capacity. May or may not cross the threshold. Not tried.

If revisiting: don't re-run the streaming-pass experiment, the answer
is in the table at the top. Start with the model swap.

### Phase 1 was kept

The cropped-ad clustering (`embed.py` + `cluster.py` +
`cluster_membership.py` + `apply_labels.py` + `viewer.html` triage)
works well. CLS-on-pre-cropped-ads at cosine ≥ 0.98 gives clean
clusters that drive a usable triage round-trip. That's all that
shipped from this plan.
