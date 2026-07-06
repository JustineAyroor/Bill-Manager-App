"""
Carrier-aware prompt registry, keyed off Plan.carrier_type.

Plan.carrier_type is free text entered via the Plans tab UI (e.g.
"T-Mobile", "tmobile", "Verizon"), so it's normalized (lowercased, stripped
of non-alphanumeric characters) before comparison - a typo like "tmobile"
should still resolve to the T-Mobile prompt, not silently fall through to
the generic one.
"""

from __future__ import annotations

import re

from app.services.bill_prompts.generic import GENERIC_SYSTEM_PROMPT
from app.services.bill_prompts.tmobile import TMOBILE_SYSTEM_PROMPT

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def _normalize_carrier(carrier_type: str | None) -> str:
    t = (carrier_type or "").strip().lower()
    return _NON_ALNUM_RE.sub("", t)


def get_system_prompt(carrier_type: str | None) -> str:
    """T-Mobile prompt when carrier_type matches (or is unset - the historical default); generic otherwise."""
    normalized = _normalize_carrier(carrier_type)
    if not normalized or normalized == "tmobile":
        return TMOBILE_SYSTEM_PROMPT
    return GENERIC_SYSTEM_PROMPT
