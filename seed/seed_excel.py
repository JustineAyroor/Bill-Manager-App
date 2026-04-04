from __future__ import annotations

import sys
import pandas as pd
from datetime import datetime, date

from app.db.database import SessionLocal
from app.services import crud


MONTH_MAP = {
    "jan": "Jan", "january": "Jan",
    "feb": "Feb", "february": "Feb",
    "mar": "Mar", "march": "Mar",
    "apr": "Apr", "april": "Apr",
    "may": "May",
    "jun": "Jun", "june": "Jun",
    "jul": "Jul", "july": "Jul",
    "aug": "Aug", "august": "Aug",
    "sept": "Sept", "sep": "Sept", "september": "Sept",
    "oct": "Oct", "october": "Oct",
    "nov": "Nov", "november": "Nov",
    "dec": "Dec", "december": "Dec",
}

def norm_month(m):
    if pd.isna(m):
        return None
    s = str(m).strip()
    key = s.lower()
    return MONTH_MAP.get(key, s)


def parse_date(x) -> date:
    if isinstance(x, date):
        return x
    if pd.isna(x):
        raise ValueError("date missing")
    s = str(x).strip()
    # allow mm/dd/yy like your screenshot
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    raise ValueError(f"Unrecognized date format: {x}")


def import_allocations(db, df: pd.DataFrame):
    """
    Expected columns:
      Year | Month | <member1> | <member2> | ...
    """
    cols = [c for c in df.columns]
    if "Year" not in cols or "Month" not in cols or "InvoiceTotal" not in cols:
        raise ValueError("allocations sheet must include columns: Year, Month, InvoiceTotal")

    member_cols = [c for c in cols if c not in ("Year", "Month", "InvoiceTotal")]
    # Create members first
    members = {}
    for name in member_cols:
        m = crud.get_or_create_member(db, name=str(name).strip())
        members[name] = m

    # Rows -> invoices + allocations
    for _, row in df.iterrows():
        year_val = row["Year"]
        if pd.isna(year_val):
            continue
        year = int(year_val)
        month = norm_month(row["Month"])
        if not month:
            continue
        invoice_total = float(row.get("InvoiceTotal", 0) or 0)
        inv = crud.upsert_invoice(db, year=year, month=month, total_amount=invoice_total)
        for mcol in member_cols:
            val = row.get(mcol, 0)
            if pd.isna(val):
                val = 0
            amount_due = float(val)
            if amount_due == 0:
                continue
            crud.upsert_allocation(db, invoice_id=inv.id, member_id=members[mcol].id, amount_due=amount_due)


def import_transactions(db, df: pd.DataFrame):
    """
    Flexible ledger import.

    Expected minimal columns:
      Date | Description | Amount
    Optional: Direction, Member

    If Direction missing:
      - Amount < 0 => OUTBOUND
      - Amount > 0 => INBOUND (requires Member, otherwise stored as OUTBOUND/unknown)

    If your sheet has per-member columns (Justine/Vinay/Zubin etc):
      - We treat non-zero entries in those member columns as payments for that member.
    """
    cols = list(df.columns)
    if "Date" not in cols or "Amount" not in cols:
        raise ValueError("transactions sheet must include columns: Date, Amount (and ideally Description)")

    # detect per-member columns: anything not in base known columns
    base_cols = {"Date", "Currency", "Description", "Amount", "Direction", "Member"}
    member_cols = [c for c in cols if c not in base_cols]

    # Create those members
    for mc in member_cols:
        crud.get_or_create_member(db, str(mc).strip())

    for _, row in df.iterrows():
        when = parse_date(row["Date"])
        desc = None if pd.isna(row.get("Description")) else str(row.get("Description"))

        # If there are per-member columns, insert one payment per member column where value != 0
        any_member_payment = False
        for mc in member_cols:
            v = row.get(mc, 0)
            if pd.isna(v) or float(v) == 0.0:
                continue
            any_member_payment = True
            m = crud.get_or_create_member(db, str(mc).strip())
            amt = float(v)

            # In your screenshot, inbound payments appear positive in member column; negatives can happen
            direction = "INBOUND" if amt > 0 else "OUTBOUND"
            crud.add_payment(db, when=when, amount=abs(amt), direction=direction, description=desc, member_id=m.id)

        if any_member_payment:
            # also store overall transaction amount (optional) — skip to avoid duplicates
            continue

        # Otherwise use Amount column
        amt_raw = float(row["Amount"])
        direction = row.get("Direction")
        if pd.isna(direction):
            direction = "OUTBOUND" if amt_raw < 0 else "INBOUND"
        direction = str(direction).strip().upper()

        member_name = row.get("Member")
        member_id = None
        if not pd.isna(member_name) and str(member_name).strip():
            m = crud.get_or_create_member(db, str(member_name).strip())
            member_id = m.id

        # If INBOUND but no member_id, keep it as OUTBOUND/unknown to avoid corrupting balances
        if direction == "INBOUND" and member_id is None:
            direction = "OUTBOUND"

        crud.add_payment(db, when=when, amount=abs(amt_raw), direction=direction, description=desc, member_id=member_id)


def main(xlsx_path: str):
    xls = pd.ExcelFile(xlsx_path)
    sheets = [s.lower() for s in xls.sheet_names]

    with SessionLocal() as db:
        if "allocations" in sheets:
            df_alloc = pd.read_excel(xlsx_path, sheet_name=xls.sheet_names[sheets.index("allocations")])
            import_allocations(db, df_alloc)
            print("✅ Imported allocations")

        if "transactions" in sheets:
            df_tx = pd.read_excel(xlsx_path, sheet_name=xls.sheet_names[sheets.index("transactions")])
            import_transactions(db, df_tx)
            print("✅ Imported transactions")

        db.commit()
        print("✅ Seed import complete.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python seed/seed_excel.py path/to/your.xlsx")
        sys.exit(1)
    main(sys.argv[1])