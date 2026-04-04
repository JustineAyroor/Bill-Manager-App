from __future__ import annotations
import re

PAGE_SPLIT = re.compile(r"\bPage\s+\d+\s+of\s+\d+\b", re.IGNORECASE)
APPENDIX_END = re.compile(r"\bAPPENDIX_END\b", re.IGNORECASE)

ANCHORS = [
    "TOTAL DUE",
    "THIS BILL SUMMARY",
    "DETAILED CHARGES",
    "PLANS",
    "EQUIPMENT",
    "HANDSETS",
    "SERVICES",
    "YOU SAVED",
]

def filter_text_for_llm(raw: str, max_pages: int = 3, max_chars: int = 12000) -> str:
    """
    Reduce long bills to only the most relevant sections so LLM doesn't get confused.
    Works well for T-Mobile-like statements.
    """
    if not raw:
        return ""

    txt = raw.replace("\u00a0", " ")

    # Stop at appendix/end marker if present
    m_end = APPENDIX_END.search(txt)
    if m_end:
        txt = txt[:m_end.start()]

    # Keep first N pages by splitting on Page markers if they exist
    parts = PAGE_SPLIT.split(txt)
    # parts may include header before first Page; keep it + first N chunks
    kept = parts[: max_pages + 1]  # header + N pages
    short = "\n\n".join([p.strip() for p in kept if p.strip()])

    # If still huge, extract windows around anchors
    if len(short) > max_chars:
        lines = short.splitlines()
        keep_idx = set()
        for i, line in enumerate(lines):
            up = line.upper()
            if any(a in up for a in ANCHORS):
                # keep a window around the anchor line
                for j in range(max(i - 40, 0), min(i + 200, len(lines))):
                    keep_idx.add(j)

        reduced = "\n".join(lines[i] for i in sorted(keep_idx)) if keep_idx else short[:max_chars]
        short = reduced
    print(short)
    return short[:max_chars].strip()