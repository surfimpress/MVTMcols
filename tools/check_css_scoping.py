#!/usr/bin/env python3
"""Guard against our CSS leaking into embedded third-party components.

Why this exists
---------------
2026-08-15: `preview/scaled/iiif/viewer.html` styled its own toolbar with
bare `select {}`, `body { font }` and `* { box-sizing }`. Those rules
also matched TIFY's *internal* controls, pushing its page-selector below
the image on mobile. The viewer looked broken; the viewer was fine. We
broke it from the outside.

That is a whole class of bug, not a one-off, and it is invisible until
someone opens the page on a real device. This catches it at author time.

Scope of the rule
-----------------
It applies ONLY to pages that mount a third-party UI component (a IIIF
viewer, a charting lib, etc). On a self-contained page like
`transcribe/entities.html` there is nothing to leak into and bare element
selectors are perfectly correct -- those files are skipped entirely.
A blanket ban would be wrong, so this does not impose one.

What it forbids, in an at-risk file
-----------------------------------
1. Universal selectors (`*`, `*::before`) -- they match everything the
   third-party component renders.
2. Bare type selectors (`select {}`, `button {}`, `a:hover {}`) --
   qualify them with your own id/class, e.g. `#chrome select {}`.
3. Inheritable declarations on `html`/`body` (font, color, line-height,
   letter-spacing, ...) -- children inherit them into the component.
   Layout-only properties on html/body are allowed, since a full-height
   flex shell is the normal way to host a viewer.

Usage::

    python3 tools/check_css_scoping.py                 # all tracked html
    python3 tools/check_css_scoping.py path/to/x.html  # specific files
    python3 tools/check_css_scoping.py --list          # show at-risk files

Exit code 1 if any violation is found, so it can gate a commit.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

# A file is "at risk" if it pulls in a third-party UI component.
THIRD_PARTY_MARKERS = (
    "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com",
    "esm.sh", "skypack.dev", "googleapis.com/ajax",
)

# Inheritable properties: set these on html/body and every descendant --
# including the third-party component's internals -- picks them up.
INHERITABLE = {
    "font", "font-family", "font-size", "font-weight", "font-style",
    "font-variant", "line-height", "letter-spacing", "word-spacing",
    "color", "text-align", "text-transform", "text-indent",
    "white-space", "visibility", "cursor", "list-style",
}

# Layout-only properties that are fine on html/body: hosting a viewer in a
# full-height flex shell is the normal pattern and leaks nothing.
BODY_ALLOWED = {
    "margin", "padding", "height", "min-height", "max-height",
    "width", "min-width", "max-width", "display", "flex-direction",
    "flex", "overflow", "overflow-x", "overflow-y", "background",
    "background-color", "position", "inset", "top", "left", "right", "bottom",
    "box-sizing", "color-scheme", "gap", "align-items", "justify-content",
}

_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.S | re.I)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
# A type selector is a bare tag name at the start of a compound selector.
_TYPE_SEL_RE = re.compile(r"^[a-z][a-z0-9]*\b", re.I)

HTML_TAGS = {
    "a", "abbr", "address", "article", "aside", "audio", "b", "blockquote",
    "body", "button", "canvas", "caption", "cite", "code", "col", "dd",
    "details", "dialog", "div", "dl", "dt", "em", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "hr", "html", "i", "iframe", "img", "input", "kbd", "label", "legend",
    "li", "main", "mark", "menu", "meter", "nav", "object", "ol", "option",
    "output", "p", "picture", "pre", "progress", "q", "s", "samp", "section",
    "select", "small", "span", "strong", "sub", "summary", "sup", "table",
    "tbody", "td", "textarea", "tfoot", "th", "thead", "time", "tr", "u",
    "ul", "video",
}


def is_at_risk(text: str) -> bool:
    return any(m in text for m in THIRD_PARTY_MARKERS)


def _split_selectors(sel: str) -> list[str]:
    return [s.strip() for s in sel.split(",") if s.strip()]


def _first_compound(sel: str) -> str:
    """Leading compound of a selector, e.g. '#chrome select' -> '#chrome'."""
    return re.split(r"[\s>+~]+", sel.strip(), maxsplit=1)[0]


def check_css(css: str, line_offset: int) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    css_nc = _COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), css)

    pos = 0
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_nc, re.S):
        selector_raw, body = m.group(1), m.group(2)
        line = line_offset + css_nc[:m.start(1)].count("\n") + 1

        sel = selector_raw.strip()
        # Skip at-rule preludes (@media/@supports/@keyframes wrappers).
        if sel.startswith("@") or not sel:
            pos = m.end()
            continue
        # Inside @keyframes the "selectors" are percentages/from/to.
        if re.fullmatch(r"(from|to|[\d.]+%)", sel, re.I):
            pos = m.end()
            continue

        for one in _split_selectors(sel):
            head = _first_compound(one)

            if head.startswith("*"):
                problems.append((line, f"universal selector `{one}` matches "
                                       f"third-party markup — scope it"))
                continue

            if one in (":root",) or head == ":root":
                continue

            tag = _TYPE_SEL_RE.match(head)
            tag_name = tag.group(0).lower() if tag else None
            if tag_name not in HTML_TAGS:
                continue  # class/id/attribute-led — properly scoped

            if tag_name in ("html", "body"):
                for decl in body.split(";"):
                    if ":" not in decl:
                        continue
                    prop = decl.split(":", 1)[0].strip().lower()
                    if not prop or prop.startswith("--"):
                        continue
                    if prop in INHERITABLE:
                        problems.append((line, f"`{one} {{ {prop} }}` is inheritable — "
                                               f"third-party children will inherit it"))
                    elif prop not in BODY_ALLOWED:
                        problems.append((line, f"`{one} {{ {prop} }}` — only layout "
                                               f"properties belong on html/body here"))
                continue

            problems.append((line, f"bare type selector `{one}` — qualify it with your "
                                   f"own id/class (e.g. `#chrome {one}`)"))
        pos = m.end()
    return problems


def check_file(path: str) -> tuple[bool, list[str]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not is_at_risk(text):
        return False, []
    msgs = []
    for sm in _STYLE_RE.finditer(text):
        offset = text[:sm.start(1)].count("\n")
        for line, msg in check_css(sm.group(1), offset):
            msgs.append(f"{path}:{line}: {msg}")
    return True, msgs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="html files (default: all in repo)")
    ap.add_argument("--list", action="store_true",
                    help="just list which files are at risk and exit")
    args = ap.parse_args()

    paths = args.paths or sorted(
        p for p in glob.glob("**/*.html", recursive=True)
        if "node_modules" not in p and "/venv/" not in p)

    at_risk, all_msgs = [], []
    for p in paths:
        if not os.path.isfile(p):
            continue
        risky, msgs = check_file(p)
        if risky:
            at_risk.append(p)
            all_msgs.extend(msgs)

    if args.list:
        print(f"{len(at_risk)} file(s) embed third-party UI components:")
        for p in at_risk:
            print(f"  {p}")
        return 0

    print(f"checked {len(paths)} html file(s); {len(at_risk)} embed third-party components")
    if not all_msgs:
        print("no CSS scoping problems found")
        return 0
    print(f"\n{len(all_msgs)} problem(s):\n")
    for m in all_msgs:
        print(f"  {m}")
    print("\nFix by scoping to your own id/class. See this file's docstring "
          "for why (TIFY regression, 2026-08-15).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
