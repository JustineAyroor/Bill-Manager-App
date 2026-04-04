from datetime import date
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.db.models import Member, Invoice, Allocation, Payment


def get_or_create_member(db: Session, name: str, email: str | None = None, phone: str | None = None) -> Member:
    m = db.execute(select(Member).where(Member.name == name)).scalar_one_or_none()
    if m:
        # Update contact info if provided
        if email and not m.email:
            m.email = email
        if phone and not m.phone:
            m.phone = phone
        return m
    m = Member(name=name.strip(), email=email, phone=phone, is_active=1)
    db.add(m)
    db.flush()
    return m


def list_members(db: Session) -> list[Member]:
    return list(db.execute(select(Member).order_by(Member.name)).scalars().all())

def update_invoice_total(db, invoice_id: int, total_amount: float) -> None:
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise ValueError("Invoice not found")
    inv.total_amount = float(total_amount)

def upsert_invoice(db: Session, year: int, month: str, total_amount: float | None = None):
    inv = db.execute(select(Invoice).where(Invoice.year == year, Invoice.month == month)).scalar_one_or_none()

    if inv:
        # ✅ update fields if provided
        if total_amount is not None:
            inv.total_amount = float(total_amount)
        return inv

    inv = Invoice(year=year, month=month, total_amount=float(total_amount or 0.0))
    db.add(inv)
    db.flush()
    return inv
    
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

def add_payment(db, when, amount, direction, description=None, member_id=None, invoice_id=None):
    p = Payment(
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


def update_payment(db, payment_id, when, amount, direction, description=None, member_id=None, invoice_id=None):
    p = db.get(Payment, int(payment_id))
    if not p:
        raise ValueError(f"Payment not found: {payment_id}")

    p.date = when
    p.amount = float(amount)
    p.direction = direction
    p.description = description
    p.member_id = member_id
    p.invoice_id = invoice_id
    return p


def delete_payment(db, payment_id):
    p = db.get(Payment, int(payment_id))
    if not p:
        raise ValueError(f"Payment not found: {payment_id}")
    db.delete(p)