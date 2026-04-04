from sqlalchemy.orm import Session
from sqlalchemy import select, func
from app.db.models import Member, Invoice, Allocation
from app.services.crud import upsert_allocation

OWNER_NAME = "Justine"

def recompute_owner_allocation(db: Session, invoice_id: int) -> None:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        return

    owner = db.execute(select(Member).where(Member.name == OWNER_NAME)).scalar_one_or_none()
    if not owner:
        owner = Member(name=OWNER_NAME, is_active=1)
        db.add(owner)
        db.flush()

    others_sum = db.execute(
        select(func.coalesce(func.sum(Allocation.amount_due), 0.0))
        .select_from(Allocation)
        .join(Member, Member.id == Allocation.member_id)
        .where(Allocation.invoice_id == invoice_id, Member.name != OWNER_NAME)
    ).scalar_one()

    owner_due = max(float(inv.total_amount or 0.0) - float(others_sum or 0.0), 0.0)

    upsert_allocation(db, invoice_id=invoice_id, member_id=owner.id, amount_due=owner_due)