"""
Classify column segments using an LLM and maintain cross-column context.

Stage 2 of the pipeline:
Stage 1 (find_splits.py): Detects split points and produces raw segments.
Stage 2 (this module): Sends each segment to the LLM for classification,
merges adjacent segments of the same item, and tracks context across
columns for continuation detection.

Usage:
    from classify_segments import classify_column, ColumnContext

    ctx = ColumnContext()

    # Process columns in order
    for col_num in range(1, 8):
        segments = [...]  # from find_splits.split_column()
        items = classify_column(col_num, segments, ctx)
        for item in items:
            print(f"{item['label']}: segments {item['seg_indices']}")

    # Context tracks what's unfinished
    print(ctx.summary())

"""

import json
import base64
import os


class ColumnContext:
    """
    Accumulates knowledge across columns during processing.

    Tracks:
      - Articles that continue across column boundaries
      - Multi-column display ads
      - The column number being processed
      - All classified items so far
    """

    def __init__(self):
        self.columns_processed = []
        self.open_items = []  # items flagged as continuing into the next column
        self.all_items = []   # every classified item across all columns

    def add_column_result(self, col_num, items):
        """Record the classified items from a column."""
        self.columns_processed.append(col_num)
        self.all_items.extend(items)

        # Update open items: clear previous, add any new continuations
        self.open_items = [
            item for item in items
            if item.get("continues_next_column", False)
        ]

    def get_prior_context(self):
        """
        Build a text summary of relevant prior context for the LLM.
        Focuses on items that may continue into the current column.
        """
        if not self.columns_processed:
            return "This is the first column being processed. No prior context."

        lines = []
        lines.append(
            f"Columns already processed: {self.columns_processed}."
        )

        if self.open_items:
            lines.append("Items from the previous column that may continue:")
            for item in self.open_items:
                lines.append(
                    f"  - Column {item['column']}, {item['label']}: "
                    f"{item.get('description', 'no description')}"
                )
                if item.get("continued_from"):
                    lines.append(
                        f"    (itself continued from column {item['continued_from']})"
                    )
        else:
            lines.append(
                "No items from the previous column were flagged as continuing."
            )

        return "\n".join(lines)

    def summary(self):
        """Human-readable summary of all processing so far."""
        lines = [f"Processed {len(self.columns_processed)} columns: {self.columns_processed}"]
        lines.append(f"Total items classified: {len(self.all_items)}")
        if self.open_items:
            lines.append(f"Open items awaiting continuation: {len(self.open_items)}")
            for item in self.open_items:
                lines.append(f"  - {item['label']} (column {item['column']})")
        return "\n".join(lines)

    def to_dict(self):
        """Serialise context for saving or passing between processes."""
        return {
            "columns_processed": self.columns_processed,
            "open_items": self.open_items,
            "all_items": self.all_items,
        }

    @classmethod
    def from_dict(cls, data):
        """Restore context from a serialised dict."""
        ctx = cls()
        ctx.columns_processed = data.get("columns_processed", [])
        ctx.open_items = data.get("open_items", [])
        ctx.all_items = data.get("all_items", [])
        return ctx

    def save(self, path):
        """Save context to a JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path):
        """Load context from a JSON file."""
        with open(path) as f:
            return cls.from_dict(json.load(f))


def _image_to_base64(path):
    """Read an image file and return base64-encoded data."""
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def _build_prompt(col_num, segments, context):
    """
    Build the system and user prompts for the LLM classification call.
    """
    system = """You are analysing segments of a single column from a scanned heritage newspaper page.

Each segment is a horizontal slice of the column image, produced by automated split detection.
The splits were made at whitespace gaps and horizontal rules. The code tends to over-split
(splitting within articles at paragraph gaps), so your job is to classify each segment and
identify which adjacent segments belong to the same item.

For each segment, classify it as one of:

- margin: PDF white space, photo edge, shadow — not newspaper content
- masthead: newspaper title, date, page number
- headline: article headline or subheadline (display type, larger than body)
- article_body: body text of a news article or feature
- continuation_note: text like "Continued from Page 1" or "Continued on Page 5"
- decorative: horizontal rules, ornamental dividers, section headers
- advertisement: display or classified advertising
- illustration: photograph, drawing, or other image content

Then group adjacent segments that belong to the same item. An item is a self-contained
piece of content: one article (headline + body), one advertisement, one decorative divider, etc.

Also flag if the last item in the column appears to continue into the next column
(eg text cut off mid-sentence at the bottom of the content area).

And flag if the first content item appears to be a continuation from the previous column
(eg it starts with body text rather than a headline, or has a continuation note).

Respond ONLY with a JSON object (no markdown, no backticks, no preamble) with this structure:
{
  "items": [
    {
      "label": "Short descriptive label for this item",
      "type": "article|advertisement|decorative|margin|masthead|illustration",
      "seg_indices": [0, 1, 2],
      "headline": "Headline text if visible, or null",
      "description": "Brief description of the content",
      "continues_next_column": false,
      "continued_from_previous": false
    }
  ]
}"""

    prior = context.get_prior_context()

    user_parts = [
        {
            "type": "text",
            "text": (
                f"Column {col_num} of the newspaper page.\n\n"
                f"Prior context:\n{prior}\n\n"
                f"There are {len(segments)} segments in this column, numbered 0 to "
                f"{len(segments) - 1}. Each segment image follows in order. "
                f"Classify each segment and group them into items."
            ),
        }
    ]

    for seg in segments:
        b64 = _image_to_base64(seg["path"])
        user_parts.append({
            "type": "text",
            "text": f"Segment {seg['index']} ({seg['y_end'] - seg['y_start']}px tall):",
        })
        user_parts.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": b64,
            },
        })

    return system, user_parts


def classify_column(col_num, segments, context, api_call_fn=None):
    """
    Classify segments in a column using the LLM.

    Args:
        col_num:      Column number (1-indexed).
        segments:     List of segment dicts from split_column().
        context:      ColumnContext with prior column knowledge.
        api_call_fn:  Function to call the LLM API. Signature:
                          api_call_fn(system_prompt, user_content) -> str
                      If None, uses a default implementation that calls
                      the Anthropic API.

    Returns:
        List of item dicts with classification and grouping.
    """
    if api_call_fn is None:
        api_call_fn = _default_api_call

    system, user_parts = _build_prompt(col_num, segments, context)
    response_text = api_call_fn(system, user_parts)

    # Parse JSON response
    cleaned = response_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]

    result = json.loads(cleaned)
    items = result.get("items", [])

    # Enrich items with column number and segment paths
    for item in items:
        item["column"] = col_num
        item["segment_paths"] = [
            segments[i]["path"]
            for i in item.get("seg_indices", [])
            if i < len(segments)
        ]

    # Update context
    context.add_column_result(col_num, items)

    return items


def _default_api_call(system_prompt, user_content):
    """
    Default LLM API call using the Anthropic messages endpoint.
    Requires the anthropic Python package or a direct HTTP call.
    """
    import urllib.request

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_content}
        ],
    }).encode()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY environment variable not set. "
            "Set it before calling classify_column()."
        )

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "x-api-key": api_key,
        },
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())

    # Extract text from response
    text_parts = [
        block["text"]
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "\n".join(text_parts)


def print_items(items):
    """Pretty-print classified items."""
    for item in items:
        segs = item.get("seg_indices", [])
        flags = []
        if item.get("continues_next_column"):
            flags.append("\u2192 continues")
        if item.get("continued_from_previous"):
            flags.append("\u2190 continued")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        print(
            f"  {item['type']:14s}  segs={segs}  "
            f"\u201c{item.get('label', '?')}\u201d{flag_str}"
        )
        if item.get("description"):
            print(f"                  {item['description']}")
