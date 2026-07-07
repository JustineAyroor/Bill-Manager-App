"""
Bill Import v2 (RAG, opt-in) extraction module.

New file - app/services/llm_invoice_extract.py (the legacy extractor) is
untouched and still used verbatim by the legacy synchronous pipeline.

Differences from the legacy module:
  - Carrier-aware system prompt (app/services/bill_prompts/registry.py)
    instead of a hardcoded T-Mobile-shaped prompt.
  - Takes already-selected top-N chunks (from vectorstore.select_relevant_chunks)
    instead of the full/regex-trimmed text - bounds input tokens.
  - Takes retrieved precedent_facts + a known_members roster instead of the
    old regex-based `_heuristic_guess()`.
  - Each line carries a generalized `identifier: {type, value}` (phone,
    email, name, account, or none) instead of the legacy phone-only
    `phone_key`, plus an optional LLM-suggested `matched_member_id`.
  - No `unassigned_pool`: lines with identifier.type == "none" simply aren't
    offered in the per-member mapping step and fall through to the existing
    owner-remainder allocation, exactly like today's account-level charges.
  - Uses JSON-object ("structured output") mode where the underlying model
    supports it, falling back to plain prompting + robust text parsing
    otherwise (mirrors _safe_json_loads from the legacy module).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import OPENROUTER_MODEL
from app.services.bill_prompts import build_user_payload, get_system_prompt
from app.services.llm_client import get_llm_client
from app.services.llm_invoice_extract import MONTHS, _normalize_month, _safe_json_loads

VALID_IDENTIFIER_TYPES = {"phone", "email", "name", "account", "none"}


@dataclass
class BillProposalV2:
    year: int
    month: str
    total_amount: float
    confidence: float
    evidence_total: str
    evidence_period: str
    # Each line: {identifier: {type, value}, matched_member_id, display, line_total,
    #             confidence, source, evidence_total_line, charges[]}
    lines: list[dict[str, Any]] = field(default_factory=list)
    # [{identifier, matched_member_id, suggested_amount}]
    allocation_by_line: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    raw_response: str = ""
    # Observability - the exact system prompt used, and OpenRouter's own
    # reported usage/cost for this call (see docs/decisions/.../08-...).
    system_prompt: str = ""
    token_usage: dict[str, Any] = field(default_factory=dict)


def _normalize_identifier(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    itype = str(raw.get("type") or "none").strip().lower()
    if itype not in VALID_IDENTIFIER_TYPES:
        itype = "none"
    ivalue = str(raw.get("value") or "").strip()
    if itype == "none":
        ivalue = ""
    return {"type": itype, "value": ivalue}


_PHONE_NUMBER_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?(\d{4})\b")


def _reconcile_phone_last4(identifier: dict[str, Any], evidence: str) -> dict[str, Any]:
    """
    LLMs are unreliable at precise digit-indexing tasks like "give me the
    last 4 digits" - in testing, a real bill's "(862) 372-0447" line came
    back from the model as identifier value "3727" (not "0447"), causing a
    silent phone-matching failure with no error anywhere. Since the model is
    separately asked to quote the evidence line verbatim (much more reliable
    than character-counting), re-derive the last 4 digits from that quoted
    text with a regex and prefer it whenever it disagrees with the model's
    own value - code does the precise extraction, the model just has to find
    the right line.
    """
    if identifier.get("type") != "phone" or not evidence:
        return identifier
    match = _PHONE_NUMBER_RE.search(evidence)
    if not match:
        return identifier
    derived = match.group(1)
    if derived and derived != identifier.get("value"):
        return {**identifier, "value": derived}
    return identifier


def _to_optional_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def _usage_to_dict(resp) -> dict[str, Any]:
    """
    Extract OpenRouter's own reported token counts + real $ cost (via
    `extra_body={"usage": {"include": True}}`) from a chat-completion
    response. Returns {} if the routed model/provider didn't include usage.
    """
    usage = getattr(resp, "usage", None)
    if not usage:
        return {}
    return {
        "chat_prompt_tokens": getattr(usage, "prompt_tokens", None),
        "chat_completion_tokens": getattr(usage, "completion_tokens", None),
        "chat_total_tokens": getattr(usage, "total_tokens", None),
        "chat_cost_usd": getattr(usage, "cost", None),
    }


def _call_llm(client, model: str, sys_prompt: str, user_obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": json.dumps(user_obj)},
    ]
    # Three-tier graceful degradation: not every model/provider routed
    # through OpenRouter supports response_format or usage accounting -
    # fall back progressively rather than failing the whole call.
    attempts: list[dict[str, Any]] = [
        {"response_format": {"type": "json_object"}, "extra_body": {"usage": {"include": True}}},
        {"extra_body": {"usage": {"include": True}}},
        {},
    ]
    last_exc: Exception | None = None
    for kwargs in attempts:
        try:
            resp = client.chat.completions.create(model=model, messages=messages, temperature=0.0, **kwargs)
            return (resp.choices[0].message.content or ""), _usage_to_dict(resp)
        except Exception as e:
            last_exc = e
    raise last_exc


def extract_bill_proposal_v2(
    selected_chunks: list[str],
    precedent_facts: list[str] | None = None,
    known_roster: list[dict[str, Any]] | None = None,
    carrier_type: str | None = None,
    model: str | None = None,
) -> BillProposalV2:
    """
    LLM-based extractor for the v2 (RAG) pipeline.

    Raises:
      - ValueError / RuntimeError with a helpful snippet when input is too
        short or the model returns bad JSON - same failure contract as the
        legacy extract_bill_proposal().
    """
    text = "\n\n".join(c for c in (selected_chunks or []) if c and c.strip())
    if not text or len(text.strip()) < 50:
        raise ValueError("No usable text in selected_chunks; bill might be scanned or extraction failed upstream.")

    sys_prompt = get_system_prompt(carrier_type)
    user_obj = build_user_payload(text, precedent_facts, known_roster)

    resolved_model = model or OPENROUTER_MODEL
    client = get_llm_client()
    content, chat_usage = _call_llm(client, resolved_model, sys_prompt, user_obj)
    chat_usage = {**chat_usage, "model": resolved_model}

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

    norm_lines: list[dict[str, Any]] = []
    for ln in obj.get("lines") or []:
        identifier = _normalize_identifier(ln.get("identifier"))
        evidence_total_line = str(ln.get("evidence_total_line") or "")[:300]
        identifier = _reconcile_phone_last4(identifier, evidence_total_line)
        matched_member_id = _to_optional_int(ln.get("matched_member_id"))

        try:
            line_total = float(ln.get("line_total") or 0.0)
        except Exception:
            line_total = 0.0
        try:
            lconf = float(ln.get("confidence") or 0.5)
        except Exception:
            lconf = 0.5

        norm_charges = []
        for ch in ln.get("charges") or []:
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
                "identifier": identifier,
                "matched_member_id": matched_member_id,
                "display": str(ln.get("display") or "").strip(),
                "line_total": line_total,
                "confidence": lconf,
                "source": str(ln.get("source") or ""),
                "evidence_total_line": evidence_total_line,
                "charges": norm_charges,
            }
        )

    # Models sometimes drop an account-level/shared charge from `lines`
    # entirely (rather than emitting it as an identifier.type=="none" line,
    # as instructed) - if the extracted lines don't sum to invoice.total,
    # synthesize a residual line rather than silently losing that money.
    # This mirrors the legacy pipeline's `unassigned_amount` concept.
    line_sum = round(sum(x["line_total"] for x in norm_lines), 2)
    residual = round(total - line_sum, 2)
    if residual > 0.5:
        norm_lines.append(
            {
                "identifier": {"type": "none", "value": ""},
                "matched_member_id": None,
                "display": f"Unattributed residual (extracted lines summed to ${line_sum:.2f}, invoice total is ${total:.2f})",
                "line_total": residual,
                "confidence": 0.3,
                "source": "synthesized_residual",
                "evidence_total_line": "",
                "charges": [],
            }
        )

    alloc = obj.get("allocation_suggestion") or {}
    alloc_by_line: list[dict[str, Any]] = []
    for a in alloc.get("by_line") or []:
        identifier = _normalize_identifier(a.get("identifier"))
        matched_member_id = _to_optional_int(a.get("matched_member_id"))
        try:
            amt = float(a.get("suggested_amount") or 0.0)
        except Exception:
            amt = 0.0
        alloc_by_line.append({"identifier": identifier, "matched_member_id": matched_member_id, "suggested_amount": amt})

    notes = str(alloc.get("notes") or "").strip()

    if residual > 0.5:
        notes = (
            notes
            + f" | NOTE: added a ${residual:.2f} 'unattributed residual' line - the model's extracted "
            "lines didn't sum to the invoice total (likely a missed account-level charge)."
        ).strip()
    elif residual < -0.5:
        notes = (
            notes
            + f" | WARNING: extracted lines sum (${line_sum:.2f}) EXCEEDS invoice total (${total:.2f}) "
            f"by ${-residual:.2f} - review for duplicate/overlapping charges."
        ).strip()

    s = sum(x["suggested_amount"] for x in alloc_by_line)
    if alloc_by_line and abs(s - total) > 10.0:
        notes = (notes + f" | WARNING: suggested sum {s:.2f} differs from total {total:.2f}").strip()

    return BillProposalV2(
        year=year,
        month=month,
        total_amount=total,
        confidence=conf,
        evidence_total=evidence_total,
        evidence_period=evidence_period,
        lines=norm_lines,
        allocation_by_line=alloc_by_line,
        notes=notes,
        raw_response=content,
        system_prompt=sys_prompt,
        token_usage=chat_usage,
    )
