# recurrence_lab — agent notes

## What's live vs what was tried and dropped

**Live (Phase 1):** cropped-ad clustering — `embed.py` + `cluster.py` +
`cluster_membership.py` + `apply_labels.py` + `viewer.html` triage
round-trip. CLS-on-cropped-ads at cosine ≥ 0.98 gives clean clusters.

**Dropped (Phases 2–4 of the original plan):** page-level recurrence
matching via DINOv2 patch features over full pages. The streaming
full-page-forward design is dead (ViT attention contaminates patches
so full-page-window vs standalone-crop sims collapse from 0.97 to
0.06). Per-window standalone forwards localise correctly but don't
discriminate between similar-shape ads (Star Theatre 0.978 vs Almonte
Garage 0.953 false positive on the same column-aligned candidate; CLS
didn't fix it — DINOv2's the constraint, not the aggregator).

**Before proposing a page-level recurrence approach, read
`archive/README.md`.** It has the empirical numbers; don't re-run those
experiments. The natural next step *if* the question is revisited is a
retrieval-trained model (CLIP / SigLIP), not another aggregator or
larger DINO.

## Lab-only writes

This lab does not import from or write to the main MVTM pipeline. Read
`../data/mvtm.db` only as `?mode=ro`. All writes land inside
`recurrence_lab/`. The one shared utility this lab is allowed to import
is `../coordinates.py` (passive helpers, per project CLAUDE.md).
