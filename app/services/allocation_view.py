"""
Shared allocation-view builder for the Bill Import v2 pipeline.

Single source of truth for turning a parsed LLM proposal's `lines` into a
per-member dollar amount - used by all three places that previously each
re-implemented this slightly differently (which caused real bugs, see
docs/decisions/2026-07-04-roadmap/08-llm-bill-import-rag-architecture.md
round 4):
  - the NORMAL-mode review UI (app/ui/bill_import.py)
  - the evaluate-only diff view (app/services/bill_import_worker.py)
  - the eval CLI harness (eval/run_eval.py)

Mirrors exactly what an approval actually writes to the ledger: any money
not claimed by a specific member (shared "none"-type charges, or a real
identifier nobody could be matched to) is split equally across the members
who actually have a line on *this* bill - never guessed onto a roster
member who simply isn't part of this bill/account this month, and never
silently dropped. If nobody on the roster matched a line at all, or a
rounding remainder is left over, that falls to the plan's real owner
(Plan.owner_member_id). If a plan has no owner set, that money is left
unattributed (callers surface it as "remaining to assign" rather than
guessing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import Plan
from app.services import member_identifiers, vectorstore

FROM_BILL = "from_bill"
FROM_HISTORY = "from_history"
OWNER_ABSORBS = "owner_absorbs"
NO_DATA = "none"


@dataclass
class MemberAllocation:
    member_id: int
    name: str
    amount: float = 0.0
    basis: str = NO_DATA
    matched_line_keys: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class AllocationView:
    members: list[MemberAllocation]
    unmatched_lines: int = 0
    unmatched_total: float = 0.0
    none_total: float = 0.0
    owner_member_id: Optional[int] = None
    unattributed_total: float = 0.0  # unmatched_total + none_total not absorbed by anyone (no owner set)


def mean_abs_diff_active(per_member: list[dict[str, Any]]) -> float:
    """
    Mean absolute per-member $ error, counted only over members who actually
    have a stake in this bill (actual != 0 or proposed != 0) - unlike a
    plain mean over every roster member, this doesn't get diluted by
    members who are correctly $0/$0 (not on this bill, no correction
    needed), which otherwise systematically understates the real error
    concentrated among the people the bill is actually about. Falls back to
    0.0 if literally nobody has a nonzero actual or proposed amount.
    """
    active = [p for p in per_member if abs(p.get("actual") or 0.0) > 0.005 or abs(p.get("proposed") or 0.0) > 0.005]
    if not active:
        return 0.0
    return round(sum(abs(p.get("diff") or 0.0) for p in active) / len(active), 2)


def build_member_allocation_view(
    db: Session,
    plan_id: int,
    roster: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    bill_period_key: int | None = None,
    lookback_months: int | None = None,
) -> AllocationView:
    """
    One MemberAllocation per roster member, guaranteed (up to rounding) to
    sum to the bill's total_amount:
      1. Sum of this bill's matched lines (identifier resolved to them).
      2. The "unclaimed" pool (identifier.type=="none" shared charges, plus
         any real identifier nobody could be matched to) is split EQUALLY
         across only the members who actually matched a line on *this*
         bill - e.g. a shared "Account" charge is split between the people
         who have a phone line on this month's bill, not the whole roster.
         Roster members with no line on this bill get $0 by default (not a
         history-based guess stacked on top of the bill's own total - that
         double-counts money and breaks reconciliation the moment a plan
         has roster members who simply aren't part of this particular
         bill/account). Their last known amount (if any) is still surfaced
         as an informational note so a human can tell "not on this bill"
         apart from "on this bill for $0", and can always add them back in
         manually.
      3. Whatever's left after that (no one matched a line on this bill at
         all, or a rounding remainder from the equal split) falls to the
         plan's owner (Plan.owner_member_id). If no owner is set, it's left
         unattributed rather than guessed.

    Mutates `lines` in place (matched_member_id/match_source), via
    member_identifiers.apply_deterministic_matches() + dedupe_llm_suggestions()
    - always re-run so a mapping saved after a job finished is picked up on
    the very next call, and so a fresh call (e.g. from eval/run_eval.py
    against a different model's raw output) always gets the same
    guardrails NORMAL-mode jobs get.
    """
    member_identifiers.apply_deterministic_matches(db, plan_id, lines)
    member_identifiers.dedupe_llm_suggestions(lines)

    by_member: dict[int, MemberAllocation] = {
        int(r["member_id"]): MemberAllocation(member_id=int(r["member_id"]), name=r.get("name") or f"Member {r['member_id']}")
        for r in roster
    }

    unmatched_lines = 0
    unmatched_total = 0.0
    none_total = 0.0

    for ln in lines:
        ident = ln.get("identifier") or {"type": "none", "value": ""}
        line_total = float(ln.get("line_total") or 0.0)
        if ident.get("type") == "none":
            none_total += line_total
            continue
        mid = ln.get("matched_member_id")
        if mid is not None and int(mid) in by_member:
            alloc = by_member[int(mid)]
            alloc.amount = round(alloc.amount + line_total, 2)
            alloc.basis = FROM_BILL
            alloc.matched_line_keys.append(f"{ident.get('type')}:{ident.get('value')}")
        else:
            unmatched_lines += 1
            unmatched_total += line_total

    plan = db.get(Plan, int(plan_id)) if plan_id else None
    owner_id = int(plan.owner_member_id) if plan and plan.owner_member_id else None

    unclaimed = round(unmatched_total + none_total, 2)
    on_bill_ids = [mid for mid, a in by_member.items() if a.basis == FROM_BILL]
    no_data_ids = [mid for mid, a in by_member.items() if a.basis == NO_DATA]

    # Informational only - members with no line on this bill still get their
    # last known amount surfaced as a note, but it is never added to `amount`.
    if no_data_ids and plan_id:
        history = vectorstore.get_latest_amount_per_member(
            plan_id, no_data_ids, before_period_key=bill_period_key, lookback_months=lookback_months
        )
        for mid, info in history.items():
            by_member[mid].detail = f"not on this bill (last time: ${float(info['amount']):.2f}, {info['label']})"

    distributed = 0.0
    if unclaimed > 0.005 and on_bill_ids:
        share = round(unclaimed / len(on_bill_ids), 2)
        for mid in on_bill_ids:
            alloc = by_member[mid]
            alloc.amount = round(alloc.amount + share, 2)
            alloc.detail = (alloc.detail + f" + ${share:.2f} equal share of ${unclaimed:.2f} shared/unassigned").strip(" +")
            distributed += share

    leftover = round(unclaimed - distributed, 2)
    unattributed_total = 0.0
    if leftover > 0.005:
        if owner_id is not None and owner_id in by_member:
            alloc = by_member[owner_id]
            alloc.amount = round(alloc.amount + leftover, 2)
            if alloc.basis == NO_DATA:
                alloc.basis = OWNER_ABSORBS
                alloc.detail = f"owner absorbs ${leftover:.2f} shared/unmatched"
            else:
                alloc.detail = (alloc.detail + f" + owner absorbs ${leftover:.2f} shared/unmatched").strip(" +")
        else:
            unattributed_total = leftover

    for alloc in by_member.values():
        alloc.amount = round(alloc.amount, 2)

    return AllocationView(
        members=list(by_member.values()),
        unmatched_lines=unmatched_lines,
        unmatched_total=round(unmatched_total, 2),
        none_total=round(none_total, 2),
        owner_member_id=owner_id,
        unattributed_total=round(unattributed_total, 2),
    )
