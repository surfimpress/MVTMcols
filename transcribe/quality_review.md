# Transcription quality review — spot check

Date: 2026-07-30
Scope: 6 columns sampled at random from completed issues (1891-11-13,
1891-12-18, 1894-05-18, 1912-12-27), cross-checked by hand against
the original slice images.

## Method

For each sampled row: pulled `transcript_text`, `quality_flags`,
`repair_needed`, and `transcriber_notes` from `transcribe.db`, then
opened the corresponding slice PNGs in `transcribe/work/slices/<id>/`
and read them side by side with the stored transcript.

Samples:
- `2edb0073…` — 1891-11-13 p8c0 (Newman & Abernethy ad + Local News)
- `a02ff478…` — 1894-05-18 p4c4 (halftone photo ad + Hemlock Bark notices)
- `e2e5c8ea…` — 1912-12-27 p8c0 (severe ink damage)
- `15ec803a…` — 1891-12-18 p11c2 (Antarctic exploration feature)
- `6baae249…` — 1912-12-27 p4c2 (Local News, obituary)
- `1eb44481…` — 1891-11-13 p4c1 (Editorial Notes)

## Verdict: transcription fidelity is strong

Word-for-word comparison against source images (e.g. `1eb44481…`,
full slice checked line by line) found **no invented text and no
misread words** in the checked samples. Specific things done well:

- Correctly transcribed items that a naive OCR-style read would get
  wrong: "Owen Sound" split oddly across a line break in the
  original, "Registrar" rendered in a way that could misread as
  "Kegistrar" in the scan — both transcribed correctly.
- Currency and figures ($50,000, $15,000, $2,000, $25,000, £4,000)
  all transcribed accurately against source.
- Diplomatic conventions followed correctly: curly quotes throughout,
  `[sic]` applied appropriately (e.g. "imlitary" kept as printed with
  `[sic]`, "mariage" kept as printed with `[sic]`), original
  capitalisation and hyphenation preserved.
- **Illegibility discipline is good.** The damage sample (`e2e5c8ea…`,
  1912-12-27 p8c0) is genuinely and severely ink-damaged — the
  transcript correctly marks large spans `[illegible]` rather than
  guessing, and `quality_flags.damage` / `low_legibility` are set
  correctly with `repair_needed: true`. This is exactly the
  conservative behaviour the agent instructions ask for.
- Ad/registered-content cross-checking works: `a02ff478…` correctly
  identifies a halftone photograph ad, references the registered ad
  UUID in `transcriber_notes`, and marks the photo caption
  `[illegible]` rather than inventing signage text.

## Issue found: slice-boundary duplication is not deduplicated

**This is a real, systemic defect, not a one-off.** Two of six
samples show the same failure mode:

- `2edb0073…` (1891-11-13 p8c0): the line "$2,000. It will shortly
  be inaugurated by a concert and ball." appears twice in
  `transcript_text` — once at the end of one slice's transcript,
  again at the start of the next slice's transcript.
- `1eb44481…` (1891-11-13 p4c1): same pattern — "...the position of
  Speaker. The other names are not yet announced," is duplicated
  across a slice boundary.

**Root cause**: slices are cut with ~20px of deliberate vertical
overlap so a transcriber can see whether a line continues into the
neighbouring slice (documented in
`.claude/agents/column-transcriber.md`, "Sliced mode" section). Each
transcriber correctly transcribes what it sees, including the
overlapping line, per its instructions ("Transcribe each slice as a
self-contained unit; the joiner reassembles"). But **no joiner
currently exists** — grepped `merge_slice_results.py` and
`ingest_column_result.py` for any overlap/dedup logic and found none.
The per-slice transcripts are concatenated as-is into the stored
`transcript_text`, so the deliberate overlap survives into the final
record verbatim.

**Scope of impact**: every multi-slice column that has a line
crossing a slice boundary is a candidate for this defect. Given
slicing is at structural rules (not paragraph breaks), this is
likely a meaningful fraction of the 191 columns transcribed so far
across the three completed issues, not just the two hit in this
6-sample check.

**Not yet checked**: how many affected columns exist in total, and
whether the duplicated line is always a clean exact-match (easy to
dedup) or sometimes has minor per-slice transcription variance
(harder to dedup automatically).

## Recommendation

Before resuming the transcription loop at volume, decide whether to:
1. Build a joiner step (in `ingest_column_result.py` or a new
   module) that detects and collapses the overlapping line between
   adjacent slices before writing `transcript_text`, or
2. Accept the duplication for now and note it as a known artefact
   for downstream consumers to filter, or
3. Do a full pass to quantify how many of the 191 already-done
   columns are affected, before deciding between (1) and (2).

No changes have been made to `transcript_text` for any row — this
review only read existing data.
