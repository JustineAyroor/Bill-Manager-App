from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import Member, Invoice, Allocation, Payment, User
from app.services import plans as plans_service


def get_or_create_member(
    db: Session,
    name: str,
    email: str | None = None,
    phone: str | None = None,
    plan_id: int | None = None,
    created_by_member_id: int | None = None,
) -> Member:
    m = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if m:
        # Update contact info if provided
        if email and not m.email:
            m.email = email
        if phone and not m.phone:
            m.phone = phone
    else:
        m = Member(
            name=name.strip(),
            email=email,
            phone=phone,
            is_active=1,
            created_by_member_id=created_by_member_id,
        )
        db.add(m)
        db.flush()
    if plan_id:
        plans_service.add_member_to_plan(db, plan_id, m.id)
    return m


def list_members(db: Session, plan_id: int | None = None) -> list[Member]:
    if plan_id is None:
        return list(db.execute(select(Member).order_by(Member.name)).scalars().all())
    return plans_service.get_plan_members(db, plan_id)


def list_member_users(db: Session) -> list[User]:
    return list(
        db.execute(
            select(User)
            .where(User.role == "MEMBER")
            .order_by(User.email)
        ).scalars().all()
    )


def get_member_user_by_email(db: Session, email: str) -> User | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    return db.execute(select(User).where(User.email == normalized)).scalar_one_or_none()

def update_invoice_total(db, invoice_id: int, total_amount: float) -> None:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    inv.total_amount = float(total_amount)

def upsert_invoice(db: Session, plan_id: int, year: int, month: str, total_amount: float | None = None):
    if not plan_id:
        raise ValueError("plan_id is required to create/update an invoice.")

    inv = db.execute(
        select(Invoice).where(Invoice.plan_id == plan_id, Invoice.year == year, Invoice.month == month)
    ).scalar_one_or_none()

    if inv:
        # ✅ update fields if provided
        if total_amount is not None:
            inv.total_amount = float(total_amount)
        return inv

    inv = Invoice(plan_id=plan_id, year=year, month=month, total_amount=float(total_amount or 0.0))
    db.add(inv)
    db.flush()
    return inv


def list_invoices(db: Session, plan_id: int | None = None) -> list[Invoice]:
    stmt = select(Invoice).order_by(Invoice.year.desc(), Invoice.id.desc())
    if plan_id is not None:
        stmt = select(Invoice).where(Invoice.plan_id == plan_id).order_by(Invoice.year.desc(), Invoice.id.desc())
    return list(db.execute(stmt).scalars().all())
    
def upsert_allocation(db: Session, invoice_id: int, member_id: int, amount_due: float) -> Allocation:
    alloc = db.execute(
        select(Allocation).where(Allocation.invoice_id == invoice_id, Allocation.member_id == member_id)
    ).scalar_one_or_none()

    if alloc:
        alloc.amount_due = float(amount_due or 0.0)
        return alloc

    alloc = Allocation(invoice_id=invoice_id, member_id=member_id, amount_due=float(amount_due or 0.0))
    db.add(alloc)
    db.flush()
    return alloc

def add_payment(db, plan_id, when, amount, direction, description=None, member_id=None, invoice_id=None):
    if not plan_id:
        raise ValueError("plan_id is required to record a payment.")
    p = Payment(
        plan_id=plan_id,
        date=when,
        amount=float(amount),
        direction=direction,
        description=description,
        member_id=member_id,
        invoice_id=invoice_id,
    )
    db.add(p)
    db.flush()
    return p


def update_payment(db, payment_id, when, amount, direction, description=None, member_id=None, invoice_id=None, plan_id=None):
    p = db.get(Payment, int(payment_id))
    if not p:
        raise ValueError(f"Payment not found: {payment_id}")

    p.date = when
    p.amount = float(amount)
    p.direction = direction
    p.description = description
    p.member_id = member_id
    p.invoice_id = invoice_id
    if plan_id:
        p.plan_id = plan_id
    return p


def delete_payment(db, payment_id):
    p = db.get(Payment, int(payment_id))
    if not p:
        raise ValueError(f"Payment not found: {payment_id}")
    db.delete(p)
