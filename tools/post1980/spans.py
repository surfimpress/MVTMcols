"""Text-span extraction from a PDF page's embedded text layer.

The PDFs in this corpus have a text layer (Adobe Paper Capture on
1990-2007, TCPDF re-wraps on 1985). We trust the *geometry* (bbox,
font size) but NOT the OCR text content — the LLM transcript pass
re-reads from rendered crops, so the text strings here are for
debugging only.
"""
from collections import Counter
from dataclasses import dataclass


@dataclass
class Span:
    size: float      # font size in points
    x0: float
    y0: float
    x1: float
    y1: float
    text: str        # raw OCR text — debug only

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def w(self): return self.x1 - self.x0

    @property
    def h(self): return self.y1 - self.y0


def extract_spans(page):
    """All non-empty text spans on the page → list[Span]."""
    spans = []
    d = page.get_text("dict")
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for s in line["spans"]:
                bbox = s["bbox"]
                if bbox[2] - bbox[0] < 1 or bbox[3] - bbox[1] < 1:
                    continue
                text = s["text"].strip()
                if not text:
                    continue
                spans.append(Span(
                    size=s["size"],
                    x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3],
                    text=text,
                ))
    return spans


def body_font_size(spans):
    """The mode of rounded font sizes — the body text size on this page.

    Requires at least 5 supporting spans to count something as the mode;
    on a near-empty page falls back to the most common (size, count).
    """
    if not spans:
        return 12.0
    sizes = [round(s.size, 1) for s in spans]
    counts = Counter(sizes).most_common()
    for size, n in counts:
        if n >= 5:
            return size
    return counts[0][0]
