from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from app.services.llm_client import get_llm_client
from app.core.config import OPENROUTER_MODEL

# NOTE: we normalize Sep -> Sept because many models return "Sep"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]

PHONE_RE = re.compile(r"\b(\+?1[\s\-\.]?)?\(?(\d{3})\)?[\s\-\.]?(\d{3})[\s\-\.]?(\d{4})\b")
LAST4_RE = re.compile(r"last4:(\d{4})", re.IGNORECASE)

# Strips ```json fences
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def phone_key_from_number(num: str) -> str:
    digits = re.sub(r"\D+", "", num or "")
    if len(digits) >= 4:
        return f"last4:{digits[-4:]}"
    return "last4:????"


def find_phone_numbers(text: str, limit: int = 25) -> List[str]:
    found: List[str] = []
    for m in PHONE_RE.finditer(text or ""):
        digits = f"{m.group(2)}{m.group(3)}{m.group(4)}"
        if digits not in found:
            found.append(digits)
        if len(found) >= limit:
            break
    return found


def _heuristic_guess(text: str) -> Dict[str, Any]:
    t = (text or "").replace("\u00a0", " ")

    amt = None
    m = re.search(
        r"(total\s+due|amount\s+due|total\s+amount\s+due)\s*[:\s]\s*\$?\s*([0-9][0-9,]*\.[0-9]{2})",
        t,
        re.IGNORECASE,
    )
    if m:
        try:
            amt = float(m.group(2).replace(",", ""))
        except Exception:
            amt = None

    period = None
    m2 = re.search(
        r"([A-Za-z]{3,9})\s+\d{1,2},\s*(20\d{2})\s*[-–]\s*([A-Za-z]{3,9})\s+\d{1,2},\s*(20\d{2})",
        t,
    )
    if m2:
        period = {
            "start_month": m2.group(1),
            "start_year": int(m2.group(2)),
            "end_month": m2.group(3),
            "end_year": int(m2.group(4)),
        }

    phones = find_phone_numbers(t)
    return {"total_amount_guess": amt, "period_guess": period, "phones_found": phones}


def _safe_json_loads(s: str) -> Dict[str, Any]:
    """
    Robust JSON loader for LLM outputs:
    - strips ```json fences
    - if extra text exists, extracts first {...} block
    """
    if not s:
        raise ValueError("Empty model response")

    txt = s.strip()

    # Strip code fences if present
    if txt.startswith("```"):
        txt = JSON_FENCE_RE.sub("", txt).strip()

    # Extract first JSON object if response contains extra text
    if not txt.startswith("{"):
        start = txt.find("{")
        end = txt.rfind("}")
        if start != -1 and end != -1 and end > start:
            txt = txt[start : end + 1].strip()

    return json.loads(txt)


def _normalize_month(m: str) -> str:
    m = (m or "").strip()
    if m == "Sep":
        return "Sept"
    return m


@dataclass
class BillProposal:
    year: int
    month: str
    total_amount: float
    confidence: float
    evidence_total: str
    evidence_period: str
    unassigned_amount: float
    # Each line: phone_key, display, line_total, confidence, source, evidence_total_line, charges[]
    lines: List[Dict[str, Any]]
    # by_phone: [{phone_key, suggested_amount}]
    allocation_by_phone: List[Dict[str, Any]]
    notes: str


def extract_bill_proposal(text: str) -> BillProposal:
    """
    LLM-based extractor for mobile carrier bills.
    Strongly prefers 'THIS BILL SUMMARY' table per-line totals when present.

    Raises:
      - ValueError / RuntimeError with a helpful snippet when the model returns bad JSON.
    """
    if not text or len(text.strip()) < 200:
        raise ValueError("PDF text too short; bill might be scanned. Try OCR later.")

    heur = _heuristic_guess(text)

    sys = (
        "You extract structured billing data from mobile carrier bills.\n"
        "Return STRICT JSON only. No markdown, no commentary.\n"
        "\n"
        "CRITICAL PRIORITY RULES (follow in order):\n"
        "1) If the text contains a section titled 'THIS BILL SUMMARY' with a table listing per-line totals,\n"
        "   then you MUST use that table as the primary source of truth for per-line totals.\n"
        "   - Each phone line row in that table becomes one entry in `lines`.\n"
        "   - The per-line `line_total` MUST match the 'Total' column for that phone line.\n"
        "2) The 'Totals' row represents the whole bill total. Use it to validate `invoice.total_amount`.\n"
        "3) The 'Account' row represents account-level charges (plan base, subscriptions, fees) that are NOT tied\n"
        "   to a specific phone line. Do NOT assign the Account row to any phone line.\n"
        "   Put account-level charges into `unassigned_pool`.\n"
        "4) Only if 'THIS BILL SUMMARY' is missing may you estimate per-line totals from 'DETAILED CHARGES'.\n"
        "\n"
        "GENERAL RULES:\n"
        "- Month must be one of: Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sept,Oct,Nov,Dec.\n"
        "- invoice.total_amount must be a number > 0.\n"
        "- Identify phone lines and group charges by phone number.\n"
        "- Use phone_key as 'last4:XXXX' when possible.\n"
        "- If some charges cannot be attributed to a specific line (taxes/fees/account charges/credits), put them into unassigned_pool.\n"
        "- Provide allocation_suggestion.by_phone. If 'THIS BILL SUMMARY' exists, allocation should match the per-line totals.\n"
        "- Provide evidence snippets for total, period, and for each line_total (copy the relevant table line).\n"
    )

    user_obj = {
        "task": (
            "Extract invoice period+total and propose allocations by phone line.\n"
            "If 'THIS BILL SUMMARY' exists, use its per-line Total values as the allocations.\n"
            "Treat 'Account' row as unassigned_pool (account-level charges), not a line allocation."
        ),
        "heuristics": heur,
        "text": (text or "")[:24000],
        "output_schema": {
            "invoice": {
                "year": "int",
                "month": "MonAbbrev",
                "total_amount": "float",
                "confidence": "float_0_to_1",
                "evidence_total": "string",
                "evidence_period": "string",
            },
            "lines": [
                {
                    "phone_key": "last4:XXXX",
                    "display": "string",
                    "line_total": "float",
                    "confidence": "float_0_to_1",
                    "source": "bill_summary_table|detailed_charges|estimate",
                    "evidence_total_line": "string",
                    "charges": [{"label": "string", "amount": "float", "evidence": "string"}],
                }
            ],
            "unassigned_pool": {
                "amount": "float",
                "items": [{"label": "string", "amount": "float", "evidence": "string"}],
            },
            "allocation_suggestion": {
                "method": "string",
                "by_phone": [{"phone_key": "last4:XXXX", "suggested_amount": "float"}],
                "notes": "string",
            },
        },
    }

    client = get_llm_client()
    resp = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": json.dumps(user_obj)},
        ],
        temperature=0.0,
    )

    content = resp.choices[0].message.content or ""
    try:
        obj = _safe_json_loads(content)
    except Exception as e:
        raise RuntimeError(
            f"LLM did not return valid JSON: {e}\n"
            f"First 600 chars of response:\n{content[:600]}"
        )

    inv = obj.get("invoice") or {}
    try:
        year = int(inv.get("year"))
    except Exception:
        raise ValueError(f"LLM invoice.year invalid: {inv.get('year')}")

    month = _normalize_month(str(inv.get("month") or ""))
    if month not in MONTHS:
        raise ValueError(f"LLM month invalid: {month}")

    try:
        total = float(inv.get("total_amount"))
    except Exception:
        raise ValueError(f"LLM total invalid: {inv.get('total_amount')}")
    if total <= 0:
        raise ValueError(f"LLM total invalid: {total}")

    try:
        conf = float(inv.get("confidence") or 0.5)
    except Exception:
        conf = 0.5

    evidence_total = str(inv.get("evidence_total") or "")[:350]
    evidence_period = str(inv.get("evidence_period") or "")[:350]

    # Lines
    lines = obj.get("lines") or []
    norm_lines: List[Dict[str, Any]] = []

    for ln in lines:
        pk = str(ln.get("phone_key") or "").strip()
        disp = str(ln.get("display") or "").strip()

        if not pk.startswith("last4:"):
            pk = phone_key_from_number(disp)

        try:
            line_total = float(ln.get("line_total") or 0.0)
        except Exception:
            line_total = 0.0

        try:
            lconf = float(ln.get("confidence") or 0.5)
        except Exception:
            lconf = 0.5

        charges = ln.get("charges") or []
        norm_charges = []
        for ch in charges:
            try:
                amt = float(ch.get("amount") or 0.0)
            except Exception:
                amt = 0.0
            norm_charges.append(
                {
                    "label": str(ch.get("label") or "")[:80],
                    "amount": amt,
                    "evidence": str(ch.get("evidence") or "")[:220],
                }
            )

        norm_lines.append(
            {
                "phone_key": pk,
                "display": disp,
                "line_total": line_total,
                "confidence": lconf,
                "source": str(ln.get("source") or ""),
                "evidence_total_line": str(ln.get("evidence_total_line") or "")[:300],
                "charges": norm_charges,
            }
        )

    # Unassigned pool
    unassigned = obj.get("unassigned_pool") or {}
    try:
        unassigned_amount = float(unassigned.get("amount") or 0.0)
    except Exception:
        unassigned_amount = 0.0

    # Allocation suggestion
    alloc = obj.get("allocation_suggestion") or {}
    by_phone = alloc.get("by_phone") or []
    alloc_by_phone: List[Dict[str, Any]] = []

    for a in by_phone:
        pk = str(a.get("phone_key") or "").strip()
        if not pk.startswith("last4:"):
            # if model gives a raw number or display, normalize to last4
            pk = phone_key_from_number(pk)

        try:
            amt = float(a.get("suggested_amount") or 0.0)
        except Exception:
            amt = 0.0

        alloc_by_phone.append({"phone_key": pk, "suggested_amount": amt})

    notes = str(alloc.get("notes") or "").strip()

    # Sanity warning only (don't fail hard)
    s = sum(x["suggested_amount"] for x in alloc_by_phone)
    if alloc_by_phone and abs(s - total) > 10.0:
        notes = (notes + f" | WARNING: suggested sum {s:.2f} differs from total {total:.2f}").strip()

    return BillProposal(
        year=year,
        month=month,
        total_amount=total,
        confidence=conf,
        evidence_total=evidence_total,
        evidence_period=evidence_period,
        unassigned_amount=unassigned_amount,
        lines=norm_lines,
        allocation_by_phone=alloc_by_phone,
        notes=notes,
    )