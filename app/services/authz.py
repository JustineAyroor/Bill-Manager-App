"""
Plan-level authorization helpers.

Permission model (see docs/decisions/2026-07-04-roadmap for background):
- OWNER (the single app admin login) has full read/write access to every
  plan, plus a synthetic "All Plans (combined)" view for cross-plan totals.
- MEMBER users can create new plans (becoming that plan's owner). Within a
  plan, only the member designated as `Plan.owner_member_id` can write
  (invoices, allocations, payments, membership); any other member who
  merely belongs to the plan gets read-only access. Members with no
  relationship to a plan at all cannot see it.

Member-record permission model:
- Removing a member from a plan is an OWNER-only action - no MEMBER, even a
  plan owner, may remove another person from a plan.
- Editing a member's profile (name/contact/reminder prefs): OWNER can edit
  anyone; a MEMBER can only edit a member they personally created
  (`Member.created_by_member_id`). Members with no recorded creator (legacy
  data, or created by the OWNER) are editable only by the OWNER.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Member
from app.services import plans as plans_service

ALL_PLANS_CHOICE = "ALL | All Plans (combined)"


def _role(role: str | None) -> str:
    return (role or "").strip().upper()


def can_manage_plan(db: Session, role: str | None, member_id: int | None, plan_id: int | None) -> bool:
    """True if this user may create/edit invoices, allocations, payments, or membership for plan_id."""
    if _role(role) == "OWNER":
        return True
    if _role(role) != "MEMBER" or not member_id or not plan_id:
        return False
    plan = plans_service.get_plan(db, int(plan_id))
    if not plan or plan.owner_member_id is None:
        return False
    return int(plan.owner_member_id) == int(member_id)


def can_view_plan(db: Session, role: str | None, member_id: int | None, plan_id: int | None) -> bool:
    """True if this user may see (read-only, at least) plan_id's data."""
    if _role(role) == "OWNER":
        return True
    if not plan_id:
        return False
    if can_manage_plan(db, role, member_id, plan_id):
        return True
    if not member_id:
        return False
    member_plan_ids = {p.id for p in plans_service.get_member_plans(db, int(member_id))}
    return int(plan_id) in member_plan_ids


def accessible_plan_choices(db: Session, role: str | None, member_id: int | None) -> list[str]:
    """Dropdown choices for the global "Active plan" selector, scoped to what this user may see."""
    if _role(role) == "OWNER":
        choices = [ALL_PLANS_CHOICE]
        choices.extend(f"{p.id} | {p.name}" for p in plans_service.list_plans(db))
        return choices

    if _role(role) != "MEMBER" or not member_id:
        return []

    choices = []
    for p in plans_service.get_member_plans(db, int(member_id)):
        tag = "you manage" if p.owner_member_id == int(member_id) else "view only"
        choices.append(f"{p.id} | {p.name} ({tag})")
    return choices


def parse_plan_choice(choice: str | None) -> int | None:
    """Parse a choice from accessible_plan_choices() back to a plan_id, or None for "All Plans"."""
    if not choice or is_all_plans_choice(choice):
        return None
    return plans_service.parse_plan_choice(choice)


def is_all_plans_choice(choice: str | None) -> bool:
    return bool(choice) and str(choice).strip().upper().startswith("ALL")


def default_plan_choice(choices: list[str], role: str | None) -> str | None:
    """Pick a sensible default value for the Active plan dropdown."""
    if not choices:
        return None
    if _role(role) == "OWNER":
        return choices[0]  # "All Plans (combined)"
    return choices[0]


def can_delete_plan_member(role: str | None) -> bool:
    """Removing a member from a plan is restricted to the application OWNER."""
    return _role(role) == "OWNER"


def can_manage_member(db: Session, role: str | None, member_id: int | None, target_member_id: int | None) -> bool:
    """True if this user may edit target_member_id's profile/contact/reminder prefs."""
    if _role(role) == "OWNER":
        return True
    if _role(role) != "MEMBER" or not member_id or not target_member_id:
        return False
    target = db.get(Member, int(target_member_id))
    if not target or target.created_by_member_id is None:
        return False
    return int(target.created_by_member_id) == int(member_id)


def manageable_member_ids(db: Session, role: str | None, member_id: int | None) -> set[int]:
    """Member ids this user may edit (not counting OWNER, which can edit everyone)."""
    if _role(role) != "MEMBER" or not member_id:
        return set()
    rows = db.query(Member.id).filter(Member.created_by_member_id == int(member_id)).all()
    return {r[0] for r in rows}
