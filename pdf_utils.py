"""Shared low-level PDF helpers.

Single home for utilities that were previously copy-pasted across the
detector modules. Body verified byte-identical against all five prior
copies (find_columns, detect_ads, detect_sliver, page_profile,
crop_pdf) before consolidation.
"""

import fitz


def open_clean_pdf(pdf_path):
    """Open a PDF and strip red overlay lines from all pages.

    Heritage scans in this corpus may carry red rule overlays from a
    previous annotation pass (PDF content stream `1 0 0 RG`). Those
    rules contaminate darkness profiles and contour detection, so
    every detector wants the document with them blanked out before
    rendering. This helper does the strip and returns the in-memory
    fitz.Document — caller is responsible for `.close()`.
    """
    doc = fitz.open(pdf_path)
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref).decode("latin-1")
            if "1 0 0 RG" in data:
                doc.update_stream(xref, b"")
    return doc
