# Newspaper column analysis pipeline

A guide for Claude Code agents working on the Almonte Gazette digitisation project. This documents the tools, techniques and design decisions developed over several R&D sessions in February 2026.

## Context

The Almonte Gazette ran from 1862 to 2007 in Almonte, Ontario. The project aims to digitise approximately 100,000 pages from bound-volume scans into structured, searchable content stored in SQLite databases with IIIF manifests for web accessibility.

The scans are heritage documents with characteristic imperfections: binding shadows near the gutter, slight skew and warp from the binding curvature, show-through from the reverse side, and variable ink density. The pages are typically arranged in a 7-column grid, though column count varies across the newspaper's 145-year run.

The pipeline described here handles **stage 2** of the process: given a PDF page, detect column boundaries, segment each column into individual items (articles, ads, notices), classify those items, and prepare them for transcription by a separate LLM pass.

## Pipeline overview

The full per-page workflow has four stages, each handled by a separate script. They are designed to run sequentially, with each stage's output feeding the next:

```
PDF page
  │
  ├─ 1. find_columns.py ─────► column boundary positions (% of page width)
  │
  ├─ 2. crop_pdf.py ──────────► individual column images (PNG at 300–450 dpi)
  │
  ├─ 3. four_probe_v5.py ─────► per-tooth classification, paragraph boundaries
  │     (replaces find_splits.py for paragraph-level work)
  │
  └─ 4. classify_segments.py ──► LLM-classified items with cross-column context
```

There is also a parallel earlier path through `find_splits.py` which uses horizontal blur analysis to find inter-item boundaries (headings, rules, whitespace gaps). This works at a coarser granularity — it finds article-level breaks rather than paragraph-level ones. The two approaches are complementary and may be combined in production.

### Auxiliary scripts

- `crop_pdf.py` — flexible PDF region extraction with a multi-unit coordinate system
- `boundary_overlay.py` — draws numbered boundary lines on a column image for LLM-assisted correction

-----

## 1. Column detection (`find_columns.py`)

### What it does

Takes a PDF page and detects the vertical column boundaries by analysing the mean darkness of each pixel column across a vertical slice of the page.

### How it works

The script renders a horizontal strip of the page (typically one grid square, eg the 6th row at 10% grid resolution) to a greyscale image, then:

1. Computes the mean inverted brightness for each pixel column (white=0, black ink=255)
1. Identifies candidate boundaries where darkness exceeds a threshold, indicating a vertical rule or text edge
1. Validates candidates by measuring row-by-row consistency (a real column rule has low standard deviation, while random text has high std) and checking for the valley–spike–valley pattern (whitespace flanking a ruled line)

### Output

A list of `ColumnBoundary` dataclass objects, each recording the boundary's position as a percentage of the page width, its peak darkness, consistency score, and valley depth.

### Design decisions

- The detection strip should avoid the masthead area (top 5%) and the bottom margin. Row 6 of a 10% grid (ie 50–60% of page height) typically contains body text in most columns, making it a reliable detection zone.
- Column boundaries are expressed as viewport-width percentages, not pixels. This decouples them from DPI.
- The approach was chosen over edge detection (Hough transform) because the scan artefacts produce too many false positives from text edges, ad borders, and binding shadows.

### Known limitations

- Assumes columns are separated by vertical rules or clear gutters. Pages with full-width ads or illustrations that span multiple columns will confuse the detector.
- Works best when the detection strip contains body text in most columns. A strip that crosses a large ad may miss boundaries.

-----

## 2. PDF cropping (`crop_pdf.py`)

### What it does

Extracts a rectangular region from a PDF page and saves it as a PNG at a specified DPI. Supports a flexible unit system designed for the newspaper workflow.

### Unit system

|Unit|Meaning                             |Use case                                         |
|----|------------------------------------|-------------------------------------------------|
|`vw`|1% of PDF page width                |Column extraction: `x=38.3, w=11.2, xunits="vw"` |
|`vh`|1% of PDF page height               |Full-height columns: `y=0, h=100, yunits="vh"`   |
|`px`|Pixels at the specified DPI         |Precise sub-item cropping                        |
|`N%`|Block units (N% per unit, 1-indexed)|Grid system: `x=8, xunits="10%"` = 70–80% of page|

Block units (where the multiplier is >1) are **1-indexed**: `x=8` with `10%` units means the region starting at `(8-1) × 10% = 70%`. Direct units (`vw`, `vh`, `px`) use the value as given.

### Typical usage

```python
from crop_pdf import crop_pdf

# Extract column 3 with 1% buffer on each side
crop_pdf("page.pdf", 37.3, 0, 13.2, 100, "vw", "vh", dpi=450,
         output_path="col3.png")

# Extract a sub-item from a column (using pixel coordinates within the column)
crop_pdf("page.pdf", col_x, y_start, col_w, height, "vw", "vh", dpi=450,
         output_path="item_01.png")
```

### Design decisions

- The unit system was designed so that column boundaries from `find_columns.py` (expressed as vw percentages) can be fed directly into `crop_pdf` without conversion.
- The buffer zone (typically 1% each side) is added at the crop stage rather than the detection stage, so the boundaries remain exact while the crops are generous.
- DPI choice matters: 300 dpi is adequate for structural analysis; 450 dpi improves transcription accuracy for small type but doubles processing time and memory.

-----

## 3. Paragraph-level analysis (`four_probe_v5.py`)

This is the most complex and most refined part of the pipeline. It takes a single-column image (already extracted by `crop_pdf`) and classifies every text line, detecting paragraph boundaries, headings, rules, and empty space.

### Core concept: the comb template

Body text in a newspaper column has a characteristic physical regularity imposed by the printing process. Every line of body text is the same height, and the line spacing (leading) is fixed. This creates a perfectly periodic vertical pattern — a "comb" of evenly spaced teeth, where each tooth is one line of text.

The algorithm exploits this regularity. Rather than trying to detect individual text features, it:

1. Measures the period (line spacing in pixels) using autocorrelation
1. Finds the phase (vertical offset where the comb best aligns with the actual text)
1. Locks a comb template onto the body text rhythm
1. Classifies each tooth of the comb based on what it finds there

### The eight-pass pipeline

**Passes 1–3: Period detection**

The image is converted to a binary mask (ink vs paper at threshold 128), then a horizontal mean darkness profile is computed for each row. Autocorrelation of this profile reveals the dominant periodicity — the body text line spacing.

Pass 1 gives a rough period from the full column. Pass 2 uses this to identify vertical zones with strong periodicity (body text regions vs ads or images). Pass 3 concatenates the body-text zones and re-measures the period from the cleaner signal, giving a more accurate value.

Typical periods: ~16px at 300 dpi, ~25px at 450 dpi.

**Pass 4: Phase alignment**

The comb must be aligned so that each tooth centre falls on a text line, not between lines. The algorithm tries every possible phase offset (0 to period−1) and picks the one where the teeth collectively capture the most ink.

This is done by summing the darkness values at the comb positions across all body-text zones. The winning phase typically snaps to within ±1px of perfect alignment.

**Pass 5: Body text margins**

Before classifying individual lines, the algorithm needs to know where the left margin, indent boundary, and right margin are. These define the classification thresholds.

It collects the first-ink x-position (`fi`) from every row that falls on a comb tooth and within a body-text zone. The 25th percentile of these positions gives the flush-left margin (`body_left`). The right margin (`body_right`) comes from the 90th percentile of last-ink positions. The indent boundary (`indent_x`) is set provisionally at `body_left + 5` and refined in Pass 6b.

**Pass 6: Per-tooth measurement and initial classification**

For each comb tooth (ie each line-height slot down the column), the algorithm measures:

- `dark` — peak darkness in a ±2px window centred on the tooth, using the 10% horizontal probe (middle 80% of column). This value indicates whether there is ink on this line.
- `spike_h` — the height of the darkness spike, measured as the number of consecutive rows above a threshold. Body text has consistent spike heights; headlines are much taller.
- `l_x` — leftmost ink position (how far the text is indented from the left edge)
- `r_x` — rightmost ink position (how far the text extends to the right edge)
- `centre` — peak darkness in the centre 20% of the column, used to detect centred headings or rules that don't reach the margins

Based on these measurements, each tooth is classified as one of:

|Class        |Meaning            |Detection criteria                                              |
|-------------|-------------------|----------------------------------------------------------------|
|`empty`      |No ink on this line|Peak darkness below ink threshold                               |
|`heading`    |Large/bold text    |Spike height exceeds dynamic threshold (2.5× median body spike) |
|`rule`       |Horizontal line    |Single very tall dark peak (heading-level) spanning just 1 tooth|
|`body_cont`  |Continuation line  |Flush left, extends to right margin                             |
|`body_indent`|Indented first line|Left edge past indent boundary, extends to right margin         |
|`body_short` |Short last line    |Flush left but ends well before right margin                    |
|`centred`    |Centred text       |Dark in centre but not at margins                               |
|`subheading` |Bold sub-heading   |Detected in Pass 7 by contextual analysis                       |

**Pass 6b: Indent threshold refinement**

The initial indent boundary from Pass 5 can be wrong if the flush-left margin cluster and the indent cluster overlap or if the percentile calculation lands in the wrong cluster. Pass 6b refines it using the actual l_x measurements from all body-height teeth.

It builds a histogram of l_x values, finds the flush-left peak (which dominates), walks rightward until the histogram drops to near-zero (the gap between the flush-left and indent clusters), and places `indent_x` at that gap. All teeth are then reclassified with the corrected threshold.

This was the last major fix applied to the algorithm and resolved a bug where columns with a flush-left margin near x=0 would fail to detect indents because the search started too far from the actual margin.

**Pass 7: Contextual fixes**

After initial classification, several context-dependent corrections are applied:

- **False short-line suppression** (7a): Two consecutive `body_short` lines where the first reaches 85%+ of the text width are reclassified — the first becomes `body_cont` since it was just a slightly narrow line.
- **Bridging** (7b): A single non-body tooth flanked by body text on both sides is reclassified as a body continuation, since it's likely a faint line rather than a genuine gap. Bridging does not cascade — if several consecutive teeth have been bridged, the gap is real.
- **Subheading detection** (7c): A bold line (above the strong-dark threshold) flanked by empty lines on both sides, and adjacent to a body_indent or body_cont, is reclassified as a subheading. Common in this newspaper for section labels like "The Lute:" or "The Clavichord:".
- **Local margin adaptation** (7d): Handles cases where the left margin shifts partway down the column (eg after a centred heading). Not yet fully implemented.

**Pass 8: Paragraph assembly**

The classified teeth are grouped into paragraphs. A new paragraph starts at:

- A `body_indent` tooth (the standard paragraph indent)
- A `body_cont` tooth following any non-body element (heading, empty, rule, subheading)

A paragraph ends when:

- The next tooth is `body_indent` (the current paragraph ends, the next begins)
- A `body_short` tooth is followed by a non-continuation tooth
- A heading, rule, empty, or subheading tooth is encountered

Paragraph boundaries are placed:

- At the **midpoint** between two body teeth when the transition is paragraph-to-paragraph (indent to indent, or short line to indent). This gives equal whitespace to both paragraphs.
- On a **split line** when the transition involves a non-body element (heading, rule, empty). The split is placed just above the non-body element so the heading "belongs to" the following paragraph.

### Output

The function returns a results dict containing:

- All measured parameters (period, phase, margins, thresholds)
- The full list of teeth with their measurements and classifications
- A list of paragraph objects with start/end y-coordinates and metadata about what caused each boundary
- A five-panel annotated chart showing the signal, 10% probe, 50% probe, classified column image, and paragraph boundaries

### Design decisions and lessons learned

**Why a comb template?** Early approaches tried peak detection on the darkness profile (finding individual text-line peaks by their prominence). This was fragile — headlines, sub-headings, and ads produce peaks at irregular heights and spacings that confuse the peak detector. The comb approach inverts the problem: instead of finding text lines, you find the rhythm and then check what's at each beat.

**Why binary thresholding?** The raw greyscale profile is noisy — scan artifacts, show-through, and binding shadows add low-level darkness that shifts the baseline. Binary thresholding (ink vs paper at pixel value 128) eliminates this noise. A pixel either has ink or doesn't. The resulting profile is much cleaner and the periodicity signal is stronger.

**Why separate measurement from classification?** Pass 6 measures l_x, r_x, dark, and spike_h independently of the classification thresholds. Pass 6b then analyses the l_x distribution to set the indent threshold correctly, and reclassifies. This two-pass approach prevents the threshold calculation from being contaminated by its own classification errors — a problem that caused incorrect indent detection in earlier versions.

**Why contextual fixes?** Classification based purely on per-tooth measurements produces false positives (faint body lines classified as empty, slightly short lines classified as paragraph-final) and misses subheadings (which look like headings in isolation but are structurally part of the body text flow). Contextual fixes catch these by looking at neighbouring teeth.

**Threshold derivation**: All classification thresholds are derived from the column's own statistics rather than being hardcoded. The ink threshold (`INK_THRESH`) is set as a fraction of the median body darkness. The heading spike threshold is 2.5× the median body spike height. The short-line cutoff is 75% of the text width. This makes the system robust across columns with different scan quality, ink density, or type size.

-----

## 4. Item-level segmentation (`find_splits.py`)

### What it does

Detects article-level boundaries within a column image using horizontal blur analysis. This is a coarser analysis than `four_probe_v5.py` — it finds the breaks between distinct items (articles, ads, notices) rather than between paragraphs within an item.

### How it works

1. **Calibration**: Analyses the column image to establish reference statistics — the body text darkness level, the typical body text darkness range, and the characteristic line height.
1. **Feature detection**: Applies a horizontal Gaussian blur (sigma=15) to collapse each row's darkness into a smooth profile, then detects:
- **Horizontal rules** — narrow dark peaks with whitespace gaps on both sides
- **Whitespace gaps** — extended regions where the blurred darkness drops below a calibrated threshold
- **Headline spikes** — very dark regions significantly exceeding the body text darkness level
1. **Boundary grouping**: Clusters adjacent features (a rule preceded by a whitespace gap, for example) into item boundaries, selecting the centre of the widest whitespace gap as the cut point.
1. **Item extraction**: Crops the column image at each boundary to produce individual item images.

### Calibration profile

The calibration data can be serialised to JSON for reuse:

```json
{
  "newspaper": "almonte_gazette",
  "issue": "1920-01-02",
  "dpi": 450,
  "columns": {
    "count": 7,
    "boundaries_vw": [16.8, 27.7, 38.3, 49.5, 60.4, 71.7, 82.2, 93.1]
  },
  "typography": {
    "body_line_height_px": 25,
    "body_darkness_range": [40, 80],
    "headline_darkness_min": 120,
    "rule_widths_pct": [30, 50, 62, 100]
  }
}
```

Within a single issue, the column grid, line height, and headline darkness thresholds are constant. Reusing the profile across pages within an issue tightens search ranges and reduces false positives. Across issues, the typographic constants (line height, headline sizes, rule widths) carry forward, while the column grid and margins need re-detection since binding position shifts between volumes.

### Relationship to four_probe_v5

`find_splits.py` and `four_probe_v5.py` operate at different scales:

- `find_splits` finds **inter-item** boundaries (where one article ends and the next begins, typically marked by a horizontal rule, a large whitespace gap, or a bold heading)
- `four_probe_v5` finds **intra-item** structure (paragraph breaks within a body of text)

In production, a likely workflow is to run `find_splits` first to identify items, then run `four_probe_v5` on each item's region to find its internal paragraphs.

-----

## 5. LLM classification (`classify_segments.py`)

### What it does

Sends each extracted item image to an LLM for semantic classification. The LLM determines what type of content each segment contains (article, advertisement, notice, continuation, etc.) and provides a brief summary. A `ColumnContext` object maintains state across columns to track articles that continue from one column to the next.

### Cross-column context

Newspapers routinely continue articles across columns and even across pages ("Continued from Page 1"). The `ColumnContext` object:

- Records which columns have been processed
- Tracks items flagged as unfinished (mid-sentence cutoffs, explicit "Continued on…" references)
- Generates a text prompt for the next column's LLM pass, informing it of what to look for
- Serialises to/from JSON for persistence between processes

```python
ctx = ColumnContext()
for col_num in range(1, 8):
    items = classify_column(col_num, segments, ctx)
# ctx now knows which articles span multiple columns
```

### Model tier strategy

Not all stages need the same model:

|Stage                  |Recommended model|Rationale                                         |
|-----------------------|-----------------|--------------------------------------------------|
|Column detection       |No LLM needed    |Pure pixel analysis                               |
|Item segmentation      |No LLM needed    |Pure pixel analysis                               |
|Boundary correction    |Haiku            |Quick visual check, low token cost                |
|Item classification    |Sonnet           |Needs to read content and make semantic judgements|
|Transcription          |Sonnet or Opus   |Character-level accuracy on small focused images  |
|Complex layout analysis|Opus             |Multi-column ads, unusual structures              |

Transcription should run on extracted item images, not on full column images. The item images are smaller (typically 400–800px tall at 450 dpi), so the LLM sees each line of type at higher effective resolution, reducing line-skipping and hallucinated repetitions.

-----

## 6. Boundary correction overlay (`boundary_overlay.py`)

### What it does

Generates an annotated column image with numbered horizontal lines at each candidate boundary position. This image is shown to an LLM correction agent alongside the candidate JSON, allowing the agent to confirm, adjust, or remove boundaries.

### Usage

```python
from boundary_overlay import generate_overlay

overlay_path = generate_overlay(
    column_image_path="col4.png",
    boundaries=[126, 229, 301, 450],
    output_path="col4_overlay.png",
)
```

### Design rationale

Automated boundary detection is good but not perfect. Rather than trying to make the detector handle every edge case, we generate overlay images that let a cheap LLM (Haiku) visually verify and correct the boundaries. This is faster and more accurate than exhaustive algorithmic tuning.

-----

## Key physical insights

These are properties of the printed newspaper that the algorithms exploit. They are not assumptions — they are measurements confirmed across multiple columns and pages.

**Body text is perfectly periodic.** The lead type and spacing material used in letterpress printing produce exactly regular line spacing. This is the single most reliable signal in the column. The autocorrelation peak at the body text period is always the strongest peak in the signal.

**The period is constant within a column.** A single column uses one type size for body text. The period may differ between columns in the same issue (eg an advertising column may use smaller type) but within a column it is fixed.

**Indentation is bimodal.** Body text lines are either flush-left (continuation lines) or indented (paragraph-start lines). The two clusters in the l_x histogram are cleanly separated by a gap. There are no intermediate values because the compositors used a fixed em-quad indent.

**Headings are taller than body text.** The spike height of a heading line is at least 2× the spike height of a body text line, because headline type is physically larger. This is the most reliable heading detector.

**Horizontal rules span a characteristic fraction of the column width.** The compositors used a small set of brass rules. The same rule widths (eg 30%, 50%, 62%, 100% of column width) recur throughout the newspaper. A centred dark spike that spans less than full width and has whitespace on both sides is almost certainly a decorative rule.

**Binding shadows are at the left or right edge.** Scans from bound volumes have a dark shadow along the gutter edge. This affects the first or last few percent of the page width but not the interior. Column images extracted with a 1% buffer contain some shadow at one edge, which the analysis handles by measuring ink positions from the binary image rather than the raw greyscale.

-----

## Processing estimates

Based on the test page (1920-01-02, page 3, 7 columns):

|Metric                        |Value                                      |
|------------------------------|-------------------------------------------|
|Column detection              |~2 seconds, no LLM needed                  |
|Column extraction (7 columns) |~5 seconds at 450 dpi                      |
|Paragraph analysis per column |~3 seconds, no LLM needed                  |
|Item classification per column|~20 seconds, ~2000 tokens per item (Sonnet)|
|Transcription per item        |~30 seconds, ~3000 tokens per item (Sonnet)|
|**Total per page**            |**~5–8 minutes**                           |
|**Total for 100,000 pages**   |**~1–2 years at current throughput**       |

The bottleneck is LLM cost and rate limits, not computation. The pixel analysis stages are fast. Cost optimisation should focus on minimising the number of LLM calls and using the cheapest adequate model tier for each call.

-----

## File inventory

|File                  |Role                                                 |LLM needed?       |
|----------------------|-----------------------------------------------------|------------------|
|`find_columns.py`     |Detect column boundaries from PDF                    |No                |
|`crop_pdf.py`         |Extract regions from PDF in flexible units           |No                |
|`find_splits.py`      |Detect article-level boundaries via h-blur           |No                |
|`four_probe_v5.py`    |Paragraph-level classification via comb template     |No                |
|`classify_segments.py`|LLM semantic classification with cross-column context|Yes (Sonnet)      |
|`boundary_overlay.py` |Generate correction overlay for LLM review           |No (but feeds LLM)|

All scripts depend on: `numpy`, `scipy`, `Pillow`. The PDF scripts additionally require `PyMuPDF` (`fitz`) and `pdf2image`.

-----

## Common pitfalls

**Don't trust OCR for positional information.** Standard OCR (Tesseract etc.) on these heritage scans produces garbled text with unreliable bounding boxes. The spatial analysis must work directly from the pixel data. OCR is essentially worthless for these documents.

**Don't blur before measuring ink positions.** Blurring shifts the apparent left and right edges of text lines, making indent detection inaccurate. All l_x and r_x measurements should be made on the binary (unblurred) image.

**Don't hardcode thresholds.** Every threshold in the system is derived from the column's own statistics. A threshold that works for one column may fail for another with different scan quality. The only hardcoded value is the binary threshold (128/255), which is robust because ink vs paper contrast is always high.

**Don't skip the phase alignment step.** Without correct phase, the comb teeth fall between text lines and every measurement is wrong. Phase alignment is cheap (one pass through period × body_zone_height values) and essential.

**Don't classify before measuring.** The indent threshold depends on the l_x distribution, which depends on which teeth are measured, which depends on the spike-height threshold. Measure everything first, then derive thresholds, then classify. Mixing measurement and classification creates circular dependencies.

**Don't assume the first peak in a histogram is the right one.** Smoothing a histogram can obliterate narrow peaks (like a flush-left margin cluster at x=0) while preserving broad ones (like an indent cluster at x=25). Use minimal smoothing (kernel=3) when the target peak is narrow.

-----

## Future work

- **Integration**: Combine `find_splits` (article boundaries) with `four_probe_v5` (paragraph boundaries) into a single pipeline that produces a complete structural decomposition of each column.
- **Profile reuse**: Build the calibration profile capture into the pipeline so that first-page measurements accelerate subsequent pages.
- **Ad detection**: Advertisements have distinct darkness profiles (often much denser than body text, with display typefaces). A classifier that identifies ad zones early would let the pipeline skip paragraph analysis on those regions.
- **Multi-column elements**: Some articles and ads span 2+ columns. Detecting these requires comparing adjacent columns' boundary positions — an item that starts at the same y-position in two adjacent columns and has no rule between them is likely a multi-column element.
- **Key-pages-first strategy**: Process the most content-rich page from each issue first, providing complete temporal coverage before filling in remaining pages.
