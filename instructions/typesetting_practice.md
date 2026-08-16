# How these pages were actually made

**Thinking like a typesetter is key to this project.** A newspaper page
is a designed, quantised artefact assembled on a fixed grid — not an
unknown natural signal to be discovered. Treating it as the latter leads
to far more complex machinery than the problem deserves. Read this
before designing any layout detector.

Scope: the Almonte Gazette, with the 1980s–1990s in mind, though most of
it holds much earlier too.

---

## The grid was physical and pre-existing

Through most of this period a community weekly was still **paste-up**:
galleys of phototypeset text waxed onto a pasteboard that already had the
column grid printed on it in **non-reproducing blue**. The compositor did
not invent boundaries — they aligned to lines that were literally on the
board.

When production moved to Mac with PageMaker or QuarkXPress (widely from
about 1987), the same thing became **master-page column guides with
snap-to**. Either way: **the grid is an input to the page, not an
outcome of it.**

## It is four numbers, fixed for the whole issue

- left margin
- column width
- gutter
- column count

Measured in **picas and points**, not proportions — 1 pica = 12 points =
⅙ inch. Column widths sat in a narrow band (roughly 11–13 picas
broadsheet, wider on tabloid); gutters were almost always 1 pica. These
were set by the paper's format and changed maybe once a decade, usually
at a redesign.

## Everything is an integer number of columns

This is the hard constraint. A photo, an ad, a story block is 1, 2 or 3
columns wide — **never 1.5**. The width isn't a design choice, it's
arithmetic:

```
width = n × col + (n − 1) × gutter
```

So every left and right edge on the page is predictable from those four
numbers.

## Ads made it quantised commercially, not just aesthetically

Display advertising was sold by the **column inch** — the rate card's
unit is literally column-width × inches deep. A "2×5" is two columns
wide, five inches deep. Ad dimensions are therefore quantised at the
point of sale, before anyone lays anything out.

## Ads go down first; news fills what's left

Standard practice is to **dummy the ads before any editorial**: stacked
from the bottom, typically in a pyramid or stepped stack rising to one
side, so every ad touches an outside edge or the bottom. What remains is
the **news hole**, which the editor fills.

That is why the bottom third of a page is often solid advertising while
the top is editorial — and why the *usable text area changes shape down
the page*.

## By the 1980s, modular layout had won

The older practice of wrapping a story around obstacles in dog-legs and
L-shapes was deliberately reformed away. The rule became: **every story
is a rectangle.** A story occupies *n* legs of text, and its headline
spans exactly those legs.

So a page is a **packing of rectangles onto a fixed grid**. This is the
same conclusion `layout_observations.md` reaches from the corpus side
("modular"), arrived at independently from the design side.

## Type came from a fixed menu

Body text is one size throughout, typically around 9pt on 10pt leading,
in one face. Headlines come from a **headline schedule** — a defined set
of sizes and weights (18, 24, 30, 36, 48, 60, 72pt), not arbitrary
values. Subheads, bylines, captions and classified type each have their
own fixed spec.

There are only a handful of distinct type sizes on any page, and they
repeat across the whole run.

## Separation is by rule and white space

Cutoff rules between stacked stories; column rules where house style used
them; plus standing furniture that never moves — flag/masthead, folio
line, section headers, jump lines ("continued on page N").

---

## Why this matters for detection

The consequence, stated plainly: **layout is not a per-page mystery to be
discovered. It is a small set of constants, and everything else snaps to
them.** A deviation from the grid is far more likely to be OCR noise, a
photo, or scan distortion than a genuine new column width.

Two corollaries worth holding onto:

- Pages that look "gridless" usually aren't. A modular page is the *same*
  grid with rectangles packed onto it; full-height gutters simply don't
  survive an ad stack.
- Because measurements are quantised, a detector should be **fitting a
  small number of parameters**, not clustering freely and scoring its
  confidence in each independent guess.

## What the RULES tell you, and why not to "fix" them

The ruling on the page is physical, laid down by a compositor, and reads
correctly only if you ask what was done rather than what an OCR engine
got wrong.

- **A column rule is ONE continuous strip.** It runs the length of the
  gutter and ads butt against it; it does not restart at each ad
  boundary. So a separator spanning several stacked boxes is an accurate
  reading, not a merge error. Attempting to break such a rule into
  per-box segments invents gaps that were never in the ink — tried on
  1980-04-06 p13, and it only destroyed real boxes.
- **A double-ruled border is two rules with a set gap**, measured at
  0.71–2.15% of page width (one to two picas) across 1980-04-06. It
  reports as two concentric rectangles; the item extends to the OUTER.
- **Rounded corners mean the sides never meet.** Measured inset 0.5% on a
  plain box and 2.5–3.9% on an ornate one. A corner is therefore where
  the two rules' AXES cross, not where their ends land.
- **A drop shadow prints the shadowed sides heavier.** One box measured
  28px on top against 48px at the bottom. Side weight is therefore
  evidence about the design, never a consistency test.
- **A fragmented rule** is one where something was pasted over it, or the
  scan lost a stretch. Those pieces belong back together.

The general rule: a surprising feature of the ruling is far more likely
to be a production fact than an error, and "correcting" it removes real
structure.

## Update history

- **2026-08-15** — Added "What the RULES tell you", recording the print
  facts behind the ruling: continuous column rules, double borders,
  rounded corners, drop shadows, fragmented rules. Written after an
  attempt to break a continuous column rule into per-box segments
  regressed box detection — it was undoing something the compositor
  actually did.
- **2026-08-15** — Created. Written after the observation that the
  detection methodology had become far too complex for something this
  regular.
