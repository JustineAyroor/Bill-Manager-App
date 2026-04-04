from __future__ import annotations
from pathlib import Path

def extract_pdf_text(pdf_path: str) -> str:
    """
    Local text extraction. Works for many bills, but some PDFs are image-scanned (then you'd need OCR later).
    """
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(pdf_path)

    try:
        from pypdf import PdfReader
    except Exception as e:
        raise RuntimeError("Install pypdf: uv add pypdf") from e

    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        if txt.strip():
            parts.append(txt)

    return "\n\n".join(parts).strip()