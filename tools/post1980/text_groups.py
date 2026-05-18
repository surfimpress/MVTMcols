"""Headlines, pull-quotes, body-column groupings within a page.

These three categories of "what is this text run" are the building
blocks of article assembly. Critical distinction the v2 prototype
missed: pull-quotes are intermediate-size text *flanked by body
above and below* — they do NOT divide articles. Headlines are
intermediate-or-larger text *with body only below* — they DO start
a new article.
"""
from dataclasses import dataclass, field
from typing import List

from .spans import Span


@dataclass
class HeadlineRun:
    """A merged run of headline spans on the same article."""
    x0: float; y0: float; x1: float; y1: float
    size: float
    text: str

    @property
    def bbox(self): return (self.x0, self.y0, self.x1, self.y1)

    @property
    def w(self): return self.x1 - self.x0


@dataclass
class BodyColumn:
    """A vertical run of body-size text — one inner column."""
    x0_min: float; x0_seed: float; x1_max: float
    y0: float; y1: float
    spans: List[Span] = field(default_factory=list)

    @property
    def width(self): return self.x1_max - self.x0_min


def real_headlines(spans, body, body_spans, min_y):
    """Spans that are >= 2× body, sit below the masthead band, and
    have at least 5 body-size spans starting within ~150pt below
    them within their x-extent.

    The body-below requirement is what kicks out:
      - masthead furniture (no body text directly below)
      - pull quotes embedded inside articles (body extends both
        above and below them — the test below catches the body
        ABOVE incidentally, since we don't check "body above"
        explicitly; pull-quote refinement is in is_pull_quote)
    """
    candidates = [s for s in spans
                  if s.size >= body * 2.0 and s.y0 >= min_y]
    real = []
    for h in candidates:
        below = [b for b in body_spans
                 if b.y0 > h.y1 - 2
                 and b.y0 < h.y1 + 150
                 and min(b.x1, h.x1) - max(b.x0, h.x0) > 15]
        if len(below) >= 5:
            real.append(h)
    return real


def is_pull_quote(h, body_spans, body):
    """Is this 'headline' actually a pull-quote embedded inside an
    article? Heuristic: intermediate size (1.3-1.99× body) AND body
    text both above AND below within 150pt within the span's x-range.

    Upper bound is < 2.0× body: anything at 2× body or larger is
    treated as a real headline, even when body wraps around it.
    Empirically: jump-article headlines on interior pages (like 'Cards
    for Shane' on 2007 p3) sit at 2.0-2.15× body with body text
    closely above (the previous article ending) and below (their own
    body); the previous 2.5× cap was misclassifying them as pull
    quotes.
    """
    if h.size >= body * 2.0 or h.size < body * 1.3:
        return False
    above = [b for b in body_spans
             if b.y1 < h.y0 + 2 and b.y1 > h.y0 - 150
             and min(b.x1, h.x1) - max(b.x0, h.x0) > 15]
    below = [b for b in body_spans
             if b.y0 > h.y1 - 2 and b.y0 < h.y1 + 150
             and min(b.x1, h.x1) - max(b.x0, h.x0) > 15]
    return len(above) >= 3 and len(below) >= 3


MIN_HEADLINE_ALPHA_CHARS = 3  # merged headline runs with fewer than
                              # this many letter characters are stub
                              # OCR fragments ("of", "s_", ".", etc.)
                              # not real headlines. A word filter that
                              # ignores punctuation and digits and is
                              # robust to OCR noise.


def merge_headline_runs(headlines):
    """Group headline spans that belong to the same multi-line
    headline. Spans qualify for the same run if:
      - similar font size (within 35%)
      - close vertically (y-overlap or within own height)
      - close horizontally (x-overlap > 0 OR within 60pt of each other)
    The last condition is what the v2 prototype missed — without it
    two unrelated headlines on the same y-band get merged.
    """
    if not headlines:
        return []
    headlines = sorted(headlines, key=lambda h: (h.y0, h.x0))
    runs = []
    for h in headlines:
        merged = False
        for r in runs:
            same_size = abs(h.size - r.size) < r.size * 0.35
            y_close = (h.y0 < r.y1 + max(h.y1 - h.y0, 8)
                       and h.y1 > r.y0 - max(h.y1 - h.y0, 8))
            x_overlap = min(h.x1, r.x1) - max(h.x0, r.x0)
            x_gap = max(h.x0, r.x0) - min(h.x1, r.x1)
            x_close = x_overlap > 0 or x_gap < 60
            if same_size and y_close and x_close:
                r.x0 = min(r.x0, h.x0); r.x1 = max(r.x1, h.x1)
                r.y0 = min(r.y0, h.y0); r.y1 = max(r.y1, h.y1)
                r.text += " " + h.text
                merged = True
                break
        if not merged:
            runs.append(HeadlineRun(
                x0=h.x0, y0=h.y0, x1=h.x1, y1=h.y1,
                size=h.size, text=h.text,
            ))
    return runs


def cluster_body_columns(spans, body, min_y, intra_col_gap=18):
    """Group body-size spans into inner columns by x0 proximity, then
    split each column vertically at internal gaps >= intra_col_gap pt.

    Returns a list of BodyColumn sorted by y0 then x0. Columns with
    fewer than 5 spans are dropped as noise.

    Why the vertical split: a single x-position on the page typically
    serves multiple stacked articles. Without the split, each x-band
    would produce one giant column running from the topmost article's
    body to the bottommost article's body, and any headline below the
    first article would find no body column "starting below it"
    (because the column starts way up at the first article's top).
    Splitting at internal whitespace gaps gives one fragment per
    article zone, which is what the assembly step needs.
    """
    body_spans = [s for s in spans
                  if 0.85 * body <= s.size <= 1.25 * body
                  and s.y0 > min_y]
    if not body_spans:
        return []
    body_spans.sort(key=lambda s: s.x0)
    BAND = 25

    # 1. cluster by x first
    x_groups = []
    for s in body_spans:
        placed = False
        for c in x_groups:
            if abs(s.x0 - c["x0_seed"]) <= BAND:
                c["spans"].append(s)
                c["x0_min"] = min(c["x0_min"], s.x0)
                c["x1_max"] = max(c["x1_max"], s.x1)
                placed = True
                break
        if not placed:
            x_groups.append({
                "x0_seed": s.x0, "x0_min": s.x0, "x1_max": s.x1,
                "spans": [s],
            })

    # 2. inside each x-group, walk spans top-to-bottom and split
    #    whenever the gap between consecutive spans exceeds the
    #    intra-column-gap threshold.
    out = []
    for g in x_groups:
        g["spans"].sort(key=lambda s: s.y0)
        current = []
        prev_y1 = None
        for s in g["spans"]:
            if prev_y1 is not None and (s.y0 - prev_y1) > intra_col_gap:
                if len(current) >= 5:
                    out.append(_make_col(g, current))
                current = []
            current.append(s)
            prev_y1 = s.y1
        if len(current) >= 5:
            out.append(_make_col(g, current))
    return sorted(out, key=lambda c: (c.y0, c.x0_min))


def _make_col(x_group, span_list):
    return BodyColumn(
        x0_seed=x_group["x0_seed"],
        x0_min=min(s.x0 for s in span_list),
        x1_max=max(s.x1 for s in span_list),
        y0=min(s.y0 for s in span_list),
        y1=max(s.y1 for s in span_list),
        spans=span_list,
    )
