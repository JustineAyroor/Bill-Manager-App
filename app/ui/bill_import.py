from __future__ import annotations

import json
import re
import traceback
from datetime import date
from typing import Any

import pandas as pd
import gradio as gr
from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Member, Invoice, Allocation, BillImportJob
from app.services.pdf_extract import extract_pdf_text
from app.services.llm_invoice_extract import extract_bill_proposal, MONTHS
from app.services.bill_text_filter import filter_text_for_llm
from app.services import plans as plans_service
from app.services import authz
from app.services import bill_import_worker
from app.services import member_identifiers
from app.services import vectorstore
from app.services import allocation_view
from app.core.config import VECTOR_RETRIEVAL_LOOKBACK_MONTHS


LAST4_RE = re.compile(r"last4:(\d{4})", re.IGNORECASE)


def _member_choice_list(plan_id: int | None = None):
    with SessionLocal() as db:
        if plan_id:
            rows = [(m.id, m.name) for m in plans_service.get_plan_members(db, plan_id)]
        else:
            rows = db.execute(select(Member.id, Member.name).order_by(Member.name)).all()
    out = []
    for mid, name in rows:
        if name and str(name).strip() and str(name).strip().lower() != "nan":
            out.append(f"{mid} | {name}")
    return out


def _parse_id(choice: str | None):
    if not choice:
        return None
    try:
        return int(str(choice).split("|", 1)[0].strip())
    except Exception:
        return None


def _member_names_map(plan_id: int | None = None) -> dict:
    with SessionLocal() as db:
        if plan_id:
            rows = [(m.id, m.name) for m in plans_service.get_plan_members(db, plan_id)]
        else:
            rows = db.execute(select(Member.id, Member.name)).all()
    return {int(mid): name for mid, name in rows}


def _last4_from_phone_key(phone_key: str) -> str | None:
    m = LAST4_RE.search(str(phone_key or ""))
    return m.group(1) if m else None


def _mapping_table(cur_map: dict) -> pd.DataFrame:
    rows = [{"phone_key": k, "member_id": v} for k, v in sorted((cur_map or {}).items())]
    return pd.DataFrame(rows)


def _auto_map_from_db(phone_choices: list[str]) -> dict:
    """
    Auto-map phone_key -> member_id using Member.phone_last4, if present.
    If your Member model doesn't have phone_last4 yet, this just returns {}.
    """
    if not phone_choices:
        return {}

    # If model doesn't have phone_last4, skip safely
    if not hasattr(Member, "phone_last4"):
        return {}

    need_last4 = []
    for pk in phone_choices:
        last4 = _last4_from_phone_key(pk)
        if last4:
            need_last4.append(last4)
    if not need_last4:
        return {}

    with SessionLocal() as db:
        rows = db.execute(
            select(Member.id, Member.phone_last4).where(Member.phone_last4.isnot(None))
        ).all()

    last4_to_member = {str(l4).strip(): int(mid) for (mid, l4) in rows if l4}
    mapping = {}
    for pk in phone_choices:
        last4 = _last4_from_phone_key(pk)
        if last4 and last4 in last4_to_member:
            mapping[pk] = last4_to_member[last4]
    return mapping


def _calc_sum_diff(total, df):
    try:
        tot = float(total or 0.0)
    except Exception:
        tot = 0.0

    if df is None:
        return "0.00", f"{tot:.2f}"

    d = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df
    if d.empty or "suggested_amount" not in d.columns:
        return "0.00", f"{tot:.2f}"

    s = pd.to_numeric(d["suggested_amount"], errors="coerce").fillna(0.0).sum()
    return f"{s:.2f}", f"{(tot - s):.2f}"


def _validate_before_upsert(y, m, tot, df, do_owner: bool, owner_choice: str | None):
    issues = []
    try:
        y = int(y)
        tot = float(tot or 0.0)
        m = str(m)
    except Exception:
        return False, "❌ Invalid year/total."

    if m not in MONTHS:
        issues.append("Invalid month.")
    if tot <= 0:
        issues.append("Total must be > 0.")

    d = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df
    if d is None or d.empty:
        issues.append("No proposal table.")
        return False, "❌ " + "; ".join(issues)

    required = {"phone_key", "suggested_amount"}
    if not required.issubset(set(d.columns)):
        issues.append(f"Proposal missing columns: {required - set(d.columns)}")

    d["suggested_amount"] = pd.to_numeric(d["suggested_amount"], errors="coerce")
    if d["suggested_amount"].isna().any():
        issues.append("Some suggested_amount values are not numbers.")
    if (d["suggested_amount"].fillna(0) < 0).any():
        issues.append("Negative suggested_amount not allowed.")

    s = float(d["suggested_amount"].fillna(0).sum())
    diff = tot - s

    owner_id = _parse_id(owner_choice) if owner_choice else None
    if do_owner and not owner_id:
        issues.append("Owner allocation enabled but owner is not selected.")

    if not do_owner and abs(diff) > 2.0:
        issues.append(f"Allocations sum (${s:.2f}) differs from total (${tot:.2f}) by ${diff:.2f}")

    ok = len(issues) == 0
    msg = ("✅ Validation passed." if ok else "❌ Validation failed:\n- " + "\n- ".join(issues))
    msg += f"\n\nSuggested Sum: ${s:.2f} | Diff (total - suggested): ${diff:.2f}"
    return ok, msg


def _v2_save_identifier_to_db(identifier_key, member_choice, plan_id):
    """
    Powers the single shared "Unresolved charges" section (used by both
    NORMAL and evaluate-only modes) - just saves to member_identifiers.
    Nothing else needs to be threaded through: the very next poll re-runs
    matching from scratch (build_member_allocation_view always re-applies
    member_identifiers.apply_deterministic_matches), so a freshly-saved
    mapping is picked up immediately without any client-side state.
    """
    if not identifier_key:
        return "❌ Pick an identifier first."
    mid = _parse_id(member_choice)
    if not mid:
        return "❌ Pick a member first."
    try:
        itype, ivalue = str(identifier_key).split(":", 1)
    except ValueError:
        return "❌ Malformed identifier."

    with SessionLocal() as db:
        try:
            member_identifiers.save_identifier(db, plan_id, int(mid), itype, ivalue)
            db.commit()
        except Exception as e:
            return f"❌ {e}"

    return f"✅ Linked {identifier_key} → member. Updating amounts above…"


_BASIS_LABEL = {
    allocation_view.FROM_BILL: "from this bill",
    allocation_view.FROM_HISTORY: "from history",
    allocation_view.OWNER_ABSORBS: "owner absorbs remainder",
    allocation_view.NO_DATA: "no data - defaulted to $0",
}

_SOURCE_BADGE = {
    "deterministic": "🔒 saved mapping",
    "llm_suggestion": "🤖 AI guess",
}


def _v2_facts_rows(lines: list[dict], names: dict[int, str]) -> pd.DataFrame:
    """
    Section A - "what we found on this bill": a plain, read-only reference
    of every real (non-"none") line the LLM extracted, independent of the
    per-person amounts below. Never edited directly - if a line's member is
    wrong, either it was a deterministic match (fix the saved identifier
    under Members) or it's now unresolved after the dedupe guardrail
    cleared a bad guess (fix it in "Unresolved charges" below).
    """
    rows = []
    for ln in lines or []:
        ident = ln.get("identifier") or {"type": "none", "value": ""}
        if ident.get("type") == "none":
            continue
        mid = ln.get("matched_member_id")
        rows.append(
            {
                "Description": ln.get("display") or f"{ident.get('type')}:{ident.get('value')}",
                "Amount": round(float(ln.get("line_total") or 0.0), 2),
                "Member": names.get(int(mid), f"Member {mid}") if mid else "— not linked —",
                "Source": _SOURCE_BADGE.get(ln.get("match_source"), "❓ unresolved"),
                "Confidence": round(float(ln.get("confidence") or 0.0), 2),
            }
        )
    return pd.DataFrame(rows)


def _v2_alloc_rows(view: "allocation_view.AllocationView", total_amount=None) -> pd.DataFrame:
    """
    Section B - one editable row per plan member, this IS what gets
    approved. Carries both "amount" ($) and "percent" (% of the bill total)
    - editing either recalculates the other live (see _v2_recalc_alloc),
    so switching between "think in dollars" and "think in percent" never
    requires re-typing everything from scratch.
    """
    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0
    rows = []
    for m in sorted(view.members, key=lambda x: x.name or ""):
        label = _BASIS_LABEL.get(m.basis, m.basis)
        amount = round(m.amount, 2)
        rows.append(
            {
                "member_id": m.member_id,
                "member": m.name,
                "amount": amount,
                "percent": round(amount / total_amount * 100, 2) if total_amount > 0.005 else 0.0,
                "basis": label,
                "note": m.detail or label,
            }
        )
    return pd.DataFrame(rows)


def _v2_reconcile_text(total_amount, view: "allocation_view.AllocationView") -> str:
    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0
    assigned = round(sum(m.amount for m in view.members), 2)
    remaining = round(total_amount - assigned, 2)
    base = f"**Bill total:** ${total_amount:.2f} &nbsp;|&nbsp; **Assigned:** ${assigned:.2f}"
    if abs(remaining) < 0.01:
        return f"{base} &nbsp;|&nbsp; ✅ **fully reconciled**"
    if view.unattributed_total > 0.005:
        return (
            f"{base} &nbsp;|&nbsp; ⚠️ **Remaining: ${remaining:.2f}** - this plan has no owner set to absorb "
            "shared/unmatched charges, so nobody's row includes it yet; edit an amount above before approving."
        )
    return f"{base} &nbsp;|&nbsp; ⚠️ **Remaining: ${remaining:.2f}** - edit an amount above before approving."


def _v2_live_reconcile(alloc_df, total_amount) -> str:
    """
    Recomputed live, straight from whatever's currently in Section B's table
    (not the last poll's snapshot) - the "calculator" check requested so a
    mismatch is visible the instant you edit an amount, not just at the
    moment a job first finished processing.
    """
    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0
    d = pd.DataFrame(alloc_df) if not isinstance(alloc_df, pd.DataFrame) else alloc_df
    if d is None or d.empty or "amount" not in d.columns:
        return ""
    assigned = round(float(pd.to_numeric(d["amount"], errors="coerce").fillna(0.0).sum()), 2)
    remaining = round(total_amount - assigned, 2)
    base = f"**Bill total:** ${total_amount:.2f} &nbsp;|&nbsp; **Assigned:** ${assigned:.2f}"
    if abs(remaining) < 0.01:
        return f"{base} &nbsp;|&nbsp; ✅ **fully reconciled**"
    return f"{base} &nbsp;|&nbsp; ⚠️ **Remaining: ${remaining:.2f}** - edit an amount above before approving."


def _v2_recalc_alloc(new_df, prev_df, total_amount):
    """
    Keeps "amount" ($) and "percent" (% of the bill total) in sync live as
    Section B is edited - editing either one for a row recalculates the
    other for that same row, so typing "60" into Percent always means
    "60% of the bill total, in dollars" without a separate apply/save step.

    Diffs the freshly-edited table against the last-rendered snapshot
    (v2_alloc_prev_state) to tell, per row, which of the two columns the
    user actually touched (Gradio's .input() event only hands you the new
    full table, not which cell changed). On the very first edit after a
    fresh load (no prior snapshot to diff against, e.g. prev_df is None or
    doesn't cover this row), percent is just recomputed from amount so the
    two columns start out consistent.
    """
    d = pd.DataFrame(new_df) if not isinstance(new_df, pd.DataFrame) else new_df.copy()
    if d is None or d.empty or "member_id" not in d.columns:
        return d, new_df, _v2_live_reconcile(d, total_amount)

    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0

    if "amount" not in d.columns:
        d["amount"] = 0.0
    if "percent" not in d.columns:
        d["percent"] = 0.0
    d["amount"] = pd.to_numeric(d["amount"], errors="coerce").fillna(0.0)
    d["percent"] = pd.to_numeric(d["percent"], errors="coerce").fillna(0.0)

    prev = pd.DataFrame(prev_df) if isinstance(prev_df, list) else prev_df
    prev_by_id: dict[int, Any] = {}
    if isinstance(prev, pd.DataFrame) and not prev.empty and "member_id" in prev.columns:
        for _, r in prev.iterrows():
            try:
                prev_by_id[int(r["member_id"])] = r
            except Exception:
                continue

    for idx, row in d.iterrows():
        try:
            mid = int(row["member_id"])
        except Exception:
            continue
        amount = float(row["amount"])
        percent = float(row["percent"])
        prev_row = prev_by_id.get(mid)
        if prev_row is None:
            d.at[idx, "percent"] = round(amount / total_amount * 100, 2) if total_amount > 0.005 else 0.0
            continue
        amount_changed = abs(amount - float(prev_row.get("amount", amount))) > 0.005
        percent_changed = abs(percent - float(prev_row.get("percent", percent))) > 0.005
        if percent_changed and not amount_changed:
            d.at[idx, "amount"] = round(percent / 100 * total_amount, 2) if total_amount > 0.005 else 0.0
        elif amount_changed or not percent_changed:
            # amount wins if both moved at once (shouldn't happen from a
            # single cell edit); also the harmless "nothing actually
            # changed for this row" case, kept consistent regardless.
            d.at[idx, "percent"] = round(amount / total_amount * 100, 2) if total_amount > 0.005 else 0.0

    return d, d.copy(), _v2_live_reconcile(d, total_amount)


def _v2_refresh_percent_on_total_change(alloc_df, total_amount):
    """
    Editing the invoice Total shifts what every row's existing $ amount
    represents as a percentage, even though nobody touched Section B
    itself - recomputes the Percent column (amounts stay exactly as typed)
    so it never silently goes stale/wrong relative to a new total.
    """
    d = pd.DataFrame(alloc_df) if not isinstance(alloc_df, pd.DataFrame) else alloc_df.copy()
    if d is None or d.empty or "amount" not in d.columns:
        return d, d, _v2_live_reconcile(d, total_amount)
    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0
    d["percent"] = pd.to_numeric(d["amount"], errors="coerce").fillna(0.0).apply(
        lambda a: round(a / total_amount * 100, 2) if total_amount > 0.005 else 0.0
    )
    return d, d.copy(), _v2_live_reconcile(d, total_amount)


def _v2_equal_split(alloc_df, total_amount, plan_id):
    """
    "⚖️ Equal split" button - resets every row currently in Section B to an
    even share of the bill total (member/basis/note columns untouched).
    Any few-cent rounding remainder that an even division can't place
    exactly goes to the plan's owner - falling back to the first row if
    the plan has no owner (or the owner isn't one of the rows shown) - the
    same "owner absorbs the leftover" rule used everywhere else in this
    app (see build_member_allocation_view), so an equal split always sums
    to the total exactly, never a penny off.
    """
    d = pd.DataFrame(alloc_df) if not isinstance(alloc_df, pd.DataFrame) else alloc_df.copy()
    if d is None or d.empty or "member_id" not in d.columns:
        return d, d, _v2_live_reconcile(d, total_amount)

    try:
        total_amount = float(total_amount or 0.0)
    except Exception:
        total_amount = 0.0

    d = d.copy()
    n = len(d)
    share = round(total_amount / n, 2) if n else 0.0
    d["amount"] = share
    leftover = round(total_amount - round(share * n, 2), 2)

    owner_id = None
    if plan_id:
        with SessionLocal() as db:
            plan = plans_service.get_plan(db, plan_id)
            owner_id = int(plan.owner_member_id) if plan and plan.owner_member_id else None

    target_idx = None
    if owner_id is not None:
        matches = d.index[d["member_id"].astype(int) == owner_id]
        if len(matches):
            target_idx = matches[0]
    if target_idx is None:
        target_idx = d.index[0]

    if abs(leftover) > 0.005:
        d.at[target_idx, "amount"] = round(d.at[target_idx, "amount"] + leftover, 2)

    d["percent"] = d["amount"].apply(lambda a: round(a / total_amount * 100, 2) if total_amount > 0.005 else 0.0)
    return d, d.copy(), _v2_live_reconcile(d, total_amount)


def _v2_unresolved_rows(lines: list[dict]) -> pd.DataFrame:
    """
    Section C content - real (non-"none") lines nobody could be matched to,
    after dedup. Deliberately excludes "none"-type shared charges (those
    were never identifier-bearing in the first place, so there's nothing to
    "link") and anything already matched.
    """
    rows = []
    for ln in lines or []:
        ident = ln.get("identifier") or {"type": "none", "value": ""}
        if ident.get("type") == "none" or ln.get("matched_member_id"):
            continue
        rows.append(
            {
                "Description": ln.get("display") or f"{ident.get('type')}:{ident.get('value')}",
                "Amount": round(float(ln.get("line_total") or 0.0), 2),
            }
        )
    return pd.DataFrame(rows)


def _v2_unresolved_identifier_choices(lines: list[dict]) -> list[str]:
    choices: list[str] = []
    for ln in lines or []:
        ident = ln.get("identifier") or {"type": "none", "value": ""}
        if ident.get("type") == "none" or ln.get("matched_member_id"):
            continue
        key = f"{ident['type']}:{ident['value']}"
        if key not in choices:
            choices.append(key)
    return choices


def _v2_empty_poll_outputs(status_msg: str, timer_active: bool = False):
    return (
        status_msg,
        gr.update(active=timer_active),
        None,
        gr.update(visible=False), "", pd.DataFrame(), pd.DataFrame(),
        gr.update(visible=False), None, None, None, "", pd.DataFrame(), "", pd.DataFrame(),
        gr.update(visible=False), pd.DataFrame(), gr.update(choices=[], value=None),
        None,
    )


def _v2_enqueue(pdf_file, member_id, plan_id):
    # Always clears every diff/review display component immediately, not
    # just status/job_id/timer - otherwise a PREVIOUS job's diff/review data
    # stays on screen (looking "stuck"/stale) until the first poll tick
    # fires a few seconds later and finally hides it, which reads exactly
    # like "it showed then disappeared" for whatever job you just uploaded.
    def _reset(status_msg: str, timer_active: bool, job_id):
        empty = _v2_empty_poll_outputs(status_msg, timer_active=timer_active)
        return (empty[0], job_id) + empty[1:]

    if not pdf_file:
        return _reset("❌ Upload a PDF first.", False, None)
    if not plan_id:
        return _reset("❌ Pick a specific Active plan (not \"All Plans\") first.", False, None)

    res = bill_import_worker.enqueue_job(int(plan_id), member_id, pdf_file.name)
    if res["status"] in ("error", "rate_limited"):
        icon = "⚠️" if res["status"] == "rate_limited" else "❌"
        return _reset(f"{icon} {res['message']}", False, None)

    return _reset(f"✅ {res['message']} (job #{res['job_id']})", True, res["job_id"])


def _v2_poll(job_id, plan_id):
    if not job_id:
        return _v2_empty_poll_outputs("Nothing queued yet.", timer_active=False)

    job = bill_import_worker.get_job(job_id)
    if not job:
        return _v2_empty_poll_outputs("❌ Job not found.", timer_active=False)

    if job["status"] in ("PENDING", "PROCESSING"):
        out = list(_v2_empty_poll_outputs(f"⏳ {job['status']}...", timer_active=True))
        return tuple(out)

    if job["status"] == "FAILED":
        return _v2_empty_poll_outputs(f"❌ Job failed: {job.get('error') or 'unknown error'}", timer_active=False)

    # DONE
    prop = job.get("proposal") or {}
    lines = prop.get("lines") or []
    roster = job.get("known_roster") or []
    names = {int(r["member_id"]): r["name"] for r in roster}

    try:
        bill_pkey = vectorstore.period_key(int(prop.get("year")), str(prop.get("month")))
    except Exception:
        bill_pkey = None

    with SessionLocal() as db:
        view = allocation_view.build_member_allocation_view(
            db, plan_id, roster, lines, bill_period_key=bill_pkey, lookback_months=VECTOR_RETRIEVAL_LOOKBACK_MONTHS
        )

    unresolved_df = _v2_unresolved_rows(lines)
    unresolved_choices = _v2_unresolved_identifier_choices(lines)
    unresolved_update = (
        gr.update(visible=not unresolved_df.empty),
        unresolved_df,
        gr.update(choices=unresolved_choices, value=(unresolved_choices[0] if unresolved_choices else None)),
    )

    # Read-only once there's an invoice attached - either this job was
    # EVALUATE_ONLY from the start (a bill for that period already existed),
    # or it was NORMAL and has since been approved. Either way, further
    # corrections must go through Payments -> Invoices from here on, not by
    # re-editing/re-approving this job's review screen (which used to stay
    # editable forever and could silently double-write allocations).
    already_approved = job["mode"] != "EVALUATE_ONLY" and bool(job.get("invoice_id"))
    if job["mode"] == "EVALUATE_ONLY" or already_approved:
        if already_approved:
            with SessionLocal() as db:
                diff = bill_import_worker.get_live_diff_for_approved_job(db, job_id) or {}
        else:
            diff = job.get("diff") or {}
        diff_rows = []
        for r in diff.get("per_member", []):
            diff_rows.append(
                {
                    "Member": names.get(int(r["member_id"]), str(r["member_id"])),
                    "Actual": r["actual_amount"],
                    "Proposed": r["proposed_amount"],
                    "Diff": r["diff"],
                    "Basis": _BASIS_LABEL.get(r.get("basis"), r.get("basis") or ""),
                }
            )
        diff_rows.append(
            {
                "Member": "**Total**",
                "Actual": diff.get("actual_total", 0),
                "Proposed": diff.get("proposed_total", 0),
                "Diff": diff.get("total_diff", 0),
                "Basis": "",
            }
        )
        unmatched_total = diff.get("unmatched_total") or 0.0
        unmatched_lines = diff.get("unmatched_lines") or 0
        unattributed = diff.get("unattributed_total") or 0.0
        diff_md = (
            f"**Existing invoice total:** ${diff.get('actual_total', 0):.2f} &nbsp;|&nbsp; "
            f"**Proposed total:** ${diff.get('proposed_total', 0):.2f} &nbsp;|&nbsp; "
            f"**Diff:** ${diff.get('total_diff', 0):.2f}\n\n"
            + (
                f"⚠️ **${unmatched_total:.2f}** across **{unmatched_lines}** line(s) had an identifier that "
                "matched nobody in this plan - see \"Unresolved charges\" below to link it for next time.\n\n"
                if unmatched_lines
                else ""
            )
            + (
                f"⚠️ **${unattributed:.2f}** in shared/unmatched charges isn't reflected in any row below "
                "because this plan has no owner set (Plan settings) to absorb it.\n\n"
                if unattributed > 0.005
                else ""
            )
            + (
                "_This is a **read-only accuracy check** - this job has already been approved, so nothing "
                "above ever touches your ledger anymore. To correct the real invoice's amounts, edit it "
                "directly from **Payments → Invoices**._"
                if already_approved
                else "_This is a **read-only accuracy check** - this bill's period already has an approved invoice, "
                "so nothing above ever touches your ledger. To correct the real invoice's amounts, edit it "
                "directly from **Payments → Invoices**._"
            )
        )
        status_msg = (
            "✅ Done (already approved - showing a read-only comparison against the current invoice)."
            if already_approved
            else "✅ Done (evaluate-only - a bill for this period already exists)."
        )
        return (
            status_msg,
            gr.update(active=False),
            prop,
            gr.update(visible=True), diff_md, _v2_facts_rows(lines, names), pd.DataFrame(diff_rows),
            gr.update(visible=False), None, None, None, "", pd.DataFrame(), "", pd.DataFrame(),
        ) + unresolved_update + (None,)

    # NORMAL mode -> review + approve
    notes_md = f"**Notes:** {prop.get('notes', '')}"
    alloc_df = _v2_alloc_rows(view, prop.get("total_amount"))
    return (
        "✅ Done - new bill, review below.",
        gr.update(active=False),
        prop,
        gr.update(visible=False), "", pd.DataFrame(), pd.DataFrame(),
        gr.update(visible=True),
        prop.get("year"), prop.get("month"), prop.get("total_amount"),
        notes_md, _v2_facts_rows(lines, names),
        _v2_reconcile_text(prop.get("total_amount"), view), alloc_df,
    ) + unresolved_update + (alloc_df,)


def _v2_poll_keep_status(job_id, plan_id):
    """
    Same as _v2_poll, but drops the status message - used right after a
    successful approve so the rest of the screen (diff/review sections,
    reconcile banner, etc.) immediately flips into the job's new
    already-approved read-only state, without clobbering the "✅ Upserted
    invoice..." confirmation _v2_approve just wrote to v2_status.
    """
    return _v2_poll(job_id, plan_id)[1:]


def _v2_refresh_recent(plan_id):
    if not plan_id:
        return pd.DataFrame()
    jobs = bill_import_worker.list_recent_jobs(plan_id, limit=10)
    rows = []
    for j in jobs:
        rows.append(
            {
                "id": j["id"],
                "status": j["status"],
                "mode": j["mode"] or "",
                "created_at": str(j["created_at"]) if j["created_at"] else "",
                "error": (j["error"] or "")[:120],
            }
        )
    return pd.DataFrame(rows)


def _fmt_cost(v) -> str:
    return f"${v:.6f}" if isinstance(v, (int, float)) else "n/a"


def _v2_inspector_choices(plan_id):
    if not plan_id:
        return gr.update(choices=[], value=None)
    jobs = bill_import_worker.list_recent_jobs(plan_id, limit=50)
    choices = [f"{j['id']} | {j['status']} | {j['mode'] or '-'} | {j['created_at']}" for j in jobs]
    return gr.update(choices=choices, value=(choices[0] if choices else None))


def _parse_job_choice(choice: str | None):
    if not choice:
        return None
    try:
        return int(str(choice).split("|", 1)[0].strip())
    except Exception:
        return None


_INSPECT_EMPTY = (
    "Pick a job above and click Inspect.",
    "", "", "", pd.DataFrame(), "", "",
    pd.DataFrame(), gr.update(visible=False), pd.DataFrame(),
)


def _v2_inspect(job_choice):
    """
    Purely read-only: pulls everything already stored on a BillImportJob row
    (cleaned text, chunks sent, precedent used, roster, exact system prompt,
    raw LLM response, parsed proposal, diff) so an owner can see exactly what
    happened for ANY job regardless of status/mode - never writes anything.
    """
    job_id = _parse_job_choice(job_choice)
    if not job_id:
        return _INSPECT_EMPTY

    job = bill_import_worker.get_job(job_id)
    if not job:
        return (f"❌ Job #{job_id} not found.",) + _INSPECT_EMPTY[1:]

    usage = job.get("token_usage") or {}
    chat_cost = usage.get("chat_cost_usd")
    emb_cost = usage.get("embedding_cost_usd")
    total_cost = (chat_cost or 0.0) + (emb_cost or 0.0) if (chat_cost is not None or emb_cost is not None) else None

    summary_lines = [
        f"**Job #{job['id']}** — status **{job['status']}**, mode **{job['mode'] or '-'}**  ",
        f"Uploaded by member: {job.get('uploaded_by_member_id') or '-'} &nbsp;|&nbsp; "
        f"Content hash: `{(job.get('content_hash') or '')[:16]}…`  ",
        f"Created: {job['created_at']} &nbsp;|&nbsp; Started: {job['started_at']} &nbsp;|&nbsp; "
        f"Completed: {job['completed_at']}  ",
        f"**Cache hit count:** {job['cache_hit_count']} "
        "(# of identical re-uploads that short-circuited to this job's cached result)  ",
        f"**Chat tokens:** prompt={usage.get('chat_prompt_tokens', '-')}, "
        f"completion={usage.get('chat_completion_tokens', '-')}, "
        f"total={usage.get('chat_total_tokens', '-')} &nbsp;|&nbsp; **Chat cost:** {_fmt_cost(chat_cost)}  ",
        f"**Embedding tokens:** {usage.get('embedding_tokens', '-')} &nbsp;|&nbsp; "
        f"**Embedding cost:** {_fmt_cost(emb_cost)}  ",
        f"**Estimated total cost:** {_fmt_cost(total_cost)}",
    ]
    if job.get("error"):
        summary_lines.append(f"\n**Error:** {job['error']}")
    summary = "\n".join(summary_lines)

    cleaned_text = job.get("cleaned_text") or ""
    chunks_text = "\n\n---\n\n".join(job.get("selected_chunks") or []) or "(no chunks recorded)"
    precedent_text = "\n".join(f"- {p}" for p in (job.get("precedent_used") or [])) or "(no precedent used - likely the first bill on file for this plan/period)"

    roster_rows = [
        {
            "member_id": r.get("member_id"),
            "name": r.get("name"),
            "known_identifiers": json.dumps(r.get("known_identifiers") or []),
        }
        for r in (job.get("known_roster") or [])
    ]
    roster_df = pd.DataFrame(roster_rows)

    system_prompt = job.get("system_prompt") or ""

    raw = job.get("llm_raw_response") or ""
    try:
        raw_pretty = json.dumps(json.loads(raw), indent=2)
    except Exception:
        raw_pretty = raw

    prop = job.get("proposal") or {}
    prop_rows = []
    for ln in prop.get("lines") or []:
        ident = ln.get("identifier") or {"type": "none", "value": ""}
        prop_rows.append(
            {
                "identifier_type": ident.get("type"),
                "identifier_value": ident.get("value"),
                "display": ln.get("display", ""),
                "line_total": round(float(ln.get("line_total") or 0.0), 2),
                "matched_member_id": ln.get("matched_member_id"),
                "match_source": ln.get("match_source") or "",
                "confidence": round(float(ln.get("confidence") or 0.0), 2),
            }
        )
    prop_df = pd.DataFrame(prop_rows)

    already_approved = job.get("mode") != "EVALUATE_ONLY" and bool(job.get("invoice_id"))
    diff_visible = job.get("mode") == "EVALUATE_ONLY" or already_approved
    diff_rows = []
    diff_data: dict = {}
    if diff_visible:
        if already_approved:
            with SessionLocal() as db:
                diff_data = bill_import_worker.get_live_diff_for_approved_job(db, job_id) or {}
        else:
            diff_data = job.get("diff") or {}
        names = {int(r["member_id"]): r["name"] for r in (job.get("known_roster") or [])}
        for r in diff_data.get("per_member", []):
            diff_rows.append(
                {
                    "member_id": r.get("member_id"),
                    "member": names.get(int(r["member_id"]), str(r["member_id"])),
                    "actual_amount": r.get("actual_amount"),
                    "proposed_amount": r.get("proposed_amount"),
                    "diff": r.get("diff"),
                    "basis": _BASIS_LABEL.get(r.get("basis"), r.get("basis") or ""),
                }
            )
        if diff_data.get("unmatched_lines"):
            diff_rows.append(
                {
                    "member_id": "-",
                    "member": f"⚠️ Unmatched ({diff_data['unmatched_lines']} line(s) - no member match, folded into owner's row above)",
                    "actual_amount": 0.0,
                    "proposed_amount": diff_data.get("unmatched_total") or 0.0,
                    "diff": "",
                    "basis": "",
                }
            )
        diff_rows.append(
            {
                "member_id": "-",
                "member": "**Total**",
                "actual_amount": diff_data.get("actual_total", 0),
                "proposed_amount": diff_data.get("proposed_total", 0),
                "diff": diff_data.get("total_diff", 0),
                "basis": "",
            }
        )
    diff_df = pd.DataFrame(diff_rows)
    unattributed_note = (
        f"\n\n⚠️ **${diff_data.get('unattributed_total', 0):.2f}** in shared/unmatched charges isn't reflected "
        "in any row above because this plan has no owner set (Plan settings) to absorb it."
        if diff_visible and (diff_data.get("unattributed_total") or 0) > 0.005
        else ""
    )

    return (
        summary + unattributed_note,
        cleaned_text,
        chunks_text,
        precedent_text,
        roster_df,
        system_prompt,
        raw_pretty,
        prop_df,
        gr.update(visible=diff_visible),
        diff_df,
    )


def _v2_load_into_review(job_choice, plan_id):
    """
    Deterministic escape hatch, independent of the live status-polling
    Timer: point v2_job_id_state at a specific already-DONE job and render
    it via the exact same _v2_poll used for live polling. Needed because
    the Timer/gr.State-based flow only tracks "the job you most recently
    enqueued in THIS browser session" - a page reload, a long-running job
    finishing while you weren't watching, or any other reason that session
    state didn't carry through otherwise leaves no way back to a job's
    review/approve screen except re-uploading the identical PDF again (which
    happens to work because it hits the content-hash cache).
    """
    # Matches the length of v2_poll_outputs (defined where this is wired up) -
    # _v2_empty_poll_outputs()'s return shape is the source of truth for that
    # count, so this can't silently drift out of sync with it.
    skip_rest = tuple(gr.skip() for _ in _v2_empty_poll_outputs("", timer_active=False))

    job_id = _parse_job_choice(job_choice)
    if not job_id:
        return ("❌ Pick a job from the dropdown first.", gr.skip()) + skip_rest

    job = bill_import_worker.get_job(job_id)
    if not job:
        return (f"❌ Job #{job_id} not found.", gr.skip()) + skip_rest
    if plan_id and job.get("plan_id") != int(plan_id):
        return (f"❌ Job #{job_id} belongs to a different plan than the active one.", gr.skip()) + skip_rest
    if job["status"] != "DONE":
        return (f"❌ Job #{job_id} isn't finished yet (status={job['status']}).", gr.skip()) + skip_rest

    poll_result = _v2_poll(job_id, plan_id)
    if job["mode"] == "EVALUATE_ONLY":
        msg = (
            f"✅ Loaded job #{job_id}'s **read-only** evaluate-only comparison above - there's nothing to "
            "approve here since this bill's period already has an approved invoice. To correct that invoice's "
            "amounts, edit it directly from Payments → Invoices."
        )
    elif job.get("invoice_id"):
        msg = (
            f"✅ Loaded job #{job_id}'s **read-only** comparison above - this job was already approved "
            "(invoice already created), so it can no longer be edited/re-approved from here. To correct that "
            "invoice's amounts, edit it directly from Payments → Invoices."
        )
    else:
        msg = f"✅ Loaded job #{job_id} into the review/approve section above."
    return (msg, job_id) + poll_result


def _v2_approve(y, m, tot, alloc_df, role=None, member_id=None, plan_id=None, job_id=None):
    """
    Writes exactly the per-member amounts shown/edited in Section B (one row
    per plan member, keyed by member_id) - no more separate identifier ->
    member "mappings" dict at approval time. This also structurally fixes
    the old overwrite-not-sum bug: since there's exactly one row per member
    here (never two rows for the same person), there's no way for a second
    write to silently clobber a first one the way two bill-line rows
    mapped to the same member used to.
    """
    try:
        try:
            y = int(y)
            m = str(m)
            tot = float(tot or 0.0)
        except Exception:
            return "❌ Invalid year/month/total"

        if m not in MONTHS:
            return "❌ Invalid month"
        if tot <= 0:
            return "❌ Total must be > 0"

        d = pd.DataFrame(alloc_df) if not isinstance(alloc_df, pd.DataFrame) else alloc_df
        if d is None or d.empty or "member_id" not in d.columns or "amount" not in d.columns:
            return "❌ No amounts table. Run AI extraction (beta) first."

        if not plan_id:
            return "❌ Pick a specific Active plan (not \"All Plans\") to import a bill for."

        d2 = d.copy()
        d2["amount"] = pd.to_numeric(d2["amount"], errors="coerce").fillna(0.0)

        # Hard-block, not just the soft warning banner above the table -
        # "the total must always equal the correct total of the bill" is a
        # real invariant now, not just a suggestion (equal split / % mode
        # already keep this true by construction; this only ever actually
        # fires after a manual $ edit that overshoots or undershoots).
        assigned = round(float(d2["amount"].sum()), 2)
        remaining = round(tot - assigned, 2)
        if abs(remaining) > 0.01:
            return (
                f"❌ Amounts (${assigned:.2f}) don't add up to the bill total (${tot:.2f}) - off by "
                f"${remaining:.2f}. Fix an amount/percent above (or click ⚖️ Equal split) until the banner "
                "reads fully reconciled, then approve."
            )

        with SessionLocal() as db:
            if not authz.can_manage_plan(db, role, member_id, plan_id):
                return "❌ You don't have write access to the active plan."

            valid_member_ids = {r.id for r in plans_service.get_plan_members(db, plan_id)}

            alloc_rows: list[tuple[int, float]] = []
            skipped_unknown = 0
            for _, r in d2.iterrows():
                try:
                    mid = int(r.get("member_id"))
                except Exception:
                    continue
                if mid not in valid_member_ids:
                    skipped_unknown += 1
                    continue
                alloc_rows.append((mid, float(r.get("amount") or 0.0)))

            if not alloc_rows:
                return "❌ No valid member amounts to write."

            inv = db.execute(
                select(Invoice).where(Invoice.plan_id == plan_id, Invoice.year == y, Invoice.month == m)
            ).scalars().first()
            if inv is None:
                inv = Invoice(plan_id=plan_id, year=y, month=m, total_amount=tot)
                db.add(inv)
                db.flush()
            else:
                inv.total_amount = tot

            for mid, amt in alloc_rows:
                existing = db.execute(
                    select(Allocation).where(Allocation.invoice_id == inv.id, Allocation.member_id == mid)
                ).scalars().first()
                if existing:
                    existing.amount_due = amt
                else:
                    db.add(Allocation(invoice_id=inv.id, member_id=mid, amount_due=amt))

            db.commit()

            # Link the source job back to the invoice it produced, so the eval
            # harness (eval/run_eval.py) has comparable ground truth for
            # NORMAL-mode jobs too, not just EVALUATE_ONLY ones.
            if job_id:
                job = db.get(BillImportJob, int(job_id))
                if job:
                    job.invoice_id = inv.id
                    db.commit()

            # Freeze "what the model predicted vs. what got approved" right
            # now, permanently - whether or not anything was hand-corrected
            # above, and regardless of any later edits to this invoice from
            # the Invoices ledger UI (see record_approval_accuracy's
            # docstring for why this can't just be "re-run eval later").
            if job_id:
                try:
                    bill_import_worker.record_approval_accuracy(
                        db, int(job_id), plan_id, {mid: amt for mid, amt in alloc_rows}
                    )
                except Exception:
                    pass  # never block a successful ledger write on a logging hiccup

            # Post-approve: build outcome facts from this already-approved data
            # (no LLM call) and upsert into the vector store for future precedent.
            try:
                facts = vectorstore.build_outcome_facts_for_invoice(db, inv.id)
                vectorstore.upsert_outcome_facts(plan_id=plan_id, invoice_ref=f"invoice-{inv.id}", year=y, month=m, facts=facts)
            except Exception:
                pass  # never block a successful ledger write on a vector-store hiccup

        warn = f" (⚠️ skipped {skipped_unknown} row(s) with an unrecognized member_id)" if skipped_unknown else ""
        return (
            f"✅ (beta) Upserted invoice {y}-{m} total=${tot:.2f}. Wrote {len(alloc_rows)} member allocations.{warn}"
        )
    except Exception as e:
        # Never let an unexpected error leave the Approve button permanently
        # disabled (the click chain's re-enable step only runs if this
        # returns normally) - surface it instead.
        return f"❌ Unexpected error: {e}"


def ui_bill_import(demo, current_role, current_member_id, current_plan_id):
    with gr.Column():
        gr.Markdown(
            """
# 🧾 Bill Import (LLM-assisted)

Upload a bill PDF below - it's cleaned, matched against your plan's members, and proposes an
invoice + per-person amounts for you to review and approve. The old manual step-by-step flow is
still available, collapsed under "Legacy manual bill import" further down.

_Scoped to the Active plan selected at the top of the app._
"""
        )

        proposal_state = gr.State(None)   # full proposal dict for charge viewer
        mappings_state = gr.State({})     # {phone_key: member_id}

        write_denied_banner = gr.Markdown(
            "⚠️ You don't have write access to the active plan. Switch to a plan you own to import a bill.",
            visible=False,
        )

        # -------------------------
        # 0) Opt-in: AI extraction (beta) - RAG pipeline
        # -------------------------
        use_v2_checkbox = gr.Checkbox(
            label="🧪 AI-assisted import (uncheck to hide and use the legacy manual flow only)",
            value=True,
        )

        with gr.Group(visible=True) as v2_group:
            gr.Markdown(
                "## 🧪 AI extraction (beta) - carrier-aware, cost-capped RAG pipeline\n"
                "Runs in the background (content-hash cached, rate-limited per plan/hour). "
                "If a bill for this plan/period is already on file, this runs in **evaluate-only** "
                "mode and never touches your ledger - it only shows you a comparison."
            )

            v2_job_id_state = gr.State(None)
            v2_proposal_state = gr.State(None)
            # Last-rendered snapshot of v2_alloc_table, kept only so
            # _v2_recalc_alloc can tell (by diffing) whether an edit event
            # changed "amount" or "percent" for a given row - see its
            # docstring. Always kept in sync with v2_alloc_table itself
            # (same value, written every time the table is).
            v2_alloc_prev_state = gr.State(None)

            with gr.Row():
                v2_pdf = gr.File(label="Upload bill PDF (beta)", file_types=[".pdf"])
                v2_enqueue_btn = gr.Button("Enqueue AI extraction (beta)", variant="primary")

            with gr.Row():
                v2_status = gr.Textbox(label="Job status", interactive=False, scale=4)
                v2_check_status_btn = gr.Button("🔄 Check status now", scale=1)
            v2_timer = gr.Timer(value=3.0, active=False)

            with gr.Accordion("Recent AI imports (this plan)", open=False):
                v2_recent_refresh_btn = gr.Button("Refresh")
                v2_recent_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True, label="Recent AI imports")

            with gr.Accordion("Read-only comparison (bill already on file / job already approved)", open=True, visible=False) as v2_diff_accordion:
                v2_diff_md = gr.Markdown()
                gr.Markdown("#### What we found on this bill")
                v2_diff_facts_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)
                gr.Markdown("#### Existing invoice vs. what this bill would have proposed")
                v2_diff_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)

            with gr.Accordion("Review & approve (new bill)", open=True, visible=False) as v2_review_accordion:
                with gr.Row():
                    v2_year = gr.Number(label="Year", precision=0)
                    v2_month = gr.Dropdown(MONTHS, label="Month")
                    v2_total = gr.Number(label="Invoice total")

                v2_notes = gr.Markdown()

                gr.Markdown("#### What we found on this bill")
                v2_facts_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)

                gr.Markdown(
                    "#### Amounts by person\n"
                    "_One row per plan member. Shared/unmatched charges (e.g. a bundled \"Account\" line) are "
                    "split equally across whoever actually has a line on **this** bill - members not on this "
                    "bill default to $0 (their last known amount is shown as a note, for reference only). Any "
                    "leftover falls to the plan owner._\n\n"
                    "_Three ways to adjust: type a **$ Amount** directly, type a **% Percent** (it recalculates "
                    "the $ Amount for that person from the bill total automatically), or click **⚖️ Equal split** "
                    "to reset everyone to an even share. Editing either $ or % always keeps the other one in sync "
                    "for that row - approving is blocked until the amounts add back up to the exact bill total._"
                )
                v2_reconcile_banner = gr.Markdown()
                v2_alloc_table = gr.Dataframe(
                    value=pd.DataFrame(),
                    interactive=True,
                    wrap=True,
                    datatype=["number", "str", "number", "number", "str", "str"],
                    static_columns=[0, 1, 4, 5],
                    label="Amounts by person",
                )
                v2_equal_split_btn = gr.Button("⚖️ Equal split")

                v2_approve_btn = gr.Button("✅ Approve & create invoice (beta)", variant="primary")

            with gr.Group(visible=False) as v2_unresolved_group:
                gr.Markdown(
                    "### ❓ Unresolved charges\n"
                    "These had an identifier (phone/email/name) on the bill that doesn't match anyone in this "
                    "plan yet. Pick who each one belongs to and link it - updates the amounts above immediately "
                    "and saves the mapping for future bills."
                )
                v2_unresolved_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)
                with gr.Row():
                    v2_unresolved_identifier_pick = gr.Dropdown(label="Unresolved identifier", choices=[], value=None)
                    v2_unresolved_member_pick = gr.Dropdown(label="Belongs to", choices=[], value=None)
                    v2_unresolved_link_btn = gr.Button("Link", variant="primary")
                v2_unresolved_status = gr.Textbox(label="Status", interactive=False)

            with gr.Accordion("🔎 Inspect a job (owner only)", open=False, visible=False) as v2_inspector_accordion:
                gr.Markdown(
                    "See exactly what was cleaned, selected, sent to the LLM, and returned for **any** job in "
                    "this plan - including evaluate-only ones. Purely read-only, never touches your ledger."
                )
                with gr.Row():
                    v2_inspect_job_pick = gr.Dropdown(label="Job (id | status | mode | created_at)", choices=[], value=None)
                    v2_inspect_refresh_btn = gr.Button("Refresh job list")
                    v2_inspect_btn = gr.Button("Inspect", variant="primary")

                with gr.Row():
                    v2_load_review_btn = gr.Button(
                        "⬆️ Load this job into Review & Approve (above)", variant="secondary"
                    )
                v2_load_review_status = gr.Markdown(
                    "_For a `DONE`/`NORMAL` job not yet approved - a reliable way to reopen the review/approve "
                    "screen at any time, independent of the live status polling above (e.g. after a page "
                    "reload, or if a job finished while you weren't watching)._"
                )

                v2_inspect_summary = gr.Markdown()

                with gr.Accordion("Cleaned text sent through the pipeline (full)", open=False):
                    v2_inspect_cleaned = gr.Textbox(label="Cleaned bill text", lines=12, interactive=False)
                with gr.Accordion("Chunks selected & sent to the LLM", open=False):
                    v2_inspect_chunks = gr.Textbox(label="Selected chunks", lines=12, interactive=False)
                with gr.Accordion("Historical precedent used", open=False):
                    v2_inspect_precedent = gr.Textbox(label="Precedent facts retrieved from the vector store", lines=8, interactive=False)
                with gr.Accordion("Known roster sent to the LLM", open=False):
                    v2_inspect_roster = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True, label="Roster snapshot")
                with gr.Accordion("System prompt", open=False):
                    v2_inspect_prompt = gr.Textbox(label="System prompt sent to the LLM", lines=12, interactive=False)
                with gr.Accordion("Raw LLM response", open=False):
                    v2_inspect_raw = gr.Textbox(label="Raw LLM JSON response", lines=14, interactive=False)

                gr.Markdown("#### Parsed proposal (all lines, including unmatched/`identifier.type == \"none\"` ones)")
                v2_inspect_proposal_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)

                with gr.Accordion("Diff vs. actual invoice (evaluate-only jobs, or already-approved jobs)", open=True, visible=False) as v2_inspect_diff_accordion:
                    v2_inspect_diff_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)

        # -------------------------
        # Legacy manual flow - superseded by the AI extraction (beta) flow
        # above, which is now the default. Kept around (collapsed) as a
        # manual fallback, not shown by default.
        # -------------------------
        with gr.Accordion("⚙️ Legacy manual bill import (old flow)", open=False):
            # -------------------------
            # 1) Upload + Extract
            # -------------------------
            with gr.Group():
                gr.Markdown("## 1) Upload & extract")

                with gr.Row():
                    pdf = gr.File(label="Upload bill PDF", file_types=[".pdf"])
                    extract_btn = gr.Button("Extract text", variant="primary")

                status = gr.Textbox(label="Status", interactive=False)

                with gr.Accordion("Extracted text preview (optional)", open=False):
                    text_preview = gr.Textbox(label="Extracted text (preview)", lines=12)

            # -------------------------
            # 2) LLM Proposal
            # -------------------------
            with gr.Group():
                gr.Markdown("## 2) Generate proposal (LLM)")
                llm_btn = gr.Button("Run LLM → propose invoice + allocations", variant="primary")
                # debug_run_btn = gr.Button("🧪 Debug Run LLM (one output)")
                # debug_out = gr.Textbox(label="Debug output", lines=18, interactive=False)

                with gr.Row():
                    year = gr.Number(label="Year", precision=0, value=date.today().year)
                    month = gr.Dropdown(MONTHS, label="Month", value=MONTHS[date.today().month - 1])
                    total = gr.Number(label="Invoice total", value=0)

                with gr.Row():
                    confidence = gr.Textbox(label="LLM confidence", interactive=False)
                    suggested_sum = gr.Textbox(label="Suggested sum", interactive=False)
                    diff_vs_total = gr.Textbox(label="Diff (total - suggested)", interactive=False)

                with gr.Accordion("Evidence & notes", open=True):
                    evidence = gr.Markdown()
                    notes = gr.Markdown()

                with gr.Accordion("Filtered text sent to LLM (debug)", open=False):
                    llm_input_preview = gr.Textbox(label="LLM input preview", lines=10)

                with gr.Accordion("Debug traceback (only if something fails)", open=False):
                    debug = gr.Textbox(label="Traceback", lines=12, interactive=False)

            # -------------------------
            # 3) Review & Edit
            # -------------------------
            with gr.Group():
                gr.Markdown("## 3) Review & edit allocations")
                proposal_df = gr.Dataframe(
                    value=pd.DataFrame(),
                    interactive=True,
                    wrap=True,
                    row_count=25,
                    label="Proposed allocations (editable: suggested_amount)",
                )

            # -------------------------
            # 4) Mapping
            # -------------------------
            with gr.Group():
                gr.Markdown("## 4) Map phone → member")

                with gr.Row():
                    phone_pick = gr.Dropdown(label="Phone key", choices=[], value=None)
                    member_pick = gr.Dropdown(label="Member", choices=[], value=None)

                with gr.Row():
                    map_btn = gr.Button("Add/Update mapping")
                    save_map_btn = gr.Button("Save mapping to DB (phone_last4)")
                    clear_map_btn = gr.Button("Clear mappings")

                mappings_table = gr.Dataframe(
                    value=pd.DataFrame(),
                    interactive=False,
                    wrap=True,
                    row_count=15,
                    label="Current mappings",
                )

            # -------------------------
            # 5) Validate + Approve
            # -------------------------
            with gr.Group():
                gr.Markdown("## 5) Validate & approve")

                with gr.Row():
                    compute_owner = gr.Checkbox(
                        label="Owner absorbs remainder (owner allocation = total - sum(others))",
                        value=True,
                    )
                    owner_pick = gr.Dropdown(label="Owner member", choices=[], value=None)

                with gr.Row():
                    validate_btn = gr.Button("Validate proposal")
                    approve_btn = gr.Button("Approve & upsert to DB", variant="primary")

                validation_box = gr.Textbox(label="Validation", interactive=False, lines=8)

            # -------------------------
            # Charges viewer (optional)
            # -------------------------
            with gr.Accordion("Charge explorer (optional)", open=False):
                charges_phone_pick = gr.Dropdown(label="Phone key", choices=[], value=None)
                charges_table = gr.Dataframe(value=pd.DataFrame(), interactive=False, wrap=True)
                charges_evidence = gr.Markdown()

        # -------------------------
        # Callbacks
        # -------------------------

        def _debug_llm(text):
            try:
                if not text:
                    return "NO TEXT IN INPUT"
                return f"TEXT LEN={len(text)}\nFIRST 400:\n{text[:400]}"
            except Exception:
                return traceback.format_exc() 

        def _load_member_choices(plan_id=None):
            choices = _member_choice_list(plan_id)
            return gr.update(choices=choices, value=(choices[0] if choices else None))

        def _write_access_update(role, member_id, plan_id):
            with SessionLocal() as db:
                allowed = authz.can_manage_plan(db, role, member_id, plan_id)
            return gr.update(visible=not allowed)

        def _extract(pdf_file):
            if not pdf_file:
                return "❌ Upload a PDF first.", "", "", ""
            try:
                text = extract_pdf_text(pdf_file.name)
                return "✅ Extracted text.", text[:8000], "", ""
            except Exception:
                return "❌ Failed to extract text.", "", "", traceback.format_exc()

        def _llm_with_preview(text):
            if not text or len(text.strip()) < 200:
                return (
                    gr.update(), gr.update(), gr.update(),
                    "", "0.00", "0.00",
                    "", "", pd.DataFrame(),
                    gr.update(choices=[]), gr.update(choices=[]),
                    None,
                    {}, pd.DataFrame(),
                    "❌ Extract text first.",
                    "",
                    ""
                )
            try:
                filtered = filter_text_for_llm(text, max_pages=3, max_chars=12000)
                prop = extract_bill_proposal(filtered)

                prop_dict = {
                    "year": prop.year,
                    "month": prop.month,
                    "total_amount": prop.total_amount,
                    "confidence": prop.confidence,
                    "evidence_total": prop.evidence_total,
                    "evidence_period": prop.evidence_period,
                    "unassigned_amount": prop.unassigned_amount,
                    "notes": prop.notes,
                    "lines": prop.lines,
                    "allocation_by_phone": prop.allocation_by_phone,
                }

                ev_md = (
                    f"**Evidence (Total):**\n\n> {prop.evidence_total}\n\n"
                    f"**Evidence (Period):**\n\n> {prop.evidence_period}\n"
                )
                notes_md = f"**Notes:** {prop.notes or ''}\n\n**Unassigned pool:** ${prop.unassigned_amount:.2f}"

                sug_map = {a["phone_key"]: a["suggested_amount"] for a in prop.allocation_by_phone}
                rows = []
                for ln in prop.lines:
                    pk = ln.get("phone_key", "")
                    rows.append({
                        "phone_key": pk,
                        "display": ln.get("display", ""),
                        "line_total": round(float(ln.get("line_total", 0.0)), 2),
                        "suggested_amount": round(float(sug_map.get(pk, 0.0)), 2),
                        "confidence": round(float(ln.get("confidence", 0.0)), 2),
                        "source": ln.get("source", ""),
                        "evidence_total_line": ln.get("evidence_total_line", ""),
                    })
                df = pd.DataFrame(rows)

                phone_choices = sorted(df["phone_key"].unique().tolist()) if not df.empty else []

                # auto-map from DB if available
                auto_map = _auto_map_from_db(phone_choices)
                map_df = _mapping_table(auto_map)

                # compute sum/diff
                ssum, diff = _calc_sum_diff(prop.total_amount, df)

                phone_pick_update = gr.update(choices=phone_choices, value=(phone_choices[0] if phone_choices else None))
                charges_pick_update = gr.update(choices=phone_choices, value=(phone_choices[0] if phone_choices else None))

                return (
                    prop.year,
                    prop.month,
                    prop.total_amount,
                    f"{prop.confidence:.2f}",
                    ssum,
                    diff,
                    ev_md,
                    notes_md,
                    df,
                    phone_pick_update,
                    charges_pick_update,
                    prop_dict,
                    auto_map,
                    map_df,
                    "✅ Proposal generated. Edit suggested_amount, map phones, validate, then approve.",
                    filtered[:6000],
                    "",
                )
            except Exception:
                return (
                    gr.update(), gr.update(), gr.update(),
                    "", "0.00", "0.00",
                    "", "", pd.DataFrame(),
                    gr.update(choices=[]), gr.update(choices=[]),
                    None,
                    {}, pd.DataFrame(),
                    "❌ LLM proposal failed. Open Debug traceback accordion.",
                    "",
                    traceback.format_exc(),
                )

        def _charges_for_phone(phone_key, proposal):
            if not proposal or not phone_key:
                return pd.DataFrame(), ""
            for ln in proposal.get("lines", []):
                if str(ln.get("phone_key")) == str(phone_key):
                    charges = ln.get("charges") or []
                    df = pd.DataFrame([{
                        "label": c.get("label", ""),
                        "amount": float(c.get("amount") or 0.0),
                        "evidence": c.get("evidence", ""),
                    } for c in charges])
                    md = f"Showing charges for **{phone_key}** (rows={len(df)})"
                    return df, md
            return pd.DataFrame(), f"No charges found for **{phone_key}**"

        def _add_mapping(phone_key, member_choice, cur_map: dict):
            if not phone_key:
                return cur_map, pd.DataFrame([{"error": "Pick a phone_key"}])
            mid = _parse_id(member_choice)
            if not mid:
                return cur_map, pd.DataFrame([{"error": "Pick a member"}])
            cur_map = dict(cur_map or {})
            cur_map[str(phone_key)] = int(mid)
            return cur_map, _mapping_table(cur_map)

        def _save_mapping_to_db(phone_key, member_choice, cur_map: dict):
            if not phone_key:
                return "❌ Pick a phone_key first.", cur_map, _mapping_table(cur_map)
            mid = _parse_id(member_choice)
            if not mid:
                return "❌ Pick a member first.", cur_map, _mapping_table(cur_map)

            if not hasattr(Member, "phone_last4"):
                return "❌ Member.phone_last4 column not found (add it to model + migration).", cur_map, _mapping_table(cur_map)

            last4 = _last4_from_phone_key(phone_key)
            if not last4:
                return f"❌ Could not parse last4 from {phone_key}", cur_map, _mapping_table(cur_map)

            with SessionLocal() as db:
                m = db.get(Member, int(mid))
                if not m:
                    return "❌ Member not found.", cur_map, _mapping_table(cur_map)
                m.phone_last4 = last4
                db.commit()

            cur_map = dict(cur_map or {})
            cur_map[str(phone_key)] = int(mid)
            return f"✅ Saved mapping: {phone_key} → Member {mid} (phone_last4={last4})", cur_map, _mapping_table(cur_map)

        def _clear_mappings():
            return {}, pd.DataFrame()

        def _approve_upsert(y, m, tot, df, mappings: dict, owner_choice, do_owner: bool, role=None, member_id=None, plan_id=None):
            try:
                y = int(y)
                m = str(m)
                tot = float(tot or 0.0)
            except Exception:
                return "❌ Invalid year/month/total"

            if m not in MONTHS:
                return "❌ Invalid month"
            if tot <= 0:
                return "❌ Total must be > 0"

            d = pd.DataFrame(df) if not isinstance(df, pd.DataFrame) else df
            if d is None or d.empty:
                return "❌ No proposal table. Generate proposal first."

            mappings = dict(mappings or {})
            if not mappings:
                return "❌ No mappings yet. Map phone_key → member first."

            owner_id = _parse_id(owner_choice) if owner_choice else None
            if do_owner and not owner_id:
                return "❌ Pick an owner member (or uncheck owner remainder)."

            if "suggested_amount" not in d.columns or "phone_key" not in d.columns:
                return "❌ Proposal table missing required columns."

            d2 = d.copy()
            d2["suggested_amount"] = pd.to_numeric(d2["suggested_amount"], errors="coerce").fillna(0.0)

            alloc_rows = []
            for _, r in d2.iterrows():
                pk = str(r.get("phone_key") or "").strip()
                amt = float(r.get("suggested_amount") or 0.0)
                if amt <= 0:
                    continue
                mid = mappings.get(pk)
                if not mid:
                    continue
                alloc_rows.append((int(mid), amt))

            if not alloc_rows:
                return "❌ No mapped allocations > 0. Ensure suggested_amounts are > 0 and mapped."

            if not plan_id:
                return "❌ Pick a specific Active plan (not \"All Plans\") to import a bill for."

            with SessionLocal() as db:
                if not authz.can_manage_plan(db, role, member_id, plan_id):
                    return "❌ You don't have write access to the active plan."

                inv = db.execute(
                    select(Invoice).where(Invoice.plan_id == plan_id, Invoice.year == y, Invoice.month == m)
                ).scalars().first()
                if inv is None:
                    inv = Invoice(plan_id=plan_id, year=y, month=m, total_amount=tot)
                    db.add(inv)
                    db.flush()
                else:
                    inv.total_amount = tot

                sum_others = 0.0
                for mid, amt in alloc_rows:
                    sum_others += amt
                    existing = db.execute(
                        select(Allocation).where(Allocation.invoice_id == inv.id, Allocation.member_id == mid)
                    ).scalars().first()
                    if existing:
                        existing.amount_due = amt
                    else:
                        db.add(Allocation(invoice_id=inv.id, member_id=mid, amount_due=amt))

                if do_owner and owner_id:
                    owner_amt = float(tot - sum_others)
                    if owner_amt < 0:
                        owner_amt = 0.0
                    existing_owner = db.execute(
                        select(Allocation).where(Allocation.invoice_id == inv.id, Allocation.member_id == int(owner_id))
                    ).scalars().first()
                    if existing_owner:
                        existing_owner.amount_due = owner_amt
                    else:
                        db.add(Allocation(invoice_id=inv.id, member_id=int(owner_id), amount_due=owner_amt))

                db.commit()

            return f"✅ Upserted invoice {y}-{m} total=${tot:.2f}. Wrote {len(alloc_rows)} member allocations" + (" + owner" if do_owner else "")

        # -------------------------
        # Wiring
        # -------------------------
        extract_btn.click(fn=_extract, inputs=[pdf], outputs=[status, text_preview, llm_input_preview, debug])
        # debug_run_btn.click(fn=_debug_llm, inputs=[text_preview], outputs=[debug_out])
        llm_btn.click(
            fn=_llm_with_preview,
            inputs=[text_preview],
            outputs=[
                year, month, total,
                confidence, suggested_sum, diff_vs_total,
                evidence, notes, proposal_df,
                phone_pick, charges_phone_pick,
                proposal_state,
                mappings_state, mappings_table,
                status,
                llm_input_preview,
                debug,
            ],
        )

        gr.on(
            triggers=[demo.load, current_plan_id.change],
            fn=_load_member_choices,
            inputs=[current_plan_id],
            outputs=[member_pick],
        )
        gr.on(
            triggers=[demo.load, current_plan_id.change],
            fn=_load_member_choices,
            inputs=[current_plan_id],
            outputs=[owner_pick],
        )
        gr.on(
            triggers=[demo.load, current_role.change, current_member_id.change, current_plan_id.change],
            fn=_write_access_update,
            inputs=[current_role, current_member_id, current_plan_id],
            outputs=[write_denied_banner],
        )

        map_btn.click(fn=_add_mapping, inputs=[phone_pick, member_pick, mappings_state], outputs=[mappings_state, mappings_table])
        save_map_btn.click(fn=_save_mapping_to_db, inputs=[phone_pick, member_pick, mappings_state], outputs=[status, mappings_state, mappings_table])
        clear_map_btn.click(fn=_clear_mappings, inputs=[], outputs=[mappings_state, mappings_table])

        charges_phone_pick.change(fn=_charges_for_phone, inputs=[charges_phone_pick, proposal_state], outputs=[charges_table, charges_evidence])

        proposal_df.change(fn=_calc_sum_diff, inputs=[total, proposal_df], outputs=[suggested_sum, diff_vs_total])
        total.change(fn=_calc_sum_diff, inputs=[total, proposal_df], outputs=[suggested_sum, diff_vs_total])

        validate_btn.click(
            fn=lambda y, m, t, df, do_owner, owner: _validate_before_upsert(y, m, t, df, do_owner, owner)[1],
            inputs=[year, month, total, proposal_df, compute_owner, owner_pick],
            outputs=[validation_box],
        )

        approve_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Approving..."),
            inputs=None,
            outputs=[approve_btn],
        ).then(
            fn=_approve_upsert,
            inputs=[year, month, total, proposal_df, mappings_state, owner_pick, compute_owner, current_role, current_member_id, current_plan_id],
            outputs=[status],
        ).then(
            fn=lambda: gr.update(interactive=True, value="Approve & upsert to DB"),
            inputs=None,
            outputs=[approve_btn],
        )

        # -------------------------
        # v2 (beta) wiring
        # -------------------------
        use_v2_checkbox.change(
            fn=lambda checked: gr.update(visible=checked),
            inputs=[use_v2_checkbox],
            outputs=[v2_group],
        )

        v2_poll_outputs = [
            v2_status, v2_timer, v2_proposal_state,
            v2_diff_accordion, v2_diff_md, v2_diff_facts_table, v2_diff_table,
            v2_review_accordion, v2_year, v2_month, v2_total, v2_notes, v2_facts_table,
            v2_reconcile_banner, v2_alloc_table,
            v2_unresolved_group, v2_unresolved_table, v2_unresolved_identifier_pick,
            v2_alloc_prev_state,
        ]

        v2_enqueue_outputs = [v2_status, v2_job_id_state] + v2_poll_outputs[1:]

        v2_enqueue_btn.click(
            fn=_v2_enqueue,
            inputs=[v2_pdf, current_member_id, current_plan_id],
            outputs=v2_enqueue_outputs,
        )

        # Manual, on-demand escape hatch for the auto-polling Timer above -
        # calls the exact same _v2_poll used by the timer, so it's always
        # available as a reliable way to (re)load a job's current state on
        # demand rather than depending solely on the Timer's automatic ticks.
        v2_check_status_btn.click(
            fn=_v2_poll,
            inputs=[v2_job_id_state, current_plan_id],
            outputs=v2_poll_outputs,
        )

        # concurrency_limit=1 on a dedicated lane forces every tick's full
        # chain to complete strictly in the order it fired. Without this, a
        # slow-to-render "still PROCESSING" response from an earlier tick
        # can arrive AFTER a later tick's "DONE" response and clobber it -
        # observed in testing as the diff/review section flashing visible
        # then disappearing again a moment later.
        v2_timer.tick(
            fn=_v2_poll,
            inputs=[v2_job_id_state, current_plan_id],
            outputs=v2_poll_outputs,
            concurrency_limit=1,
            concurrency_id="v2_poll_chain",
        ).then(
            fn=_v2_refresh_recent,
            inputs=[current_plan_id],
            outputs=[v2_recent_table],
            concurrency_limit=1,
            concurrency_id="v2_poll_chain",
        ).then(
            fn=_v2_inspector_choices,
            inputs=[current_plan_id],
            outputs=[v2_inspect_job_pick],
            concurrency_limit=1,
            concurrency_id="v2_poll_chain",
        )

        gr.on(
            triggers=[demo.load, current_plan_id.change, v2_recent_refresh_btn.click],
            fn=_v2_refresh_recent,
            inputs=[current_plan_id],
            outputs=[v2_recent_table],
        )

        gr.on(
            triggers=[demo.load, current_plan_id.change],
            fn=_load_member_choices,
            inputs=[current_plan_id],
            outputs=[v2_unresolved_member_pick],
        )

        # Live "calculator" check - recomputes the moment you edit an Amount
        # cell or the invoice total itself, independent of the last poll's
        # snapshot, so a mismatch is visible before you ever click Approve.
        # Deliberately `.input()`, not `.change()` - `.input()` only fires on
        # a real user edit, not when polling re-renders the table with fresh
        # data, so it can never race with / overwrite the richer poll-time
        # banner (which also knows about the "no owner set" edge case).
        # .input() (not .change()) so this only fires on an actual user
        # edit, never on the programmatic table updates from polling/
        # approving/equal-split below - see _v2_recalc_alloc's docstring
        # for why the previous-snapshot diff needs that guarantee.
        v2_alloc_table.input(
            fn=_v2_recalc_alloc,
            inputs=[v2_alloc_table, v2_alloc_prev_state, v2_total],
            outputs=[v2_alloc_table, v2_alloc_prev_state, v2_reconcile_banner],
        )
        v2_total.input(
            fn=_v2_refresh_percent_on_total_change,
            inputs=[v2_alloc_table, v2_total],
            outputs=[v2_alloc_table, v2_alloc_prev_state, v2_reconcile_banner],
        )
        v2_equal_split_btn.click(
            fn=_v2_equal_split,
            inputs=[v2_alloc_table, v2_total, current_plan_id],
            outputs=[v2_alloc_table, v2_alloc_prev_state, v2_reconcile_banner],
        )

        # Saves straight to member_identifiers, then immediately re-polls so
        # the just-saved mapping is reflected in the amounts/facts tables
        # right away (build_member_allocation_view always re-applies
        # deterministic matching fresh) - no separate "mappings" state to
        # keep in sync anymore.
        v2_unresolved_link_btn.click(
            fn=_v2_save_identifier_to_db,
            inputs=[v2_unresolved_identifier_pick, v2_unresolved_member_pick, current_plan_id],
            outputs=[v2_unresolved_status],
        ).then(
            fn=_v2_poll,
            inputs=[v2_job_id_state, current_plan_id],
            outputs=v2_poll_outputs,
        )

        v2_approve_btn.click(
            fn=lambda: gr.update(interactive=False, value="⏳ Approving..."),
            inputs=None,
            outputs=[v2_approve_btn],
        ).then(
            fn=_v2_approve,
            inputs=[
                v2_year, v2_month, v2_total, v2_alloc_table,
                current_role, current_member_id, current_plan_id,
                v2_job_id_state,
            ],
            outputs=[v2_status],
        ).then(
            # Locks the job into its read-only "already approved" view right
            # away - without this, Section B stayed editable/re-approvable
            # after a successful approve until the next unrelated refresh.
            fn=_v2_poll_keep_status,
            inputs=[v2_job_id_state, current_plan_id],
            outputs=v2_poll_outputs[1:],
        ).then(
            fn=lambda: gr.update(interactive=True, value="✅ Approve & create invoice (beta)"),
            inputs=None,
            outputs=[v2_approve_btn],
        )

        # -------------------------
        # "Inspect a job" wiring (owner-only, read-only)
        # -------------------------
        def _inspector_visibility_update(role, member_id, plan_id):
            with SessionLocal() as db:
                allowed = authz.can_manage_plan(db, role, member_id, plan_id)
            return gr.update(visible=allowed)

        gr.on(
            triggers=[demo.load, current_role.change, current_member_id.change, current_plan_id.change],
            fn=_inspector_visibility_update,
            inputs=[current_role, current_member_id, current_plan_id],
            outputs=[v2_inspector_accordion],
        )

        gr.on(
            triggers=[demo.load, current_plan_id.change, v2_inspect_refresh_btn.click],
            fn=_v2_inspector_choices,
            inputs=[current_plan_id],
            outputs=[v2_inspect_job_pick],
        )

        v2_load_review_btn.click(
            fn=_v2_load_into_review,
            inputs=[v2_inspect_job_pick, current_plan_id],
            outputs=[v2_load_review_status, v2_job_id_state] + v2_poll_outputs,
        )

        _v2_inspect_outputs = [
            v2_inspect_summary, v2_inspect_cleaned, v2_inspect_chunks, v2_inspect_precedent,
            v2_inspect_roster, v2_inspect_prompt, v2_inspect_raw, v2_inspect_proposal_table,
            v2_inspect_diff_accordion, v2_inspect_diff_table,
        ]
        # Selecting a different job in the dropdown refreshes immediately -
        # previously only the "Inspect" button did, so picking a new job
        # without also remembering to click it left the previous job's data
        # on screen looking like stale/wrong data for the newly-picked job.
        gr.on(
            triggers=[v2_inspect_btn.click, v2_inspect_job_pick.change],
            fn=_v2_inspect,
            inputs=[v2_inspect_job_pick],
            outputs=_v2_inspect_outputs,
        )

    return