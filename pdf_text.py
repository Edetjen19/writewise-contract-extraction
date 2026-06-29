"""Deterministic PDF text extraction + normalization for grounding checks.

This layer is *not* LLM-driven, so it is itself trustworthy provenance:
- extract_pages() gives the page text the agent can cite and the corpus the
  grounding verifier checks against.
- normalize() canonicalizes whitespace / dashes / bullet glyphs so the grounding
  comparison tests content, not incidental formatting differences.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

import pdfplumber

# pdfplumber emits this placeholder for the bullet glyph it can't map to a char.
_CID_BULLET = "(cid:127)"
_DASHES = "‐‑‒–—―−"  # hyphen/figure/en/em/horizontal-bar/minus


@dataclass(frozen=True)
class Page:
    page: int
    text: str


def extract_pages(pdf_path: str) -> list[Page]:
    """Return one Page per PDF page, in document order."""
    pages: list[Page] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages.append(Page(page=i, text=page.extract_text() or ""))
    return pages


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_bullets(text: str) -> str:
    """Turn the pdfplumber bullet placeholder into a real bullet, for storage."""
    return text.replace(_CID_BULLET, "•")


def normalize(text: str) -> str:
    """Canonical form for substring grounding comparison.

    The model may copy a value with different spacing or a normalized dash; the
    PDF text may carry bullet placeholders and non-breaking spaces. We fold both
    sides to the same shape so the check compares *content*.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = t.replace(_CID_BULLET, " ").replace(" ", " ")
    for d in _DASHES:
        t = t.replace(d, "-")
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def page_numbered_text(pages: list[Page]) -> str:
    """Render all pages as one string with explicit page markers.

    Given to the extractor so it can (a) copy exact value strings into raw_text
    and (b) assign the correct source page to every row.
    """
    blocks = []
    for p in pages:
        blocks.append(f"===== PAGE {p.page} =====\n{clean_bullets(p.text)}")
    return "\n\n".join(blocks)
