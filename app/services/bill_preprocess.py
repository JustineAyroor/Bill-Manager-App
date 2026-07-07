"""
Preprocessing for the Bill Import v2 (RAG, opt-in) pipeline.

Deliberately separate from app/services/bill_text_filter.py (the legacy
regex-anchor filter, untouched and still used by the legacy pipeline). This
module does only cheap, local, carrier-agnostic normalization - no LLM call
is involved anywhere here.
"""

from __future__ import annotations

import hashlib
import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

_WHITESPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_HYPHEN_LINEBREAK_RE = re.compile(r"(\w)-\n(\w)")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

_MONTH_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Matches common US bill-date formats near the top of a bill, e.g. "Apr 19, 2024"
# or "April 19, 2024" - carrier-agnostic, no LLM call.
_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+(\d{4})\b",
    re.IGNORECASE,
)


def guess_bill_period_key(cleaned: str) -> int | None:
    """
    Cheap, local, regex-only best-effort guess at the bill's own (year, month)
    as a period_key (e.g. 202404), used ONLY to scope historical precedent to
    facts that predate this bill - never to leak "future" outcome facts (from
    later real-world uploads) into the context for an older bill being
    imported/backfilled today. This is NOT the authoritative extracted
    invoice period - the LLM still determines that from the full bill text;
    this only needs to be roughly right to avoid future-leakage.

    Bill/account statement dates are reliably near the top of the document
    (e.g. "Bill issue date\\nApr 19, 2024"), so only the first ~2000 chars are
    scanned.
    """
    if not cleaned:
        return None
    match = _DATE_RE.search(cleaned[:2000])
    if not match:
        return None
    month_num = _MONTH_TO_NUM.get(match.group(1).lower())
    if not month_num:
        return None
    try:
        year = int(match.group(2))
    except ValueError:
        return None
    return year * 100 + month_num


def clean_text(raw: str) -> str:
    """Whitespace/non-breaking-space/hyphenation normalization only - no LLM call."""
    if not raw:
        return ""

    text = raw.replace("\u00a0", " ")
    # Re-join words split across a line break by a hyphen, e.g. "hand-\nset" -> "handset"
    text = _HYPHEN_LINEBREAK_RE.sub(r"\1\2", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    lines = [ln.strip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def content_hash(cleaned: str) -> str:
    """sha256 of the cleaned text - the cache key for BillImportJob dedup."""
    return hashlib.sha256((cleaned or "").encode("utf-8")).hexdigest()


def chunk_text(cleaned: str, chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP) -> list[Document]:
    """Split cleaned text into overlapping chunks for embedding-based selection."""
    if not cleaned:
        return []
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    return splitter.create_documents([cleaned])
