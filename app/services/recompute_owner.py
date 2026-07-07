from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.db.models import Member, Invoice, Allocation, Plan
from app.services.crud import upsert_allocation
from app.services import plans as plans_service

OWNER_NAME = "Justine"


def _resolve_plan_owner(db: Session, plan: Plan | None) -> Member:
    """
    Resolve the owner member for a plan, falling back to the legacy
    hardcoded OWNER_NAME lookup (and self-healing the plan's owner_member_id)
    if the plan doesn't have one set yet.
    """
    if plan and plan.owner_member_id:
        owner = db.get(Member, plan.owner_member_id)
        if owner:
            return owner

    owner = db.execute(select(Member).where(Member.name == OWNER_NAME)).scalar_one_or_none()
    if not owner:
        owner = Member(name=OWNER_NAME, is_active=1)
        db.add(owner)
        db.flush()

    if plan and not plan.owner_member_id:
        plan.owner_member_id = owner.id
        plans_service.add_member_to_plan(db, plan.id, owner.id)

    return owner


def recompute_owner_allocation(db: Session, invoice_id: int) -> None:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return

    plan = db.get(Plan, inv.plan_id) if inv.plan_id else None
    owner = _resolve_plan_owner(db, plan)

    others_sum = db.execute(
        select(func.coalesce(func.sum(Allocation.amount_due), 0.0))
        .where(Allocation.invoice_id == invoice_id, Allocation.member_id != owner.id)
    ).scalar_one()

    owner_due = max(float(inv.total_amount or 0.0) - float(others_sum or 0.0), 0.0)

    upsert_allocation(db, invoice_id=invoice_id, member_id=owner.id, amount_due=owner_due)
