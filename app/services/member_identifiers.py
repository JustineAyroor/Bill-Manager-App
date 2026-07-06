"""
Generalized member-matching identifiers for the Bill Import v2 pipeline.

T-Mobile bills identify a line by phone number; a generic bill might only
have an email, a name, an account holder field, or nothing splittable at
all. This module generalizes the legacy Bill Import "save mapping to DB"
feature (which only ever wrote Member.phone_last4) to any identifier type,
via the additive `MemberIdentifier` table - the legacy column/behavior is
untouched.

Matching here is intentionally exact/normalized only (never fuzzy) - two
members with similar names should never be silently cross-matched by code.
Fuzzy matching (e.g. "J. Smith" vs "John Smith") is deliberately left to the
LLM-suggestion + human-confirmation path in llm_invoice_extract_v2.py /
app/ui/bill_import.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Member, MemberIdentifier
from app.services import plans as plans_service

PHONE_LAST4 = "PHONE_LAST4"
EMAIL = "EMAIL"
NAME = "NAME"
ACCOUNT = "ACCOUNT"
NONE_TYPE = "none"

VALID_TYPES = {PHONE_LAST4, EMAIL, NAME, ACCOUNT}

_WHITESPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\D+")


@dataclass(frozen=True)
class Identifier:
    type: str  # "phone" | "email" | "name" | "account" | "none"
    value: str = ""


def _normalize_phone(value: str) -> str:
    digits = _DIGITS_RE.sub("", value or "")
    return digits[-4:] if len(digits) >= 4 else digits


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_name(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", (value or "").strip().lower())


def _normalize_account(value: str) -> str:
    return (value or "").strip().lower()


def _normalize_value(identifier_type: str, value: str) -> str:
    if identifier_type == PHONE_LAST4:
        return _normalize_phone(value)
    if identifier_type == EMAIL:
        return _normalize_email(value)
    if identifier_type == NAME:
        return _normalize_name(value)
    if identifier_type == ACCOUNT:
        return _normalize_account(value)
    return (value or "").strip()


def _extraction_type_to_db_type(extraction_type: str) -> Optional[str]:
    t = (extraction_type or "").strip().lower()
    return {
        "phone": PHONE_LAST4,
        "email": EMAIL,
        "name": NAME,
        "account": ACCOUNT,
    }.get(t)


def match_member(db: Session, plan_id: int, identifier: Identifier | dict[str, Any]) -> Optional[int]:
    """
    Exact/normalized lookup only - phone -> email -> name, each plan-scoped
    first then global. Returns None (never a guess) if nothing matches or
    identifier.type == "none".
    """
    if isinstance(identifier, dict):
        identifier = Identifier(type=str(identifier.get("type") or ""), value=str(identifier.get("value") or ""))

    if not identifier or not identifier.type or identifier.type.strip().lower() == NONE_TYPE:
        return None

    db_type = _extraction_type_to_db_type(identifier.type)
    if not db_type:
        return None

    norm_value = _normalize_value(db_type, identifier.value)
    if not norm_value:
        return None

    # Plan-scoped rows win over global ones; within each scope, compare
    # normalized values in Python since normalization (e.g. digit-only last4,
    # lowercased email) isn't something SQLite can do for us portably.
    for scope_plan_id in (int(plan_id) if plan_id else None, None):
        stmt = select(MemberIdentifier.member_id, MemberIdentifier.identifier_value).where(
            MemberIdentifier.identifier_type == db_type,
        )
        stmt = stmt.where(
            MemberIdentifier.plan_id == scope_plan_id if scope_plan_id is not None else MemberIdentifier.plan_id.is_(None)
        )
        for member_id, value in db.execute(stmt).all():
            if _normalize_value(db_type, value) == norm_value:
                return int(member_id)
        if plan_id is None:
            # No plan-scoped pass to run separately from the global one.
            break

    return None


def match_member_any(db: Session, plan_id: int, identifier: Identifier | dict[str, Any]) -> Optional[int]:
    """
    Convenience wrapper matching decision 10's priority order when the
    identifier's own declared type doesn't resolve: phone -> email -> name.
    Only used defensively; the primary path is match_member() using the
    LLM-extracted identifier.type directly.
    """
    if isinstance(identifier, dict):
        identifier = Identifier(type=str(identifier.get("type") or ""), value=str(identifier.get("value") or ""))
    return match_member(db, plan_id, identifier)


def save_identifier(
    db: Session,
    plan_id: Optional[int],
    member_id: int,
    identifier_type: str,
    identifier_value: str,
) -> MemberIdentifier:
    """Generalized 'save mapping' write - upserts on (member_id, plan_id, identifier_type, identifier_value)."""
    db_type = identifier_type if identifier_type in VALID_TYPES else _extraction_type_to_db_type(identifier_type)
    if not db_type:
        raise ValueError(f"Unknown identifier_type: {identifier_type}")

    norm_value = _normalize_value(db_type, identifier_value)
    if not norm_value:
        raise ValueError("Empty identifier_value")

    existing = db.execute(
        select(MemberIdentifier).where(
            MemberIdentifier.member_id == int(member_id),
            MemberIdentifier.identifier_type == db_type,
            MemberIdentifier.plan_id == (int(plan_id) if plan_id else None),
        )
    ).scalars().first()
    if existing:
        existing.identifier_value = identifier_value.strip()
        db.flush()
        return existing

    row = MemberIdentifier(
        member_id=int(member_id),
        plan_id=int(plan_id) if plan_id else None,
        identifier_type=db_type,
        identifier_value=identifier_value.strip(),
    )
    db.add(row)
    db.flush()
    return row


def apply_deterministic_matches(db: Session, plan_id: int, lines: list[dict[str, Any]]) -> None:
    """
    Mutates `lines` in place: sets matched_member_id + match_source per line.
    A deterministic match (a real saved MemberIdentifier) always wins over
    the LLM's own suggestion; the LLM's guess is kept only as a
    low-confidence suggestion for lines the deterministic lookup couldn't
    resolve. Idempotent and cheap (just DB lookups) - safe to call more than
    once on the same lines (e.g. every time the review UI polls, so a
    mapping saved after the job finished is picked up on the very next
    poll without re-running the LLM).
    """
    for ln in lines:
        det_match = match_member(db, plan_id, ln.get("identifier"))
        if det_match is not None:
            ln["matched_member_id"] = det_match
            ln["match_source"] = "deterministic"
        elif ln.get("matched_member_id") is not None:
            ln["match_source"] = "llm_suggestion"
        else:
            ln["match_source"] = None


def dedupe_llm_suggestions(lines: list[dict[str, Any]]) -> None:
    """
    Mutates `lines` in place. Call after apply_deterministic_matches().

    A deterministic match may legitimately "own" a member across multiple
    lines (e.g. one person with two phone lines on the account) - that's
    real evidence, not a guess. An LLM suggestion (match_source ==
    "llm_suggestion", no saved identifier backing it) may NOT: if it
    collides with a member already claimed deterministically, or if two
    lines both guess the same member via suggestion with no deterministic
    claim, none of the colliding guesses are trustworthy enough to accept
    silently - they're cleared back to unmatched so a human resolves it via
    the "unresolved charges" step instead of the model silently doubling
    someone's bill or leaving someone else at $0.

    This is a code-level backstop for a real, observed failure mode where
    the model's own prompt instructions not to do this weren't reliably
    followed - see docs/decisions/.../08-llm-bill-import-rag-architecture.md
    round 4.
    """
    deterministic_members = {
        int(ln["matched_member_id"])
        for ln in lines
        if ln.get("match_source") == "deterministic" and ln.get("matched_member_id") is not None
    }

    suggestion_member_counts: dict[int, int] = {}
    for ln in lines:
        if ln.get("match_source") == "llm_suggestion" and ln.get("matched_member_id") is not None:
            mid = int(ln["matched_member_id"])
            suggestion_member_counts[mid] = suggestion_member_counts.get(mid, 0) + 1

    for ln in lines:
        if ln.get("match_source") != "llm_suggestion" or ln.get("matched_member_id") is None:
            continue
        mid = int(ln["matched_member_id"])
        if mid in deterministic_members or suggestion_member_counts.get(mid, 0) > 1:
            ln["matched_member_id"] = None
            ln["match_source"] = None


def build_known_roster(db: Session, plan_id: int) -> list[dict[str, Any]]:
    """
    [{member_id, name, known_identifiers: [{type, value}]}] for every member
    of plan_id - fed into the v2 prompt so the LLM can propose a
    matched_member_id per line using its fuzzy-matching strength. Cheap:
    bounded by member count, not bill size.
    """
    members = plans_service.get_plan_members(db, int(plan_id))
    roster: list[dict[str, Any]] = []
    for m in members:
        rows = db.execute(
            select(MemberIdentifier.identifier_type, MemberIdentifier.identifier_value).where(
                MemberIdentifier.member_id == m.id,
                (MemberIdentifier.plan_id == int(plan_id)) | (MemberIdentifier.plan_id.is_(None)),
            )
        ).all()
        known = [{"type": t, "value": v} for (t, v) in rows]
        roster.append({"member_id": m.id, "name": m.name, "known_identifiers": known})
    return roster
