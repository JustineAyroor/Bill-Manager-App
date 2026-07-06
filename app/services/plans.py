from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Member, Plan, PlanMember

DEFAULT_PLAN_NAME = "Default Plan"


def list_plans(db: Session) -> list[Plan]:
    return list(db.execute(select(Plan).order_by(Plan.name)).scalars().all())


def get_plan(db: Session, plan_id: int) -> Plan | None:
    return db.get(Plan, int(plan_id))


def get_default_plan(db: Session) -> Plan | None:
    """
    The plan created by the multi-plan migration to hold all pre-existing
    data. Used as a fallback when no plan has been explicitly selected yet
    (e.g. first run after upgrading, or callers that haven't been updated to
    pass a plan_id).
    """
    plan = db.execute(select(Plan).where(Plan.name == DEFAULT_PLAN_NAME)).scalar_one_or_none()
    if plan:
        return plan
    return db.execute(select(Plan).order_by(Plan.id)).scalars().first()


def resolve_plan_id(db: Session, plan_id: int | None) -> int | None:
    """Validate a requested plan_id, or fall back to the default/first plan."""
    if plan_id is not None:
        plan = get_plan(db, plan_id)
        if plan:
            return plan.id
    plan = get_default_plan(db)
    return plan.id if plan else None


def create_plan(db: Session, name: str, carrier_type: str | None = None, owner_member_id: int | None = None) -> Plan:
    name = (name or "").strip()
    if not name:
        raise ValueError("Plan name is required.")
    existing = db.execute(select(Plan).where(Plan.name == name)).scalar_one_or_none()
    if existing:
        raise ValueError(f"A plan named '{name}' already exists.")
    plan = Plan(name=name, carrier_type=(carrier_type or "").strip() or None, owner_member_id=owner_member_id)
    db.add(plan)
    db.flush()
    if owner_member_id:
        add_member_to_plan(db, plan.id, owner_member_id)
    return plan


def set_plan_owner(db: Session, plan_id: int, owner_member_id: int | None) -> None:
    plan = get_plan(db, plan_id)
    if not plan:
        raise ValueError("Plan not found.")
    plan.owner_member_id = owner_member_id
    if owner_member_id:
        add_member_to_plan(db, plan_id, owner_member_id)


def get_plan_owner(db: Session, plan_id: int) -> Member | None:
    plan = get_plan(db, plan_id)
    if not plan or not plan.owner_member_id:
        return None
    return db.get(Member, plan.owner_member_id)


def get_plan_members(db: Session, plan_id: int) -> list[Member]:
    return list(
        db.execute(
            select(Member)
            .join(PlanMember, PlanMember.member_id == Member.id)
            .where(PlanMember.plan_id == plan_id)
            .order_by(Member.name)
        ).scalars().all()
    )


def get_member_plans(db: Session, member_id: int) -> list[Plan]:
    return list(
        db.execute(
            select(Plan)
            .join(PlanMember, PlanMember.plan_id == Plan.id)
            .where(PlanMember.member_id == member_id)
            .order_by(Plan.name)
        ).scalars().all()
    )


def add_member_to_plan(db: Session, plan_id: int, member_id: int) -> None:
    existing = db.execute(
        select(PlanMember).where(PlanMember.plan_id == plan_id, PlanMember.member_id == member_id)
    ).scalar_one_or_none()
    if existing:
        return
    db.add(PlanMember(plan_id=plan_id, member_id=member_id))
    db.flush()


def remove_member_from_plan(db: Session, plan_id: int, member_id: int) -> None:
    existing = db.execute(
        select(PlanMember).where(PlanMember.plan_id == plan_id, PlanMember.member_id == member_id)
    ).scalar_one_or_none()
    if existing:
        db.delete(existing)


def plan_choice_list(db: Session) -> list[str]:
    return [f"{p.id} | {p.name}" for p in list_plans(db)]


def parse_plan_choice(choice: str | None) -> int | None:
    if not choice:
        return None
    try:
        return int(str(choice).split("|", 1)[0].strip())
    except Exception:
        return None
