# CSS standard for pages that embed third-party UI

Short version: **when a page mounts a third-party component (a IIIF
viewer, a chart lib), every rule you write must be scoped to your own
markup.** Enforced by `tools/check_css_scoping.py`.

## The failure this prevents

2026-08-15, `preview/scaled/iiif/viewer.html`. A four-line toolbar was
styled with:

```css
* { box-sizing: border-box; }
body { font: 13px/1.3 system-ui; }
select, a.btn { font-size: 12px; padding: .18rem .4rem; border: 1px solid ...; }
```

All three matched TIFY's *internal* DOM. TIFY's own page-selector picked
up the border/padding/font and its header layout collapsed, dropping the
control below the image on mobile. **The viewer looked broken; the viewer
was fine.** We broke it from the outside, and it was only visible on a
real phone — no error, no console warning, nothing in a desktop check.

This is a class of bug, not a one-off: any inheritable property or
unqualified selector reaches into every component you embed.

## The rules

Only for files that load a third-party UI component (detected by
`cdn.jsdelivr.net`, `unpkg.com`, `cdnjs`, `esm.sh`, `skypack`).
**Self-contained pages are exempt** — `transcribe/entities.html` has
nothing to leak into, and bare element selectors there are correct. A
blanket ban would be wrong.

1. **No universal selectors.** `*`, `*::before` match everything the
   component renders.
2. **No bare type selectors.** `select {}`, `button {}`, `a:hover {}`
   → qualify with your own id/class: `#chrome select {}`.
3. **Nothing inheritable on `html`/`body`.** `font`, `color`,
   `line-height`, `letter-spacing`, `text-align`, `cursor`,
   `white-space`, `visibility`, `list-style` — children inherit these
   straight into the component. Put them on your own container instead.
   *Layout* properties on html/body are fine (`height`, `display`,
   `flex-direction`, `margin`, `overflow`, `background`): a full-height
   flex shell is the normal way to host a viewer.
4. **Give your chrome one id and hang everything off it.** The fixed
   file uses `#chrome` for the toolbar and `#stage` for the viewer
   container; every rule starts with one of those.
5. **State the rule in the file.** The `<style>` block opens with a
   comment saying why no unscoped selectors are allowed, so the next
   person doesn't "tidy" them back in.

## Enforcement

```bash
python3 tools/check_css_scoping.py            # all html in the repo
python3 tools/check_css_scoping.py --list     # which files are at risk
python3 tools/check_css_scoping.py path.html  # one file
```

Exit code 1 on any violation, so it can gate a commit. No dependencies —
**Node/npm are not installed on this machine**, so Stylelint (whose
`selector-max-type` rule would cover point 2) is not an option here; this
is a small dependency-free stand-in, not a replacement for a real linter
if Node ever arrives.

**The checker was validated against the actual bug**, not just written
and assumed correct: run against the pre-fix commit of `viewer.html` it
reports all 8 real violations including the `select` rule that caused the
visible breakage; against the fixed file and the two existing
`mirador.html` pages (which already use a scoped `.topbar` pattern) it
reports clean.

## Wider quality notes for these pages

- **Test on a real phone**, or at least a narrow viewport. This bug was
  invisible at desktop width.
- **Don't restyle a third-party component to taste.** If a viewer's own
  UI needs changing, use its documented config (TIFY exposes `viewer`
  for OpenSeadragon; Mirador has a theme API) — not a CSS override that
  will break on their next release.
- **Respect `prefers-color-scheme`** via custom properties on `:root`,
  which is exempt from the scoping rule because it defines variables
  rather than matching elements.
- **Use `env(safe-area-inset-*)`** for fixed chrome on notched phones.

## Update history

- **2026-08-15** — Created after the TIFY layout regression described
  above. `tools/check_css_scoping.py` added and validated against the
  known-bad commit.
