"""Assemble article-block rectangles from headlines + body columns.

The v2 prototype had two specific bugs this module fixes:

1. Headline runs were merged without an x-proximity gate — two
   unrelated headlines sharing a y-band got fused. Fixed in
   text_groups.merge_headline_runs.

2. Articles weren't trimmed when a sub-article headline appeared
   inside them. An article's bottom = the y0 of the next headline
   that *horizontally overlaps* it, not just any next headline.
   That's what this module enforces.

3. Articles overlapping a display ad get their bottom clipped at
   the ad's top edge (similarly for photo regions if the photo is
   adjacent-not-internal).
"""
from dataclasses import dataclass, field
from typing import List

from .text_groups import HeadlineRun, BodyColumn


@dataclass
class ArticleBlock:
    headline: HeadlineRun
    columns: List[BodyColumn] = field(default_factory=list)
    photos: list = field(default_factory=list)   # ImageRegion
    bbox: tuple = (0.0, 0.0, 0.0, 0.0)


def _x_overlap(a, b):
    """Length of horizontal overlap between two (x0, x1) ranges."""
    return min(a[1], b[1]) - max(a[0], b[0])


def assemble_articles(headline_runs, body_columns, display_ads,
                      page_w, page_h, whitespace_bands=()):
    """Each headline-run becomes an article block; body columns get
    distributed to whichever headline-run is directly above them.

    Vertical bound for each article = min of:
      - y0 of the next headline that horizontally overlaps it >30%
      - y0 of any display ad whose x-range overlaps >30%
      - y0 of the next horizontal whitespace band below the headline
        (rows with no ink across the page — these are the strong
        row dividers in modular layouts)
      - page bottom
    """
    runs = sorted(headline_runs, key=lambda h: h.y0)
    articles = []
    for i, h in enumerate(runs):
        bottom_y = page_h
        for j, other in enumerate(runs):
            if j == i or other.y0 <= h.y1:
                continue
            ovlp = _x_overlap((other.x0, other.x1), (h.x0, h.x1))
            if h.w > 0 and ovlp / h.w > 0.3:
                bottom_y = min(bottom_y, other.y0)
        for ad in display_ads:
            if ad.y0 <= h.y1:
                continue
            ovlp = _x_overlap((ad.x0, ad.x1), (h.x0, h.x1))
            if h.w > 0 and ovlp / h.w > 0.3:
                bottom_y = min(bottom_y, ad.y0)
        for band_y0, band_y1 in whitespace_bands:
            if band_y0 <= h.y1 + 4:
                continue   # band is at or above headline
            bottom_y = min(bottom_y, band_y0)
            break          # bands sorted top-down; first one below wins

        chosen = []
        for col in body_columns:
            if col.y0 < h.y1 - 5 or col.y0 >= bottom_y:
                continue
            x_ovlp = _x_overlap((col.x0_min, col.x1_max), (h.x0, h.x1))
            if x_ovlp < 25:
                continue
            clipped_y1 = min(col.y1, bottom_y)
            if clipped_y1 - col.y0 < 30:
                continue
            chosen.append(col)

        if not chosen:
            continue

        ax0 = min(h.x0, *(c.x0_min for c in chosen))
        ax1 = max(h.x1, *(c.x1_max for c in chosen))
        ay0 = h.y0
        ay1 = min(bottom_y, max(min(c.y1, bottom_y) for c in chosen))
        articles.append(ArticleBlock(
            headline=h, columns=chosen,
            bbox=(ax0, ay0, ax1, ay1),
        ))
    return articles


def drop_noise_articles(articles, min_alpha_chars=3):
    """Drop articles whose headline text has fewer than `min_alpha_chars`
    letter characters. These are stub OCR fragments ("of", "s_", ".",
    "=") that got headline-sized but don't represent a real article.

    Filtering at this stage rather than at merge_headline_runs is
    important: the noise runs serve as bottom-bound constraints during
    `assemble_articles`. Removing them earlier causes the article above
    to extend further and absorb the article below.
    """
    kept = []
    for a in articles:
        alpha = sum(1 for c in a.headline.text if c.isalpha())
        if alpha >= min_alpha_chars:
            kept.append(a)
    return kept


def absorb_contained(articles, tolerance=10.0):
    """Drop any article whose bbox is wholly contained within another
    article's bbox. The larger article is treated as authoritative;
    the smaller is a redundant sub-detection (often a sub-element
    that survived earlier filtering).

    `tolerance` (in points) gives a small slack so that near-identical
    edges still count as "inside".
    """
    keep = list(articles)
    dropped = set()
    for i, a in enumerate(keep):
        if i in dropped:
            continue
        ax0, ay0, ax1, ay1 = a.bbox
        for j, b in enumerate(keep):
            if j == i or j in dropped:
                continue
            bx0, by0, bx1, by1 = b.bbox
            # B wholly inside A?
            if (bx0 >= ax0 - tolerance and bx1 <= ax1 + tolerance
                    and by0 >= ay0 - tolerance and by1 <= ay1 + tolerance):
                # Don't absorb if the two bboxes are essentially identical
                # (within tolerance on all sides) — keep the first one only.
                same = (
                    abs(bx0 - ax0) <= tolerance
                    and abs(by0 - ay0) <= tolerance
                    and abs(bx1 - ax1) <= tolerance
                    and abs(by1 - ay1) <= tolerance
                )
                # Drop B regardless — A is bigger or equal.
                dropped.add(j)
    return [a for i, a in enumerate(keep) if i not in dropped]


def clip_overlapping(articles):
    """If article A fully contains article B (B is below A's headline
    and B's x-range is mostly inside A's x-range), clip A's bottom
    to B's top. The lead article was too greedy; the sub-article is
    the correct granularity.

    Pairwise check, O(n^2); the article count is small per page.
    Returns the list with mutated bboxes (and matching .columns).
    """
    arts = sorted(articles, key=lambda a: (a.bbox[1], a.bbox[0]))
    for i, a in enumerate(arts):
        ax0, ay0, ax1, ay1 = a.bbox
        for j in range(i + 1, len(arts)):
            b = arts[j]
            bx0, by0, bx1, by1 = b.bbox
            if by0 >= ay1:
                continue       # B is below A — no overlap
            if by0 < ay0 + 10:
                continue       # B starts at or above A — not a sub
            # B's horizontal range must be mostly inside A's
            x_overlap = max(0, min(ax1, bx1) - max(ax0, bx0))
            b_w = bx1 - bx0
            if b_w > 0 and x_overlap / b_w >= 0.5:
                # Clip A's bottom to B's top, trim A's columns too.
                new_y1 = by0 - 2
                a.bbox = (ax0, ay0, ax1, new_y1)
                a.columns = [
                    type(c)(
                        x0_seed=c.x0_seed,
                        x0_min=c.x0_min, x1_max=c.x1_max,
                        y0=c.y0, y1=min(c.y1, new_y1),
                        spans=[s for s in c.spans if s.y0 < new_y1],
                    )
                    for c in a.columns
                    if c.y0 < new_y1
                ]
                ay1 = new_y1
    return arts


def attach_photos(articles, image_regions):
    """Photos inside an article's bbox get attached to that article."""
    for art in articles:
        ax0, ay0, ax1, ay1 = art.bbox
        for img in image_regions:
            # Inside or substantially overlapping
            ix0 = max(ax0, img.x0); iy0 = max(ay0, img.y0)
            ix1 = min(ax1, img.x1); iy1 = min(ay1, img.y1)
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            inter = (ix1 - ix0) * (iy1 - iy0)
            if inter / img.area > 0.5:
                art.photos.append(img)
    return articles
