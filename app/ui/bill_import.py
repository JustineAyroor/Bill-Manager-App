from __future__ import annotations

import re
import traceback
from datetime import date

import pandas as pd
import gradio as gr
from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Member, Invoice, Allocation
from app.services.pdf_extract import extract_pdf_text
from app.services.llm_invoice_extract import extract_bill_proposal, MONTHS
from app.services.bill_text_filter import filter_text_for_llm


LAST4_RE = re.compile(r"last4:(\d{4})", re.IGNORECASE)


def _member_choice_list():
    with SessionLocal() as db:
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


def ui_bill_import(demo):
    with gr.Column():
        gr.Markdown(
            """
# 🧾 Bill Import (LLM-assisted)

**Flow:** 1) Extract → 2) LLM proposal → 3) Edit allocations → 4) Map phone → member → 5) Validate → 6) Approve
"""
        )

        proposal_state = gr.State(None)   # full proposal dict for charge viewer
        mappings_state = gr.State({})     # {phone_key: member_id}

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

        def _load_member_choices():
            choices = _member_choice_list()
            return gr.update(choices=choices, value=(choices[0] if choices else None))

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

        def _approve_upsert(y, m, tot, df, mappings: dict, owner_choice, do_owner: bool):
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

            with SessionLocal() as db:
                inv = db.execute(select(Invoice).where(Invoice.year == y, Invoice.month == m)).scalars().first()
                if inv is None:
                    inv = Invoice(year=y, month=m, total_amount=tot)
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

        gr.on(triggers=[demo.load], fn=_load_member_choices, inputs=[], outputs=[member_pick])
        gr.on(triggers=[demo.load], fn=_load_member_choices, inputs=[], outputs=[owner_pick])

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
            fn=_approve_upsert,
            inputs=[year, month, total, proposal_df, mappings_state, owner_pick, compute_owner],
            outputs=[status],
        )

    return