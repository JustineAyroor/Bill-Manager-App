from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd
from datetime import date

# Members you track
OWNER_NAME = "Justine"
MEMBER_COLS_TX = ["Justine", "Vinay", "Zubin", "Rose", "Roshan", "Prachi", "Julian", "Others"]

STOP_DESC_PREFIXES = (
    "TOTAL AMOUNT OWED",
    "TOTAL PAID TO",
    "TOTAL PENDING",
    "TOTAL BILL RESOLVED",
)

def _to_float(x) -> float:
    if pd.isna(x):
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").strip()
    # remove currency symbols etc.
    s = "".join(ch for ch in s if ch.isdigit() or ch in ".-")
    try:
        return float(s)
    except Exception:
        return 0.0

def _is_number(x) -> bool:
    try:
        if pd.isna(x):
            return False
        float(x)
        return True
    except Exception:
        return False

def _find_allocations_header(raw: pd.DataFrame) -> tuple[int, int]:
    for r in range(min(len(raw), 250)):
        row = raw.iloc[r].astype(str).str.strip().str.lower().tolist()
        if "year" in row and "month" in row:
            year_col = row.index("year")
            return r, year_col
    raise RuntimeError("Could not find allocations header row with 'Year' and 'Month'.")

def extract_allocations_from_sheet1(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="Sheet1", header=None, dtype=object)
    header_row, year_col = _find_allocations_header(raw)
    month_col = year_col + 1

    headers = raw.iloc[header_row, year_col:].tolist()
    headers = [str(h).strip() for h in headers]
    member_headers = headers[2:]  # after Year, Month

    out_rows = []
    for r in range(header_row + 1, len(raw)):
        year_val = raw.iat[r, year_col]
        month_val = raw.iat[r, month_col]

        # Stop when year stops being numeric (this is where Due Total/Recovered/Final Due rows begin)
        if not _is_number(year_val):
            break

        y = int(float(year_val))
        m = str(month_val).strip()

        row_vals = raw.iloc[r, year_col + 2 : year_col + 2 + len(member_headers)].tolist()
        rec = {"Year": y, "Month": m}
        for h, v in zip(member_headers, row_vals):
            if pd.isna(h):
                continue
            name = str(h).strip()
            if not name or name.lower() == "nan":
                continue
            rec[name] = _to_float(v)
        out_rows.append(rec)

    df = pd.DataFrame(out_rows).fillna(0.0)
    return df


def _prev_month(d: date) -> date:
    # shift date back by one month (keep day=1 to avoid month-length issues)
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


def extract_tmobile_totals_by_year_month(path: str) -> dict[tuple[int, str], float]:
    """
    Uses TMOBILE OUTBOUND transactions to estimate the monthly bill total.
    IMPORTANT: payment in Feb usually corresponds to Jan invoice, so we map to previous month.
    Keyed by (year, month_abbrev) like (2026, 'Jan').
    """
    df = pd.read_excel(path, sheet_name="Sheet1", header=0, dtype=object)
    if "Date" not in df.columns or "Description" not in df.columns or "Amount" not in df.columns:
        raise RuntimeError("Expected Date/Description/Amount columns in Sheet1.")

    totals: dict[tuple[int, str], float] = {}

    for _, row in df.iterrows():
        desc = "" if pd.isna(row.get("Description")) else str(row.get("Description")).strip()
        if any(desc.startswith(p) for p in STOP_DESC_PREFIXES):
            break

        dt = row.get("Date")
        if pd.isna(dt):
            continue
        paid_on = pd.to_datetime(dt).to_pydatetime().date()

        amt = _to_float(row.get("Amount"))

        # Outbound T-Mobile payment rows: TMOBILE and negative amount
        if "TMOBILE" in desc.upper() and amt < 0:
            bill_period = _prev_month(paid_on)   # ✅ shift back one month
            y = bill_period.year
            m = bill_period.strftime("%b")       # Jan/Feb/Mar...
            key = (y, m)
            totals[key] = totals.get(key, 0.0) + abs(amt)

    return totals

def add_owner_allocation(alloc_df: pd.DataFrame, tmobile_totals: dict[tuple[int, str], float]) -> pd.DataFrame:
    """
    Adds:
      - InvoiceTotal column (estimated)
      - OWNER_NAME column computed as InvoiceTotal - sum(others)
    """
    df = alloc_df.copy()

    # Make sure owner column exists (even if 0)
    if OWNER_NAME not in df.columns:
        df[OWNER_NAME] = 0.0

    # Determine which columns are "member" columns
    member_cols = [c for c in df.columns if c not in ("Year", "Month", "InvoiceTotal")]

    invoice_totals = []
    owner_dues = []

    for _, row in df.iterrows():
        year = int(row["Year"])
        month = str(row["Month"]).strip()

        # Normalize month to 3-letter form if it's longer like "January"
        try:
            month_abbrev = pd.to_datetime(month[:3], format="%b").strftime("%b")
        except Exception:
            # fallback: take first 3 letters
            month_abbrev = month[:3].title()

        total = tmobile_totals.get((year, month_abbrev), 0.0)

        # Sum everyone except owner
        others = 0.0
        for c in member_cols:
            if c == OWNER_NAME:
                continue
            others += float(row.get(c, 0.0))

        owner = max(total - others, 0.0)

        invoice_totals.append(total)
        owner_dues.append(owner)

    df["InvoiceTotal"] = invoice_totals
    df[OWNER_NAME] = owner_dues

    # Reorder columns (nice output)
    cols = ["Year", "Month", "InvoiceTotal"] + [c for c in df.columns if c not in ("Year", "Month", "InvoiceTotal")]
    return df[cols]

def extract_transactions_normalized(path: str) -> pd.DataFrame:
    """
    Normalizes member payments and TMOBILE outbound:
      Date | Direction | Member | Amount | Description
    """
    df = pd.read_excel(path, sheet_name="Sheet1", header=0, dtype=object)
    records = []

    for _, row in df.iterrows():
        desc = "" if pd.isna(row.get("Description")) else str(row.get("Description")).strip()

        if any(desc.startswith(p) for p in STOP_DESC_PREFIXES):
            break

        dt = row.get("Date")
        if pd.isna(dt):
            continue
        when = pd.to_datetime(dt).date()

        amt = _to_float(row.get("Amount"))

        # Outbound TMOBILE payments
        if "TMOBILE" in desc.upper() and amt < 0:
            records.append({
                "Date": when.isoformat(),
                "Direction": "OUTBOUND",
                "Member": "",
                "Amount": abs(amt),
                "Description": desc,
            })

        # Inbound payments: negative values under member columns
        for mcol in MEMBER_COLS_TX:
            if mcol in df.columns:
                v = _to_float(row.get(mcol))
                if v < 0:
                    records.append({
                        "Date": when.isoformat(),
                        "Direction": "INBOUND",
                        "Member": mcol,
                        "Amount": abs(v),
                        "Description": desc,
                    })

    out = pd.DataFrame(records)
    return out

def main(input_xlsx: str, output_xlsx: str):
    alloc = extract_allocations_from_sheet1(input_xlsx)
    tmobile_totals = extract_tmobile_totals_by_year_month(input_xlsx)
    alloc = add_owner_allocation(alloc, tmobile_totals)

    tx_norm = extract_transactions_normalized(input_xlsx)

    out_path = Path(output_xlsx)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        alloc.to_excel(writer, sheet_name="allocations", index=False)
        tx_norm.to_excel(writer, sheet_name="transactions", index=False)

    print(f"✅ Wrote cleaned workbook: {out_path}")
    print(f"   allocations rows: {len(alloc)}")
    print(f"   transactions_normalized rows: {len(tx_norm)}")
    print("ℹ️ InvoiceTotal is estimated from TMOBILE outbound payment month (by payment date).")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: uv run python seed/cleanup_tmobile_excel.py <input.xlsx> <output.xlsx>")
        raise SystemExit(1)
    main(sys.argv[1], sys.argv[2])