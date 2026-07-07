from sqlalchemy.orm import Session
from sqlalchemy import select, func, case
from app.db.models import Member, Allocation, PaymentApplication, Payment, Invoice, Plan, PlanMember

OWNER_NAME = "Justine"


def _plan_owner_member_id(db, plan_id: int | None) -> int | None:
    if not plan_id:
        return None
    plan = db.get(Plan, plan_id)
    return plan.owner_member_id if plan else None


def plan_totals(db, plan_id: int | None = None, owner_name: str = "Justine"):
    """
    Aggregate dues/recovered/deficit for one plan (plan_id given) or across
    all plans (plan_id=None - preserves the original single-plan behavior).
    """
    rows = member_balances(db, plan_id=plan_id)

    owner_id = _plan_owner_member_id(db, plan_id)
    if owner_id is not None:
        non_owner = [r for r in rows if r["member_id"] != owner_id]
    else:
        non_owner = [r for r in rows if (r["member"] or "").strip().lower() != owner_name.strip().lower()]

    total_due = sum(max(r["balance"], 0.0) for r in non_owner)     # sum of outstanding only
    recovered = sum(r["total_paid"] for r in non_owner)            # total applied from members
    deficit = total_due  # by your definition, due already is what's remaining
    # If you instead mean "total originally due" vs recovered:
    # original_due = sum(r["total_due"] for r in non_owner)
    # deficit = original_due - recovered

    # Total bill paid by owner = sum OUTBOUND payments, scoped by
    # Payment.plan_id directly (every payment now belongs to exactly one plan).
    outbound_stmt = select(func.coalesce(func.sum(Payment.amount), 0.0)).where(Payment.direction == "OUTBOUND")
    if plan_id is not None:
        outbound_stmt = outbound_stmt.where(Payment.plan_id == plan_id)
    total_paid_owner = db.execute(outbound_stmt).scalar_one()
    total_paid_owner = abs(float(total_paid_owner or 0.0))  # outbound stored positive in your UI; if negative, abs helps

    return {
        "plan_due_outstanding": round(total_due, 2),
        "plan_recovered": round(recovered, 2),
        "plan_deficit": round(deficit, 2),
        "owner_total_outbound": round(total_paid_owner, 2),
    }


def all_plans_totals(db) -> list[dict]:
    """Per-plan breakdown for cross-plan spend analytics, plus a combined row."""
    out = []
    for plan in db.execute(select(Plan).order_by(Plan.name)).scalars().all():
        totals = plan_totals(db, plan_id=plan.id)
        totals["plan_id"] = plan.id
        totals["plan_name"] = plan.name
        out.append(totals)

    combined = plan_totals(db, plan_id=None)
    combined["plan_id"] = None
    combined["plan_name"] = "All plans (combined)"
    out.append(combined)
    return out

# def plan_totals(db: Session) -> dict:
#     # Total Due = allocations excluding owner
#     total_due = db.execute(
#         select(func.coalesce(func.sum(Allocation.amount_due), 0.0))
#         .select_from(Allocation)
#         .join(Member, Member.id == Allocation.member_id)
#         .where(Member.name != OWNER_NAME)
#     ).scalar_one()

#     # Recovered = inbound excluding owner
#     recovered = db.execute(
#         select(
#             func.coalesce(
#                 func.sum(case((Payment.direction == "INBOUND", Payment.amount), else_=0.0)),
#                 0.0,
#             )
#         )
#         .select_from(Payment)
#         .join(Member, Member.id == Payment.member_id)
#         .where(Member.name != OWNER_NAME)
#     ).scalar_one()

#     # Total bill paid = outbound to carrier (member_id is often NULL; that's okay)
#     total_bill_paid = db.execute(
#         select(
#             func.coalesce(
#                 func.sum(case((Payment.direction == "OUTBOUND", Payment.amount), else_=0.0)),
#                 0.0,
#             )
#         )
#     ).scalar_one()

#     remaining = float(total_due) - float(recovered)

#     return {
#         "total_due": float(total_due),
#         "recovered": float(recovered),
#         "remaining": float(remaining),
#         "total_bill_paid": float(total_bill_paid),
#     }

def member_balances(db, plan_id: int | None = None):
    """
    Per-member due/paid/balance. When plan_id is given, scopes to that plan's
    roster and only counts allocations/applications tied to that plan's
    invoices. When plan_id is None, preserves the original all-members/
    all-invoices behavior (single implicit plan).
    """
    # due per member
    due_sq = select(
        Allocation.member_id.label("member_id"),
        func.coalesce(func.sum(Allocation.amount_due), 0.0).label("total_due"),
    ).select_from(Allocation)
    if plan_id is not None:
        due_sq = due_sq.join(Invoice, Invoice.id == Allocation.invoice_id).where(Invoice.plan_id == plan_id)
    due_sq = due_sq.group_by(Allocation.member_id).subquery()

    # applied per member
    applied_sq = select(
        PaymentApplication.member_id.label("member_id"),
        func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0).label("total_applied"),
    ).select_from(PaymentApplication)
    if plan_id is not None:
        applied_sq = applied_sq.join(Invoice, Invoice.id == PaymentApplication.invoice_id).where(
            Invoice.plan_id == plan_id
        )
    applied_sq = applied_sq.group_by(PaymentApplication.member_id).subquery()

    members_stmt = (
        select(
            Member.id,
            Member.name,
            func.coalesce(due_sq.c.total_due, 0.0).label("total_due"),
            func.coalesce(applied_sq.c.total_applied, 0.0).label("total_applied"),
        )
        .select_from(Member)
        .outerjoin(due_sq, due_sq.c.member_id == Member.id)
        .outerjoin(applied_sq, applied_sq.c.member_id == Member.id)
    )
    if plan_id is not None:
        members_stmt = members_stmt.join(PlanMember, PlanMember.member_id == Member.id).where(
            PlanMember.plan_id == plan_id
        )
    members_stmt = members_stmt.order_by(Member.name)

    rows = db.execute(members_stmt).all()

    owner_id = _plan_owner_member_id(db, plan_id)

    out = []
    for r in rows:
        due = float(r.total_due or 0.0)
        paid = float(r.total_applied or 0.0)

        # Owner gets outbound credit
        is_owner = (r.id == owner_id) if owner_id is not None else (r.name == OWNER_NAME)
        if is_owner:
            paid += float(r.total_due or 0.0)

        bal = due - paid  # outstanding (negative means credit)
        out.append({
            "member_id": int(r.id),
            "member": r.name or "",
            "total_due": round(due, 2),
            "total_paid": round(paid, 2),
            "balance": round(bal, 2),
        })
    return out

# def member_balances(db: Session) -> list[dict]:

#     due_subq = (
#         select(
#             Allocation.member_id.label("member_id"),
#             func.coalesce(func.sum(Allocation.amount_due), 0.0).label("total_due"),
#         )
#         .group_by(Allocation.member_id)
#         .subquery()
#     )

#     inbound_subq = (
#         select(
#             Payment.member_id.label("member_id"),
#             func.coalesce(
#                 func.sum(
#                     case((Payment.direction == "INBOUND", Payment.amount), else_=0.0)
#                 ),
#                 0.0,
#             ).label("total_inbound"),
#         )
#         .where(Payment.member_id.isnot(None))
#         .group_by(Payment.member_id)
#         .subquery()
#     )

#     outbound_total = db.execute(
#         select(
#             func.coalesce(
#                 func.sum(
#                     case((Payment.direction == "OUTBOUND", Payment.amount), else_=0.0)
#                 ),
#                 0.0,
#             )
#         )
#     ).scalar_one()

#     rows = db.execute(
#         select(
#             Member.id,
#             Member.name,
#             func.coalesce(due_subq.c.total_due, 0.0),
#             func.coalesce(inbound_subq.c.total_inbound, 0.0),
#         )
#         .outerjoin(due_subq, due_subq.c.member_id == Member.id)
#         .outerjoin(inbound_subq, inbound_subq.c.member_id == Member.id)
#         .order_by(Member.name)
#     ).all()

#     results = []

#     for member_id, name, total_due, total_inbound in rows:

#         total_paid = float(total_inbound or 0.0)

#         # Owner gets outbound credit
#         if name == OWNER_NAME:
#             total_paid += float(total_due or 0.0)

#         balance = float(total_due) - total_paid

#         results.append(
#             {
#                 "member_id": member_id,
#                 "member": name,
#                 "total_due": float(total_due),
#                 "total_paid": total_paid,
#                 "balance": balance,
#             }
#         )

#     return results