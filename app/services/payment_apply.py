from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy.orm import Session
from sqlalchemy import select, func, case

from app.db.models import Payment, PaymentApplication, Allocation, Invoice, Member


MONTH_NUM = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sept": 9, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}


@dataclass
class ApplyRow:
    invoice_id: int
    invoice_label: str
    due: float
    previously_applied: float
    applied_now: float
    remaining_after: float


def _invoice_month_case():
    # SQL CASE to map month abbrev -> month number for correct ordering
    whens = {k: v for k, v in MONTH_NUM.items()}
    return case(whens, value=Invoice.month, else_=99)


def clear_payment_applications(db: Session, payment_id: int) -> None:
    db.query(PaymentApplication).filter(PaymentApplication.payment_id == payment_id).delete(synchronize_session=False)


def clear_member_applications(db: Session, member_id: int) -> None:
    db.query(PaymentApplication).filter(PaymentApplication.member_id == member_id).delete(synchronize_session=False)


def member_unapplied_credit(db: Session, member_id: int) -> float:
    inbound_total = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0.0))
        .where(Payment.direction == "INBOUND", Payment.member_id == member_id)
    ).scalar_one()

    applied_total = db.execute(
        select(func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0))
        .where(PaymentApplication.member_id == member_id)
    ).scalar_one()

    return float(inbound_total) - float(applied_total)


def auto_apply_payment_fifo(db: Session, payment_id: int) -> tuple[list[ApplyRow], float]:
    """
    Apply THIS inbound payment FIFO to oldest unpaid allocations for that member.
    Safe to run for new payments.
    """
    p = db.get(Payment, int(payment_id))
    if not p:
        raise ValueError(f"Payment not found: {payment_id}")
    if p.direction != "INBOUND":
        raise ValueError("Only INBOUND payments can be auto-applied.")
    if not p.member_id:
        raise ValueError("INBOUND payment must have member_id.")

    # If re-applying this payment, clear its own apps (does NOT touch other payments)
    clear_payment_applications(db, p.id)
    db.flush()
    remaining = float(p.amount or 0.0)
    if remaining <= 0:
        return ([], 0.0)

    month_case = _invoice_month_case()

    allocs = db.execute(
        select(
            Allocation.invoice_id,
            Allocation.amount_due,
            Invoice.year,
            Invoice.month,
        )
        .join(Invoice, Invoice.id == Allocation.invoice_id)
        .where(Allocation.member_id == p.member_id)
        .order_by(Invoice.year.asc(), month_case.asc(), Allocation.id.asc())
    ).all()

    results: list[ApplyRow] = []

    for inv_id, due_amt, inv_year, inv_month in allocs:
        if remaining <= 0:
            break

        due = float(due_amt or 0.0)

        prev = db.execute(
            select(func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0))
            .where(
                PaymentApplication.member_id == p.member_id,
                PaymentApplication.invoice_id == inv_id
            )
        ).scalar_one()
        prev = float(prev or 0.0)

        unpaid = max(due - prev, 0.0)
        if unpaid <= 0:
            continue

        apply_now = min(unpaid, remaining)

        db.add(PaymentApplication(
            payment_id=p.id,
            invoice_id=inv_id,
            member_id=p.member_id,
            amount_applied=apply_now,
        ))

        remaining -= apply_now

        label = f"{inv_year}-{inv_month}"
        results.append(ApplyRow(
            invoice_id=int(inv_id),
            invoice_label=label,
            due=due,
            previously_applied=prev,
            applied_now=apply_now,
            remaining_after=max(unpaid - apply_now, 0.0),
        ))
    db.flush()
    return results, float(remaining)


def reconcile_member_fifo(db: Session, member_id: int) -> dict:
    """
    Rebuild ALL applications for a member from scratch based on all inbound payments (chronological).
    This is the safe operation after editing/deleting any payment for that member.
    """
    m = db.get(Member, int(member_id))
    if not m:
        raise ValueError(f"Member not found: {member_id}")

    # Remove existing applications for this member
    clear_member_applications(db, m.id)
    db.flush()
    # Re-apply each inbound payment in time order
    payments = db.execute(
        select(Payment.id, Payment.amount, Payment.date)
        .where(Payment.direction == "INBOUND", Payment.member_id == m.id)
        .order_by(Payment.date.asc(), Payment.id.asc())
    ).all()

    total_inbound = sum(float(p.amount or 0.0) for p in payments)
    applied_total_before = 0.0

    applied_rows = 0
    for pid, _, _ in payments:
        rows, _remainder = auto_apply_payment_fifo(db, int(pid))
        applied_rows += len(rows)

    applied_total_after = db.execute(
        select(func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0))
        .where(PaymentApplication.member_id == m.id)
    ).scalar_one()
    applied_total_after = float(applied_total_after or 0.0)

    credit = total_inbound - applied_total_after

    return {
        "member": m.name,
        "member_id": m.id,
        "inbound_payments": len(payments),
        "inbound_total": round(total_inbound, 2),
        "applications_rows": applied_rows,
        "applied_total": round(applied_total_after, 2),
        "unapplied_credit": round(credit, 2),
    }


def reconcile_all_members_fifo(db: Session) -> list[dict]:
    members = db.execute(select(Member.id).order_by(Member.name)).scalars().all()
    results = []
    for mid in members:
        # Skip members with no inbound payments quickly
        cnt = db.execute(
            select(func.count())
            .select_from(Payment)
            .where(Payment.direction == "INBOUND", Payment.member_id == mid)
        ).scalar_one()
        if int(cnt) == 0:
            continue
        results.append(reconcile_member_fifo(db, mid))
    return results