# ARCHIVED — vision-only layout analysis, 2026-08-16

**Closed. Do not reopen without a reason not already in
`instructions/scaled_pipeline.md` §5u**, which records the results and
why the line stopped.

Four subagent runs analysed **2001-01-03 page 7** from images alone, with
an explicit rule against writing or running any code. The question was
whether a model looking at a page could substitute for, or calibrate,
the classical pipeline.

**It cannot substitute.** 38–87k tokens for one page, against the
77–104k/page that the whole scaled experiment exists to escape.

| run | model | input | tokens | sec | items | matched production (of 17) | n_columns |
|---|---|---|---|---|---|---|---|
| 1 | Opus | 892px + drawn grid + 6 tiles | 67,605 | 210 | 20 | 12 | **8** ✓ |
| 2 | Haiku | 600px, no grid, 1 image | 38,500 | 21 | 11 | 6 | 3 |
| 3 | Haiku | 892px + drawn grid + 6 tiles | 61,047 | 164 | 23 | 9 | 3 |
| 4 | Sonnet | 892px + drawn grid + 6 tiles | 86,552 | 282 | 19 | 7 | 3 |

The fitted lattice says 8 columns. Our classical route finds 6 zones.

**The scores are concordance with the production LLM route, not
accuracy.** Those 17 items are themselves a model's output. Items with no
production counterpart score zero while arguably being correct.

## Files

| file | what it is |
|---|---|
| `p7_low600_input.png` | the 600px image given to run 2 |
| `p7_grid_input.png` | the 892px image with the 5%/10% measurement grid, given to runs 1, 3, 4 |
| `p7_vision_agent_result.json` | run 1 (Opus) raw output |
| `p7_haiku_result.json` | run 2 (Haiku, no grid) raw output |
| `p7_haiku_grid_result.json` | run 3 (Haiku, run-1 inputs) raw output |
| `p7_sonnet_result.json` | run 4 (Sonnet) raw output |
| `p7_*_markup.png` | each run's boxes drawn on the page |

The six detail tiles given to runs 1, 3 and 4 were working files and were
not kept; they are reproducible from `page_display.png` by the crop list
in the session history (six overlapping regions, 55% wide, covering
y 0–40 / 35–72 / 68–100).
