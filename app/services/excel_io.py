from __future__ import annotations
from datetime import date
from datetime import datetime
from pathlib import Path
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import select, func, case

from app.db.models import Member, Invoice, Allocation, Payment, PaymentApplication
from app.services.accounting import member_balances

EXPORT_DIR = Path("data/exports")
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


MONTH_ORDER = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sept": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def _month_sort_key(m: str) -> int:
    return MONTH_ORDER.get(str(m).strip(), 99)


def _prev_month_year(y: int, m: int) -> tuple[int, int]:
    if m == 1:
        return (y - 1, 12)
    return (y, m - 1)

def export_excel(db: Session) -> str:
    """
    Creates an Excel workbook with:
      - Dues Summary (table)
      - Allocations (matrix: Year/Month rows, members as columns, includes InvoiceTotal)
      - Payments Pivot (matrix of INBOUND per member by Year/Month)
      - Payments Ledger (detailed transactions)
      - Invoices (invoice list with totals)
    """

    # ---------- Members list (for consistent column ordering) ----------
    member_names = db.execute(
        select(Member.name).where(Member.name.isnot(None)).order_by(Member.name)
    ).scalars().all()
    member_names = [m for m in member_names if str(m).strip() and str(m).strip().lower() != "nan"]

    # ---------- Summary ----------
    summary_df = pd.DataFrame(member_balances(db))

    # ---------- Invoices list ----------
    invoices_rows = db.execute(
        select(Invoice.id, Invoice.year, Invoice.month, Invoice.total_amount)
        .order_by(Invoice.year, Invoice.month)
    ).all()
    invoices_df = pd.DataFrame([{
        "invoice_id": r.id,
        "year": r.year,
        "month": r.month,
        "invoice_total": float(r.total_amount or 0.0),
    } for r in invoices_rows])

    if not invoices_df.empty:
        invoices_df["month_num"] = invoices_df["month"].apply(_month_sort_key)
        invoices_df = invoices_df.sort_values(["year", "month_num"]).drop(columns=["month_num"])

    # ---------- Allocations (matrix) ----------
    alloc_rows = db.execute(
        select(
            Invoice.year.label("year"),
            Invoice.month.label("month"),
            Invoice.total_amount.label("invoice_total"),
            Member.name.label("member"),
            Allocation.amount_due.label("amount_due"),
        )
        .select_from(Allocation)
        .join(Invoice, Invoice.id == Allocation.invoice_id)
        .join(Member, Member.id == Allocation.member_id)
    ).all()

    alloc_long = pd.DataFrame([{
        "year": r.year,
        "month": r.month,
        "invoice_total": float(r.invoice_total or 0.0),
        "member": r.member,
        "amount_due": float(r.amount_due or 0.0),
    } for r in alloc_rows])

    if alloc_long.empty:
        alloc_matrix = pd.DataFrame(columns=["year", "month", "invoice_total"] + member_names + ["row_total"])
    else:
        # Pivot to member columns
        alloc_matrix = alloc_long.pivot_table(
            index=["year", "month", "invoice_total"],
            columns="member",
            values="amount_due",
            aggfunc="sum",
            fill_value=0.0,
        ).reset_index()

        # Ensure all members appear as columns (even if zeros)
        for m in member_names:
            if m not in alloc_matrix.columns:
                alloc_matrix[m] = 0.0

        alloc_matrix["row_total"] = alloc_matrix[member_names].sum(axis=1)

        # Sort Year/Month properly
        alloc_matrix["month_num"] = alloc_matrix["month"].apply(_month_sort_key)
        alloc_matrix = alloc_matrix.sort_values(["year", "month_num"]).drop(columns=["month_num"])

        # Reorder columns
        alloc_matrix = alloc_matrix[["year", "month", "invoice_total"] + member_names + ["row_total"]]
# ---------- Applications Pivot (applied per member by invoice Year/Month) ----------
    app_rows = db.execute(
        select(
            Invoice.year.label("year"),
            Invoice.month.label("month"),
            Member.name.label("member"),
            func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0).label("applied"),
        )
        .select_from(PaymentApplication)
        .join(Invoice, Invoice.id == PaymentApplication.invoice_id)
        .join(Member, Member.id == PaymentApplication.member_id)
        .group_by(Invoice.year, Invoice.month, Member.name)
    ).all()

    app_long = pd.DataFrame([{
        "year": r.year,
        "month": r.month,
        "member": r.member,
        "applied": float(r.applied or 0.0),
    } for r in app_rows])

    if app_long.empty:
        applications_matrix = pd.DataFrame(columns=["year", "month"] + member_names + ["row_total"])
    else:
        applications_matrix = app_long.pivot_table(
            index=["year", "month"],
            columns="member",
            values="applied",
            aggfunc="sum",
            fill_value=0.0,
        ).reset_index()

        for m in member_names:
            if m not in applications_matrix.columns:
                applications_matrix[m] = 0.0

        applications_matrix["row_total"] = applications_matrix[member_names].sum(axis=1)
        applications_matrix["month_num"] = applications_matrix["month"].apply(_month_sort_key)
        applications_matrix = applications_matrix.sort_values(["year", "month_num"]).drop(columns=["month_num"])
        applications_matrix = applications_matrix[["year", "month"] + member_names + ["row_total"]]

    # ---------- Applications Ledger (raw splits) ----------
    app_ledger_rows = db.execute(
        select(
            PaymentApplication.id,
            PaymentApplication.amount_applied,
            PaymentApplication.created_at,
            Member.name.label("member"),
            Payment.id.label("payment_id"),
            Payment.date.label("payment_date"),
            Invoice.year.label("inv_year"),
            Invoice.month.label("inv_month"),
        )
        .select_from(PaymentApplication)
        .join(Member, Member.id == PaymentApplication.member_id)
        .join(Payment, Payment.id == PaymentApplication.payment_id)
        .join(Invoice, Invoice.id == PaymentApplication.invoice_id)
        .order_by(Payment.date.asc(), Payment.id.asc(), PaymentApplication.id.asc())
    ).all()

    applications_ledger = pd.DataFrame([{
        "app_id": int(r.id),
        "member": r.member or "",
        "payment_id": int(r.payment_id),
        "payment_date": r.payment_date.isoformat() if r.payment_date else "",
        "invoice": f"{r.inv_year}-{r.inv_month}",
        "amount_applied": float(r.amount_applied or 0.0),
        "created_at": r.created_at.isoformat() if r.created_at else "",
    } for r in app_ledger_rows])
    # ---------- Payments Pivot (INBOUND per member by Year/Month) ----------
    # We’ll bucket by payment date year/month. If later you want invoice-period mapping, we can link by invoice_id.
    pay_rows = db.execute(
        select(
            Payment.date.label("date"),
            Payment.amount.label("amount"),
            Payment.direction.label("direction"),
            Payment.description.label("description"),
            Member.name.label("member"),
        )
        .select_from(Payment)
        .outerjoin(Member, Member.id == Payment.member_id)
        .order_by(Payment.date.desc())
    ).all()

    pay_ledger = pd.DataFrame([{
        "date": r.date.isoformat(),
        "year": r.date.year,
        "month": r.date.strftime("%b"),
        "direction": r.direction,
        "member": r.member or "",
        "amount": float(r.amount or 0.0),
        "description": r.description or "",
    } for r in pay_rows])

    # Pivot only inbound member payments
    pay_inbound = pay_ledger[(pay_ledger["direction"] == "INBOUND") & (pay_ledger["member"].str.strip() != "")]
    if pay_inbound.empty:
        payments_matrix = pd.DataFrame(columns=["year", "month"] + member_names + ["row_total"])
    else:
        payments_matrix = pay_inbound.pivot_table(
            index=["year", "month"],
            columns="member",
            values="amount",
            aggfunc="sum",
            fill_value=0.0,
        ).reset_index()

        for m in member_names:
            if m not in payments_matrix.columns:
                payments_matrix[m] = 0.0

        payments_matrix["row_total"] = payments_matrix[member_names].sum(axis=1)

        payments_matrix["month_num"] = payments_matrix["month"].apply(_month_sort_key)
        payments_matrix = payments_matrix.sort_values(["year", "month_num"]).drop(columns=["month_num"])
        payments_matrix = payments_matrix[["year", "month"] + member_names + ["row_total"]]

    # Outbound by Month (shift payment month back by 1 to represent bill period)
    outbound = pay_ledger[pay_ledger["direction"] == "OUTBOUND"].copy()
    if outbound.empty:
        outbound_monthly = pd.DataFrame(columns=["year", "month", "amount"])
    else:
        # Use the real payment date to compute shifted (bill) period
        dt = pd.to_datetime(outbound["date"])
        shifted = dt.apply(lambda s: _prev_month_year(s.year, s.month))
        outbound["bill_year"] = [t[0] for t in shifted]
        outbound["bill_month_num"] = [t[1] for t in shifted]
        outbound["bill_month"] = outbound["bill_month_num"].apply(lambda n: date(2000, n, 1).strftime("%b"))

        outbound_monthly = (
            outbound.groupby(["bill_year", "bill_month_num", "bill_month"], as_index=False)["amount"]
            .sum()
            .rename(columns={"bill_year": "year", "bill_month": "month"})
            .sort_values(["year", "bill_month_num"])
            .drop(columns=["bill_month_num"])
        )
    # ---------- Write workbook ----------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = EXPORT_DIR / f"tmobile_export_{ts}.xlsx"

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Dues Summary", index=False)
        invoices_df.to_excel(writer, sheet_name="Invoices", index=False)

        alloc_matrix.to_excel(writer, sheet_name="Allocations", index=False)
        payments_matrix.to_excel(writer, sheet_name="Payments Pivot", index=False)
        applications_matrix.to_excel(writer, sheet_name="Applications Pivot", index=False)
        applications_ledger.to_excel(writer, sheet_name="Applications Ledger", index=False)
        # Optional extra sheet for outbound
        if outbound_monthly is not None and not outbound_monthly.empty:
            outbound_monthly.to_excel(writer, sheet_name="Outbound by Month", index=False)

        pay_ledger.to_excel(writer, sheet_name="Payments Ledger", index=False)

        # Nice column sizing (basic)
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

    return str(out_path)