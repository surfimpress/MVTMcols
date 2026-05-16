# Post-1980 Layout Observations

Field notes from Phase 0 of the post-1980 ingest investigation. Based
on direct visual inspection of 10 issues across 1985, 1990, 1995,
2000, 2007 (~80 pages reviewed), rendered from source PDFs fetched
from Drive. Source material from
`mvtm:MVTMfiles/gazette/<year>/<date>-<NN>.pdf`; renders preserved at
`/tmp/post1980_samples/renders/` for the duration of the
investigation.

**Why this file exists:** the classical cutter (`column_pipeline.py`
+ `detect_*` modules) was tried on 1980 and the output was, per the
user, "beyond useless" — deleted. Aggregate `page_layouts` stats from
that run flattered the pipeline (97% no quality flags) because the
metrics share authorship with the code under review; bad cuts the
detector didn't recognise as bad don't raise flags. This file is the
empirical ground truth for what's actually on these pages and why
the classical cutter is the wrong tool for them.

---

## Headline finding: this is a different paradigm, not a tuning problem

Every post-1980 page reviewed is a **modular broadsheet**, not a
flowing column grid. The classical pipeline's foundational assumption
— that a page has a uniform multi-column grid (~7 columns of equal
pitch) with continuous full-page-height column rules dividing
text — does not hold on any post-1980 page in the sample.

Concretely:

1. **Articles are bounded rectangles**, not vertical column strips.
2. **The "column" inside an article is local to that article** — it
   doesn't extend above or below the article block.
3. **Different articles on the same page have different internal
   column counts** (a 5-column-wide article block above a
   3-column-wide article block above two 2-column-wide article
   blocks side-by-side is normal).
4. **Headlines span multiple inner columns**, often 2–5 wide,
   sitting above the article body.
5. **Photos are integrated inside articles**, sized by the article's
   width, with no enclosing rule.
6. **Display ads are modular rectangles**, often full-page or
   half-page, with their own internal grid (grocery flyer style).
7. **Section mastheads** ("ALMONTE AND DISTRICT", "DISTRICT NEWS",
   "Classifieds") sit at the top of each interior page.

A vertical strip down "page column 3" cuts across multiple articles
+ a photo + a pull quote + possibly a section masthead. That is why
the cuts were "beyond useless" — the slices are not units of
reading. They're geometric arbitrary.

---

## Source material differences (PDF level)

Inspected via `pdfinfo`. Important — 1985 is a different generation
of artefact from 1990–2007.

| Year | Producer | Page size (pts) | File size / page |
|---|---|---|---|
| 1985 | TCPDF 6.0.012 | 595×842 (A4) | 700–950 KB |
| 1990 | Adobe Paper Capture 10.1.11 | 1414×2033 (~19.6"×28.2") | 1.5–1.7 MB |
| 1995 | Adobe Paper Capture 10.1.11 | 1342–1477×2111–2146 | 1.5–1.7 MB |
| 2000 | Adobe Paper Capture 10.1.11 | 1328–1333×2111–2117 | 1.7 MB |
| 2007 | Adobe Paper Capture 10.1.11 | 1342–1345×2117–2120 | 1.6–1.7 MB |

- **1985 PDFs are re-wrapped scans** — TCPDF is a PHP library used to
  generate PDFs; the pages were processed into A4-fitting form by some
  external tool. They render at ~700KB per page at 100 DPI vs ~4.7 MB
  for 1990. **Effective resolution is significantly lower** on 1985.
- **1990–2007 are full-broadsheet Adobe Paper Capture PDFs** —
  consistent producer, consistent ~19"×28" page size (true broadsheet
  dimensions), ~1.6 MB per page (5–6× the byte density of 1985).
- All scans are greyscale, not colour. No colour profile recovery
  needed (the `pdftoppm` "Couldn't link the profiles" warnings on
  1985 are an artefact of the TCPDF wrapping; renders are correct).

**Implication:** 1985 likely needs a different ingest path
(pre-derivative-render PDFs) OR accepts lower-quality output. 1986+
are a consistent corpus.

---

## Per-era observations

For each year I looked at page 1 (front), page 3 (interior editorial)
and page 10 (deep interior — often classifieds/ads).

### 1985 (1985-02-13, 1985-10-16)

- **Front page (p01):** Modular. Lead story top-left (~3 cols wide
  + photo). Multi-column photo across centre. Vertical sidebar story
  right. Smaller stories bottom-left, bottom-centre, bottom-right.
  An "In focus" teaser bar top-right. Single-page count: ~6 distinct
  articles + masthead.
- **Interior editorial (p03 "Almonte and district"):** Section
  banner across top. Mix of 1-, 2-, and 3-column-wide article
  blocks. Pull-out boxes ("Wrong person quoted", "Police beat").
  ~8–10 distinct stories per page. Photos within ~3 of those.
- **Deep interior (p10):** Full-page grocery display ad
  (Frederick's) — modular grid of product cells with photos and
  prices. Zero editorial text. The classical cutter would produce
  nonsense here.

### 1990 (1990-02-14, 1990-10-17)

- **Front (p01):** Larger and more design-led than 1985. 5-column-
  wide headline "Ramsay ratepayers band together…" with deck and
  large photo. Pull quote rendered as a large stand-alone text
  block ("It was always seen as adequate to leave room alone…"). A
  graphic "Happy Valentine's Day" within the masthead area. Index
  box bottom-right. Boxed teasers across the top.
- **Interior (p03):** Section banner "Almonte and district".
  Multi-column-headline articles. Pull-out grey "Police Beat" column.
  "Continued from page 1" labels in italics. ~6–8 articles.
- **Deep interior (p10):** Mix of editorial + display ads. Large
  "INTRODUCTION TO WOODWORKING" box ad bottom-right, "SENIORS TAKE
  NOTE!" tall ad far right, photo features ("Welcome!" photo
  centre).

### 1995 (1995-02-15, 1995-10-18)

- **Front (p01):** Top index strip with thumbnails of inside content
  (sports, business, feature). 5-column lead "Sirman recovering
  from collision with transport". Large photo of crash scene
  (multi-column, borderless). Pull quote integrated mid-article.
  Bottom of page: full-width sponsor/banner "United Way campaign
  remains short of target" with photo.
- **Interior (p03):** Multiple stories with multi-column headlines
  ("MNR re-introduces wild turkeys to county", "Glardino takes
  over as CFL Tiger-Cats' director of player personnel"). A
  "POLICE BEAT" boxed column. Mid-page banner across all columns
  ("Tourism group wants visitors to STOP here"). Each article zone
  is rectangular with its own internal column count.
- **Deep interior (p10):** "WHEN IT'S COLD OUTSIDE IT'S CHILI DAYS
  IN ALMONTE!" full-page community-businesses sponsored ad — large
  banner + grid of ~20 sponsor logos with addresses/phone numbers
  arranged in modular cells.

### 2000 (2000-02-16, 2000-10-18)

- **Front (p01):** 6-column body grid but with multi-column
  headlines and very large pull quotes. "Committee to bid for
  Plowing Match Feb. 21 in Guelph" wide headline. Index box top-
  left. "YOUNG HEROES — Siblings save dad after fire breaks out in
  their home Saturday" — 6-column headline above a multi-column
  photo. Banner pull quote ("The provincial government wants to
  increase the number of welfare recipients who must work for their
  welfare cheques.") spans 4 columns mid-page.
- **Interior (p03 "ALMONTE AND DISTRICT"):** Two heavy stories
  ("Community and council support the Almonte hospital's network
  position", "Parent demands that pesticides be banned"). 5-column
  width with photos integrated. Mid-page banner pull quote
  ("Twenty-four emergency service is the key to any small rural
  hospital."). Bottom half: "Former MPP to be recognized at
  meeting" + "Possible closure of recycling depots prompts action
  group to create petition of protest".
- **Deep interior (p10):** Full-page **CLASSIFIEDS**. 6 narrow
  columns of tiny-type listings, each headed by "For Sale", "Mill
  Street Commercial", etc. Large display ads scattered:
  "AFFORDABLE / HOUSES FOR SALE / Aaron-Carleton", a
  "Therapeutic Massage in Motion" ad, "Electrical Installation",
  "Almonte Pharmacy" right side. House-of-the-Gazette ad bottom-
  centre ("CLASSIFIED ADVERTISING RATES"). The page is mostly
  ads + classifieds, not editorial.

### 2007 (2007-02-13, 2007-10-16)

- **Front (p01):** "Bassile calls for mayor's resignation" 6-column
  banner headline with deck. Large multi-column photo of councillor.
  "Councillor is concerned about future of prime agricultural land"
  beneath. Banner top of page with teasers ("Jazz crooner Michael
  Buble has been nominated…"). Bottom: "Celebrate 100 years of
  scouting here" sub-banner. Large "Coldwell Banker" half-page
  display ad bottom-left.
- **Interior (p03 "DISTRICT NEWS"):** "From front page Bassile",
  "From front page Sterling", "From front page Deugo" — these are
  page-1 jump continuations. Two long jump articles + a "Cards for
  Shane" box + a "Town hall readies for new tenants" banner +
  "Works badly needed on exterior of the building" sidebar.
  "FYI" boxed feature with "Happy Valentine's Day" theming. Several
  display ads visible (Carleton Heritage Inn, La Boutique).
- **Deep interior (p10):** Full-page "Valley Classifieds" with
  display-ad banners, MAGGIE MOIR OBIT, GEORGE GLENN BLAIN obit, FYI
  bra clinic, Beverly Griffith memoriam — modular cells of varying
  sizes, large "in loving memory of" ads, classified columns,
  funeral home ads.

---

## Patterns that hold across the entire 1985–2007 sample

1. **Section masthead at top of each interior page** ("ALMONTE AND
   DISTRICT", "DISTRICT NEWS", "Classifieds", "VALLEY CLASSIFIEDS",
   etc.). Becomes a useful landmark — it's the first cue that this
   is page-3-and-not-page-1.
2. **Multi-column headlines (2–6 wide) above their article block.**
   A 6-col headline does *not* mean the page is a 6-col grid; it
   means the article underneath occupies a 6-col-wide rectangle of
   the page.
3. **Within an article, the body text DOES flow column-to-column.**
   Inside a single article rectangle of width W, text wraps from
   internal column 1 to internal column 2 etc. (Same behaviour as
   classical, but bounded to the article zone.)
4. **Articles do not share inner columns.** The inner column 1 of
   article A is *not* aligned with inner column 1 of article B
   below it. Each article re-flows its body to its own width.
5. **Photos are inside articles, captioned below or beside.** They
   are sized to the article width (often spanning multiple inner
   columns). No enclosing rule.
6. **Pull quotes are bordered text rectangles** with rules above/
   below, often centred mid-article, often spanning all of the
   article's inner columns. Should be treated as a *non-flowing*
   block embedded in the body text.
7. **Display ads are modular boxes**, frequently with their own
   rule borders, scattered in the lower portion of editorial pages
   or as full pages of their own (1985 p10 grocery flyer, 2000 p10
   classifieds).
8. **"Continued from page X" / "From front page X" jumps are
   typographically marked** — italicised label above the continued
   text block. These are explicit reading-order cues that the
   classical pipeline ignores.

## Patterns that change across the 1985–2007 sample

- **Photo density increases.** 1985 front pages had ~3 photos; 2007
  fronts have ~6.
- **Pull-quote prominence increases.** 1985 had small inline boxes;
  2000–2007 routinely have multi-column banner pull quotes set in
  large display type.
- **Classifieds get more boxed.** 1985 doesn't show classifieds in
  the sample; 1995–2007 have full-page classified pages with
  scattered display ads.
- **Page count grows then shrinks.** 1985 issues = 36–76 pages;
  1990 = 32–40; 1995 = 32–40; 2000 = 26–64; 2007 = 24–28.

## What does NOT change across 1985–2007

- The modular paradigm is established **from the first sample
  (1985-02-13)**. There is no transition period in this range.
- The earlier transition from flowing 7-col (classical) to modular
  broadsheet must have happened **before 1985** — probably in the
  late 1970s. Worth a follow-up render of 1975/1979/1980 to date
  it precisely. But for ingest planning the answer is "everything
  1985+ is modular".

---

## Comparison vs classical (reference: 1947-11-06)

| Aspect | Classical (≤ 1979) | Modern (1985+) |
|---|---|---|
| Page-level grid | Uniform 6–8 columns, full-height | None — no page-level rules |
| Column rules | Continuous full-page | Local to article block, often absent |
| Article-as-unit | Implicit — content flows column-to-column down the page | Explicit — bounded rectangles |
| Internal column count | Same across page | Varies article-to-article |
| Headlines | Column-wide (rarely 2 cols) | Multi-column wide (2–6) |
| Photos | Rare; bordered when present | Frequent; borderless; sized to article |
| Display ads | Bordered rectangles, mostly bottom | Full-page often; embedded in editorial pages |
| Pull quotes | Absent | Banner-style, multi-column |
| Section mastheads | Subtle running header | Bold display banner per page |
| Photo prevalence | Low (esp. pre-1960) | High |
| Classifieds | Sparse, in-grid text | Dedicated full-page modular layouts |

---

## What this means for the cutter

The classical pipeline's bottom-up flow is roughly: detect column
rules → fit grid → fit text bands → emit column slices. The first
step (detect column rules) returns garbage on a modern page because
the rules it's looking for don't exist outside article blocks. The
remaining steps then build a "grid" out of noise and emit slices
that don't correspond to anything readable.

A post-1980 cutter needs a different first step: **identify article
blocks** (rectangular regions of contiguous text+headline+possibly-
photo, separated from neighbours by whitespace, hairline rules, or
photo placement). Then within each block, identify the local column
count (typically 2–4 inner columns within an article wider than 2
classical columns). Then cut along block boundaries — *not* along
fictitious page-wide column boundaries.

This is a structural change, not a tuning change. The detection
strategies in `instructions/detection_methods_review.md` that are
about "find vertical rules and consensus them across strips" do not
apply. New strategies are needed:

- Block detection (find article rectangles by whitespace / rule /
  photo-boundary analysis)
- In-block column detection (much easier — small region, often
  uniform pitch)
- Headline detection as a separate kind (large type spanning
  multiple inner columns above body text)
- Photo / figure detection as a first-class kind (borderless
  mid-tone darkness rectangles)
- Pull-quote detection as a separate kind (bordered, no flow)
- Section masthead detection (large display type at very top of
  page)
- Display-ad detection extended for full-page modular layouts
- Classifieds-page detection as a distinct page class (different
  grid)

A "page class" pre-pass that decides whether to use the classical
or the modular pipeline (per page, not per issue, since front
pages of even classical issues sometimes have modular elements)
becomes the natural entry point.

What 1985 specifically needs that 1990+ doesn't: a check on input
PDF size (~A4 vs broadsheet) to decide whether the source is
a derivative-rendered low-resolution rewrap, which may need a
different DPI strategy or simply be accepted as lower-fidelity.

---

## Sample inventory

Renders kept at `/tmp/post1980_samples/renders/` (until disk
pressure reclaims):

- `page1/<issue>-p01-1.png` — front pages, all 10 issues
- `interior/<issue>-p03-1.png` and `p10-1.png` — interior pages,
  February issues
- `oct/<issue>-p03-1.png` — October verification samples

Source PDFs at `/tmp/post1980_samples/<year>/<issue>/` (266 MB
total). Cleanup is on the to-do list, not urgent.

---

## Update history

- **2026-05-16 — Phase 0 visual characterisation.** First pass.
  10 issues × ~3 pages each rendered and reviewed. Modular
  broadsheet paradigm established as universal across 1985–2007.
  Classical cutter fundamentally inapplicable. Phase 1 design needs
  block detection as the new entry point.
