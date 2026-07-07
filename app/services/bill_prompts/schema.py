"""
Shared output schema + user-payload builder for the Bill Import v2 prompts.

Both app/services/bill_prompts/tmobile.py and generic.py emit this same JSON
shape - only the *system* prompt (bill-layout-specific guidance) differs by
carrier. Notably, `identifier` generalizes the legacy phone_key-only field
(app/services/llm_invoice_extract.py) to phone/email/name/account/none, and
`matched_member_id` carries the LLM's optional fuzzy-match suggestion against
the plan's known_members roster (see app/services/member_identifiers.py).
"""

from __future__ import annotations

from typing import Any

OUTPUT_SCHEMA_V2: dict[str, Any] = {
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
            "identifier": {"type": "phone|email|name|account|none", "value": "string"},
            "matched_member_id": "int_or_null - only if confident, from known_members",
            "display": "string",
            "line_total": "float",
            "confidence": "float_0_to_1",
            "source": "string",
            "evidence_total_line": "string",
            "charges": [{"label": "string", "amount": "float", "evidence": "string"}],
        }
    ],
    "allocation_suggestion": {
        "method": "string",
        "by_line": [
            {
                "identifier": {"type": "phone|email|name|account|none", "value": "string"},
                "matched_member_id": "int_or_null",
                "suggested_amount": "float",
            }
        ],
        "notes": "string",
    },
}


def build_user_payload(
    text: str,
    precedent_facts: list[str] | None = None,
    known_roster: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Shared user-message payload for both carrier prompts.

    - `text` is the already chunk-selected bill text (top-N relevant chunks,
      rejoined) - not the raw/trimmed full text the legacy pipeline sends.
    - `precedent_facts` are compact strings retrieved from the vector store
      (already-computed outcome facts from past approved bills), never a
      fresh LLM summarization call.
    - `known_roster` is this plan's members + already-known identifiers
      (cheap - bounded by member count, not bill size).
    """
    return {
        "task": (
            "Extract invoice period+total and propose allocations by charge line, following the "
            "identifier rules in the system prompt. Use known_members to suggest matched_member_id "
            "only when you recognize a clear match - a human will confirm every mapping before it is used."
        ),
        "precedent_facts": precedent_facts or [],
        "known_members": known_roster or [],
        "text": (text or "")[:24000],
        "output_schema": OUTPUT_SCHEMA_V2,
    }
