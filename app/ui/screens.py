from __future__ import annotations

from datetime import date
import asyncio
import pandas as pd
import gradio as gr
import matplotlib.pyplot as plt
import plotly.graph_objects as go
from app.db.database import SessionLocal
from app.services import crud
from app.services.accounting import member_balances, plan_totals
from app.services.excel_io import export_excel
from sqlalchemy import select,case,func
from app.db.models import Payment, Member,Invoice,Allocation, ReminderLog,PaymentApplication
from app.services.recompute_owner import recompute_owner_allocation
from app.services.reminder_service import compute_reminder_candidates, build_reminder_email, get_eligible_reminder_candidates,ReminderPolicy
from app.services.email_service import send_email
from app.services.payment_apply import auto_apply_payment_fifo
import re
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _valid_email(s: str) -> bool:
    return bool(EMAIL_RE.match((s or "").strip()))

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sept","Oct","Nov","Dec"]

def _preview_reminders_df():

    policy = ReminderPolicy(owner_name="Justine", min_balance=10.0, cooldown_days=7)

    with SessionLocal() as db:
        candidates = compute_reminder_candidates(db, policy)

    rows = []
    for c in candidates:
        rows.append({
            "member": c.member,
            "email": c.email,
            "balance": round(float(c.balance), 2),
            "eligible": "YES" if c.eligible else "NO",
            "reason": c.reason,
            "last_reminder_at": c.last_reminder_at.isoformat(timespec="seconds") if c.last_reminder_at else "",
        })

    return pd.DataFrame(rows)

def _send_reminders_now():

    policy = ReminderPolicy(owner_name="Justine", min_balance=10.0, cooldown_days=7)

    with SessionLocal() as db:
        candidates = get_eligible_reminder_candidates(db, policy)

    if not candidates:
        return "No eligible reminders to send (threshold/cooldown/email rules).", _preview_reminders_df()

    async def _send_all():
        results = []
        for c in candidates:
            subject, text_body, html_body = build_reminder_email(c.member, c.balance)
            try:
                await send_email(c.email, subject, text_body, html_body)
                results.append((c, subject, html_body, 1, None, "QUEUED"))
            except Exception as e:
                results.append((c, subject, html_body, 0, str(e), "FAILED"))
        return results

    results = asyncio.run(_send_all())

    sent = 0
    failures = []

    with SessionLocal() as db:
        for c, subject, body, success, err, status in results:
            if success == 1:
                sent += 1
            else:
                failures.append(f"{c.member} ({c.email}): {err}")

            db.add(ReminderLog(
                member_id=c.member_id,
                email=c.email,
                amount=float(c.balance),
                subject=subject,
                body=body,          # store html (or store both if you add another column)
                success=int(success),
                error=err,
                status=status,
            ))
        db.commit()

    if failures:
        msg = f"Sent {sent} reminders. Failures:\n" + "\n".join(failures[:10])
    else:
        msg = f"✅ Queued {sent} reminder emails."

    return msg, _preview_reminders_df()

# def _send_reminders_now():
#     # Step 1: read candidates (sync DB)
#     with SessionLocal() as db:
#         candidates = get_reminder_candidates(db)

#     if not candidates:
#         return "No reminders to send (either no one owes money or emails are missing).", _preview_reminders_df()

#     # Step 2: send emails (async), collect results
#     async def _send_all():
#         results = []
#         for c in candidates:
#             subject, body = build_reminder_email(c.member, c.balance)

#             # ✅ NEW: fail fast on bad format
#             if not _valid_email(c.email):
#                 results.append({
#                     "member_id": c.member_id,
#                     "email": c.email,
#                     "amount": c.balance,
#                     "subject": subject,
#                     "body": body,
#                     "success": 0,
#                     "error": "Invalid email format",
#                     "member": c.member,
#                 })
#                 continue

#             try:
#                 await send_email(c.email, subject, body)
#                 results.append({
#                     "member_id": c.member_id,
#                     "email": c.email,
#                     "amount": c.balance,
#                     "subject": subject,
#                     "body": body,
#                     "success": 1,
#                     "error": None,
#                     "member": c.member,
#                 })
#             except Exception as e:
#                 results.append({
#                     "member_id": c.member_id,
#                     "email": c.email,
#                     "amount": c.balance,
#                     "subject": subject,
#                     "body": body,
#                     "success": 0,
#                     "error": str(e),
#                     "member": c.member,
#                 })
#         return results

#     results = asyncio.run(_send_all())

#     # Step 3: write logs (sync DB)
#     sent = 0
#     failures = []
#     with SessionLocal() as db:
#         for r in results:
#             if r["success"] == 1:
#                 sent += 1
#             else:
#                 failures.append(f'{r["member"]} ({r["email"]}): {r["error"]}')

#             db.add(ReminderLog(
#                 member_id=r["member_id"],
#                 email=r["email"],
#                 amount=r["amount"],
#                 subject=r["subject"],
#                 body=r["body"],
#                 success=r["success"],
#                 error=r["error"],
#             ))
#         db.commit()

#     if failures:
#         msg = f"Sent {sent} reminders. Failures:\n" + "\n".join(failures[:10])
#     else:
#         msg = f"✅ Sent {sent} reminders successfully."

#     return msg, _preview_reminders_df()

def _money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return "$0.00"


def _plan_totals_html():
    with SessionLocal() as db:
        t = plan_totals(db, owner_name="Justine")

    outstanding = t.get("plan_due_outstanding", 0.0)
    recovered = t.get("plan_recovered", 0.0)
    owner_outbound = t.get("owner_total_outbound", 0.0)

    # If you want a % recovered indicator:
    denom = (outstanding + recovered) if (outstanding + recovered) > 0 else 0.0
    pct = (recovered / denom * 100.0) if denom else 0.0

    return f"""
    <div style="display:flex; flex-direction:column; gap:12px;">

      <div style="display:flex; align-items:center; justify-content:space-between;">
        <div>
          <div style="font-size:18px; font-weight:700; line-height:1.2;">Plan Overview</div>
          <div style="font-size:12px; opacity:0.75;">Totals are based on allocations vs applied payments (FIFO applications).</div>
        </div>
        <div style="font-size:12px; opacity:0.7; padding:6px 10px; border:1px solid rgba(255,255,255,0.15); border-radius:999px;">
          Recovery: <b>{pct:.1f}%</b>
        </div>
      </div>

      <div style="display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px;">
        { _kpi_card("Outstanding (excl. owner)", _money(outstanding), "What members still owe right now") }
        { _kpi_card("Recovered (excl. owner)", _money(recovered), "Amount applied to invoices so far") }
        { _kpi_card("Owner Outbound Paid", _money(owner_outbound), "Total bill payments you recorded as OUTBOUND") }
      </div>

      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        { _pill("Tip", "Use Reconcile after editing payments to rebuild applications accurately.") }
        { _pill("Note", "‘Success’ email logs mean queued, not guaranteed delivered.") }
      </div>

    </div>
    """


def _kpi_card(title, value, subtitle):
    return f"""
    <div style="
      border:1px solid rgba(255,255,255,0.12);
      border-radius:14px;
      padding:14px;
      background:rgba(255,255,255,0.03);
      ">
      <div style="font-size:12px; opacity:0.75; margin-bottom:6px;">{title}</div>
      <div style="font-size:24px; font-weight:800; letter-spacing:0.2px;">{value}</div>
      <div style="font-size:11px; opacity:0.65; margin-top:6px;">{subtitle}</div>
    </div>
    """


def _pill(label, text):
    return f"""
    <div style="
      border:1px solid rgba(255,255,255,0.12);
      border-radius:999px;
      padding:8px 10px;
      font-size:12px;
      opacity:0.9;
      background:rgba(255,255,255,0.02);
      ">
      <b style="margin-right:6px;">{label}:</b>{text}
    </div>
    """
# def _plan_totals_html():

#     with SessionLocal() as db:
#         t = plan_totals(db)

#     return f"""
#     <div style="display:flex; gap:20px; flex-wrap:wrap">

#         <div style="background:#1f2937;padding:20px;border-radius:12px;color:white;min-width:200px">
#             <div style="font-size:14px;opacity:0.7">Total Due</div>
#             <div style="font-size:28px;font-weight:bold">${t['total_due']:.2f}</div>
#         </div>

#         <div style="background:#065f46;padding:20px;border-radius:12px;color:white;min-width:200px">
#             <div style="font-size:14px;opacity:0.7">Recovered</div>
#             <div style="font-size:28px;font-weight:bold">${t['recovered']:.2f}</div>
#         </div>

#         <div style="background:#7c2d12;padding:20px;border-radius:12px;color:white;min-width:200px">
#             <div style="font-size:14px;opacity:0.7">Remaining</div>
#             <div style="font-size:28px;font-weight:bold">${t['remaining']:.2f}</div>
#         </div>

#         <div style="background:#1e3a8a;padding:20px;border-radius:12px;color:white;min-width:200px">
#             <div style="font-size:14px;opacity:0.7">Total Bill Paid</div>
#             <div style="font-size:28px;font-weight:bold">${t['total_bill_paid']:.2f}</div>
#         </div>

#     </div>
#     """

def _df_balances():
    with SessionLocal() as db:
        data = member_balances(db)
    return pd.DataFrame(data)


def ui_dashboard(demo):
    with gr.Column():
        gr.Markdown("## Dashboard — Who owes what")

        # --- Top row: Chart + Actions ---
        with gr.Row():
            # LEFT: Chart + Totals
            with gr.Column(scale=2):
                gr.Markdown("### Balances chart")

                with gr.Row():
                    show_only_owed = gr.Checkbox(label="Show only members who still owe", value=False)
                    sort_by = gr.Dropdown(["Name", "Most Due", "Most Paid"], value="Name", label="Sort")

                chart = gr.Plot(value=_balances_chart_plotly(False, "Name"))

                with gr.Row():
                    refresh_chart = gr.Button("🔄 Refresh chart")
                    refresh_chart.click(
                        fn=_balances_chart_plotly,
                        inputs=[show_only_owed, sort_by],
                        outputs=[chart],
                    )

                # Auto-update chart on control changes
                show_only_owed.change(fn=_balances_chart_plotly, inputs=[show_only_owed, sort_by], outputs=[chart])
                sort_by.change(fn=_balances_chart_plotly, inputs=[show_only_owed, sort_by], outputs=[chart])

                # Plan totals (HTML KPI cards)
                totals_html = gr.HTML(value=_plan_totals_html())

            # RIGHT: Export + Reminders
            with gr.Column(scale=1):
                gr.Markdown("### Export")
                export_btn = gr.Button("⬇️ Download current DB as Excel")
                export_file = gr.File(label="Exported file")
                export_btn.click(fn=_export_click, inputs=[], outputs=[export_file])

                gr.Markdown("### Email reminders")

                with gr.Row():
                    preview_btn = gr.Button("👀 Preview")
                    send_btn = gr.Button("📨 Send now")

                send_status = gr.Textbox(label="Status", interactive=False)
                preview_table = gr.Dataframe(value=_preview_reminders_df(), interactive=False)

                preview_btn.click(fn=_preview_reminders_df, inputs=[], outputs=[preview_table])
                send_btn.click(fn=_send_reminders_now, inputs=[], outputs=[send_status, preview_table])

        # --- Bottom: Table + Refresh controls ---
        gr.Markdown("### Balances table")
        balances = gr.Dataframe(value=_df_balances(), interactive=False)

        with gr.Row():
            refresh_table = gr.Button("🔄 Refresh table")
            refresh_all = gr.Button("🔁 Refresh everything")

        refresh_table.click(fn=_df_balances, inputs=[], outputs=[balances])

        # Refresh everything: chart + table + totals + reminder preview
        refresh_all.click(fn=_balances_chart_plotly, inputs=[show_only_owed, sort_by], outputs=[chart])
        refresh_all.click(fn=_df_balances, inputs=[], outputs=[balances])
        refresh_all.click(fn=_plan_totals_html, inputs=[], outputs=[totals_html])
        refresh_all.click(fn=_preview_reminders_df, inputs=[], outputs=[preview_table])

        # Optional: initial load refresh (ensures the dashboard starts consistent)
        gr.on(
            triggers=[demo.load],
            fn=_balances_chart_plotly,
            inputs=[show_only_owed, sort_by],
            outputs=[chart],
        )
        gr.on(triggers=[demo.load], fn=_df_balances, inputs=[], outputs=[balances])
        gr.on(triggers=[demo.load], fn=_plan_totals_html, inputs=[], outputs=[totals_html])
        gr.on(triggers=[demo.load], fn=_preview_reminders_df, inputs=[], outputs=[preview_table])

    return

def _export_click():
    with SessionLocal() as db:
        path = export_excel(db)
    return path

def _balances_chart_plotly(show_only_owed: bool = False, sort_by: str = "Name"):
    df = _df_balances().copy()
    if df.empty:
        return go.Figure()

    # Clean + normalize
    df["member"] = df["member"].fillna("").astype(str)
    df = df[df["member"].str.strip() != ""]
    df = df[df["member"].str.lower() != "nan"]

    df["total_due"] = pd.to_numeric(df["total_due"], errors="coerce").fillna(0.0)
    df["total_paid"] = pd.to_numeric(df["total_paid"], errors="coerce").fillna(0.0)

    # Remaining due
    df["remaining"] = (df["total_due"] - df["total_paid"]).clip(lower=0.0)

    if show_only_owed:
        df = df[df["remaining"] > 0]

    if sort_by == "Most Due":
        df = df.sort_values("remaining", ascending=False)
    elif sort_by == "Most Paid":
        df = df.sort_values("total_paid", ascending=False)
    else:
        df = df.sort_values("member", ascending=True)

    members = df["member"].tolist()
    due = df["total_due"].tolist()
    paid = df["total_paid"].tolist()
    remaining = df["remaining"].tolist()

    fig = go.Figure()

    # Total Due (orange) — requested explicitly
    fig.add_trace(
        go.Bar(
            x=members,
            y=due,
            name="Total Due",
            marker=dict(color="orange"),
            hovertemplate="Member: %{x}<br>Total Due: %{y:.2f}<extra></extra>",
        )
    )

    # Total Paid overlays the due bar (default Plotly color, interactive legend)
    fig.add_trace(
        go.Bar(
            x=members,
            y=paid,
            name="Total Paid",
            hovertemplate="Member: %{x}<br>Total Paid: %{y:.2f}<extra></extra>",
        )
    )

    # Add remaining in hover (without adding another bar)
    # fig.update_traces(
    #     hovertemplate=None
    # )

    fig.update_layout(
        barmode="overlay",
        title="Paid overlays Total Due (orange remainder = still owed)",
        xaxis_title="Member",
        yaxis_title="Amount",
        hovermode="x unified",
        bargap=0.25,
        legend_title_text="Toggle series",
    )

    # Custom hover for both traces showing remaining
    # (Plotly uses per-trace hovertemplate; we’ll include remaining there)
    fig.data[0].hovertemplate = "Member: %{x}<br>Total Due: %{y:.2f}<br>Remaining: %{customdata:.2f}<extra></extra>"
    fig.data[0].customdata = remaining
    fig.data[1].hovertemplate = "Member: %{x}<br>Total Paid: %{y:.2f}<br>Remaining: %{customdata:.2f}<extra></extra>"
    fig.data[1].customdata = remaining

    return fig
  
def ui_members(demo):
    with gr.Column():
        gr.Markdown("## Members")

        member_pick = gr.Dropdown(label="Select member to edit", choices=[], value=None)
        refresh = gr.Button("🔄 Refresh list")

        name = gr.Textbox(label="Name")
        email = gr.Textbox(label="Email (optional)")
        phone = gr.Textbox(label="Phone (optional)")
        is_active = gr.Checkbox(label="Active", value=True)

        save_btn = gr.Button("Save (create or update)")
        out = gr.Textbox(label="Result", interactive=False)
        table = gr.Dataframe(value=_members_df(), interactive=False)

        refresh.click(fn=_refresh_members_screen, inputs=[], outputs=[member_pick, table])

        member_pick.change(fn=_load_member_details, inputs=[member_pick], outputs=[name, email, phone, is_active])

        save_btn.click(fn=_save_member_by_selection, inputs=[member_pick, name, email, phone, is_active], outputs=[out, table, member_pick])

        # initial list
        gr.on(triggers=[demo.load], fn=_load_members_dropdown, inputs=[], outputs=[member_pick])

    return

def _refresh_members_screen():
    return _load_members_dropdown(), gr.update(value=_members_df().copy())

def _load_members_dropdown():
    df = _members_df()
    # dropdown items like "12 | Vinay"
    choices = [f"{row['id']} | {row['name']}" for _, row in df.iterrows()]
    return gr.Dropdown(choices=choices)

def _load_member_details(member_pick):
    if not member_pick:
        return "", "", "", True
    member_id = int(str(member_pick).split("|")[0].strip())
    with SessionLocal() as db:
        m = db.get(Member, member_id)
        return m.name or "", m.email or "", m.phone or "", bool(m.is_active)

def _save_member_by_selection(member_pick, name, email, phone, is_active):
    if not name or not str(name).strip():
        return "❌ Name is required", _members_df(), _load_members_dropdown()

    with SessionLocal() as db:
        if member_pick:
            member_id = int(str(member_pick).split("|")[0].strip())
            m = db.get(Member, member_id)
            m.name = str(name).strip()
            m.email = (email or "").strip() or None
            m.phone = (phone or "").strip() or None
            m.is_active = 1 if is_active else 0
        else:
            crud.get_or_create_member(db, name=str(name).strip(), email=email or None, phone=phone or None)
        db.commit()
    df = _members_df().copy()
    return "✅ Saved", gr.update(value=df), _load_members_dropdown()

def _members_df():
    with SessionLocal() as db:
        members = crud.list_members(db)
    return pd.DataFrame([{"id": m.id, "name": m.name, "email": m.email or "", "phone": m.phone or ""} for m in members])


def _invoice_choice_label(inv: Invoice) -> str:
    return f"{inv.id} | {inv.year}-{inv.month} | total=${float(inv.total_amount or 0.0):.2f}"

def _load_invoice_and_member_choices():
    with SessionLocal() as db:
        invoices = db.execute(select(Invoice).order_by(Invoice.year.desc(), Invoice.month.desc(), Invoice.id.desc())).scalars().all()
        invoice_choices = [_invoice_choice_label(inv) for inv in invoices]

        members = crud.list_members(db)
        member_choices = [m.name for m in members if m.name and str(m.name).strip().lower() != "nan"]

    return gr.Dropdown(choices=invoice_choices), gr.Dropdown(choices=member_choices)

def _parse_invoice_id(invoice_pick: str | None) -> int | None:
    if not invoice_pick:
        return None
    return int(str(invoice_pick).split("|")[0].strip())

def _load_invoice_details(invoice_pick):
    invoice_id = _parse_invoice_id(invoice_pick)
    if not invoice_id:
        return date.today().year, MONTHS[date.today().month - 1], 0.0, _invoice_allocations_df(None)

    with SessionLocal() as db:
        inv = db.get(Invoice, invoice_id)
        return inv.year, inv.month, float(inv.total_amount or 0.0), _invoice_allocations_df(invoice_id)

def _invoice_allocations_df(invoice_id: int | None):
    if not invoice_id:
        return pd.DataFrame(columns=["member", "amount_due"])

    with SessionLocal() as db:
        rows = db.execute(
            select(Member.name, Allocation.amount_due)
            .join(Allocation, Allocation.member_id == Member.id)
            .where(Allocation.invoice_id == invoice_id)
            .order_by(Member.name)
        ).all()

    return pd.DataFrame([{"member": r[0], "amount_due": float(r[1])} for r in rows])

def _create_new_invoice(year, month, total):
    if not year or not month:
        return "❌ Year and month required", gr.update()

    with SessionLocal() as db:
        inv = crud.upsert_invoice(db, int(year), str(month), float(total or 0.0))
        # ensure owner alloc consistent
        recompute_owner_allocation(db, inv.id)
        db.commit()

        # refresh invoice choices
        invoices = db.execute(select(Invoice).order_by(Invoice.year.desc(), Invoice.month.desc(), Invoice.id.desc())).scalars().all()
        choices = [_invoice_choice_label(i) for i in invoices]
        selected = _invoice_choice_label(inv)

    return f"✅ Created/Found invoice {inv.year}-{inv.month} total=${inv.total_amount:.2f}", gr.Dropdown(choices=choices, value=selected)

def _save_invoice_changes(invoice_pick, year, month, total):
    invoice_id = _parse_invoice_id(invoice_pick)
    if not invoice_id:
        return "❌ Select an invoice first", gr.update(), _invoice_allocations_df(None)

    with SessionLocal() as db:
        inv = db.get(Invoice, invoice_id)
        inv.year = int(year)
        inv.month = str(month)
        inv.total_amount = float(total or 0.0)

        # ✅ Recompute owner allocation since total changed
        recompute_owner_allocation(db, invoice_id)

        db.commit()

        # refresh invoice choices and keep selection updated
        invoices = db.execute(select(Invoice).order_by(Invoice.year.desc(), Invoice.month.desc(), Invoice.id.desc())).scalars().all()
        choices = [_invoice_choice_label(i) for i in invoices]
        selected = _invoice_choice_label(inv)

    return f"✅ Saved invoice total=${inv.total_amount:.2f}", gr.Dropdown(choices=choices, value=selected), _invoice_allocations_df(invoice_id)

def _save_allocation_for_selected_invoice(invoice_pick, member_name, amount_due):
    invoice_id = _parse_invoice_id(invoice_pick)
    if not invoice_id:
        return "❌ Select an invoice first", _invoice_allocations_df(None)
    if not member_name:
        return "❌ Pick a member", _invoice_allocations_df(invoice_id)

    with SessionLocal() as db:
        m = crud.get_or_create_member(db, member_name.strip())
        crud.upsert_allocation(db, invoice_id=invoice_id, member_id=m.id, amount_due=float(amount_due or 0.0))

        # ✅ keep owner consistent
        recompute_owner_allocation(db, invoice_id)

        db.commit()

    return f"✅ Allocation saved for {member_name}", _invoice_allocations_df(invoice_id)

def _recompute_owner_for_selected_invoice(invoice_pick):
    invoice_id = _parse_invoice_id(invoice_pick)
    if not invoice_id:
        return "❌ Select an invoice first", _invoice_allocations_df(None)

    with SessionLocal() as db:
        recompute_owner_allocation(db, invoice_id)
        db.commit()

    return "✅ Recomputed owner allocation", _invoice_allocations_df(invoice_id)


def ui_invoices(demo):
    with gr.Column():
        gr.Markdown("## Invoices (Create / Edit)")

        with gr.Row():
            invoice_pick = gr.Dropdown(label="Select invoice", choices=[], value=None)
            refresh_btn = gr.Button("🔄 Refresh")

        # Invoice editor
        with gr.Row():
            inv_year = gr.Number(label="Year", precision=0)
            inv_month = gr.Dropdown(MONTHS, label="Month")
            inv_total = gr.Number(label="Invoice total ($)", value=0)

        with gr.Row():
            create_new_btn = gr.Button("➕ Create new")
            save_invoice_btn = gr.Button("💾 Save invoice changes")

        alloc_table = gr.Dataframe(value=_invoice_allocations_df(None), interactive=False)
            
        invoice_msg = gr.Textbox(label="Invoice status", interactive=False)

        gr.Markdown("### Allocations for selected invoice")

        member_pick = gr.Dropdown(label="Member", choices=[], value=None)
        alloc_amount = gr.Number(label="Amount due", value=0)

        with gr.Row():
            add_alloc_btn = gr.Button("Save allocation (upsert)")
            recompute_btn = gr.Button("Recompute owner allocation")

        alloc_msg = gr.Textbox(label="Allocation status", interactive=False)

        # --- Wiring ---
        refresh_btn.click(fn=_load_invoice_and_member_choices, inputs=[], outputs=[invoice_pick, member_pick])

        # Selecting invoice loads details + allocations
        invoice_pick.change(
            fn=_load_invoice_details,
            inputs=[invoice_pick],
            outputs=[inv_year, inv_month, inv_total, alloc_table],
        )

        create_new_btn.click(
            fn=_create_new_invoice,
            inputs=[inv_year, inv_month, inv_total],
            outputs=[invoice_msg, invoice_pick],
        )

        save_invoice_btn.click(
            fn=_save_invoice_changes,
            inputs=[invoice_pick, inv_year, inv_month, inv_total],
            outputs=[invoice_msg, invoice_pick, alloc_table],
        )

        add_alloc_btn.click(
            fn=_save_allocation_for_selected_invoice,
            inputs=[invoice_pick, member_pick, alloc_amount],
            outputs=[alloc_msg, alloc_table],
        )

        recompute_btn.click(
            fn=_recompute_owner_for_selected_invoice,
            inputs=[invoice_pick],
            outputs=[alloc_msg, alloc_table],
        )

        # initial load
        gr.on(
            triggers=[demo.load],
            fn=_load_invoice_and_member_choices,
            inputs=[],
            outputs=[invoice_pick, member_pick],
        )

    return

def _parse_id(pick):
    if not pick:
        return None
    return int(str(pick).split("|")[0].strip())

def _toggle_member_visibility(direction):
    if direction == "OUTBOUND":
        return gr.update(visible=False, value=None)
    return gr.update(visible=True)

def _member_choice_list():
    with SessionLocal() as db:
        rows = db.execute(select(Member.id, Member.name).order_by(Member.name)).all()
    return [f"{r.id} | {r.name}" for r in rows if r.name and str(r.name).strip().lower() != "nan"]

def _invoice_choice_list():
    with SessionLocal() as db:
        rows = db.execute(
            select(Invoice.id, Invoice.year, Invoice.month)
            .order_by(Invoice.year.desc(), Invoice.month.desc(), Invoice.id.desc())
        ).all()
    return [f"{r.id} | {r.year}-{r.month}" for r in rows]

def _payment_choice_list(limit=200):
    with SessionLocal() as db:
        rows = db.execute(
            select(Payment.id, Payment.date, Payment.direction, Payment.amount, Member.name)
            .outerjoin(Member, Member.id == Payment.member_id)
            .order_by(Payment.date.desc(), Payment.id.desc())
            .limit(int(limit))
        ).all()

    out = []
    for r in rows:
        out.append(f"{r.id} | {r.date.isoformat()} | {r.direction} | ${float(r.amount or 0.0):.2f} | {r.name or ''}")
    return out

def _payment_pick_update():
    return gr.update(choices=_payment_choice_list(limit=200), value=None)

def _payments_page_df(page=1, page_size=30, direction_filter="All", member_filter=None, invoice_filter=None, search_text=""):
    page = int(page or 1)
    page = 1 if page < 1 else page
    page_size = int(page_size or 30)
    offset = (page - 1) * page_size

    member_id = _parse_id(member_filter)
    invoice_id = _parse_id(invoice_filter)

    with SessionLocal() as db:
        q = (
            select(
                Payment.id,
                Payment.date,
                Payment.direction,
                Payment.amount,
                Payment.description,
                Member.name.label("member"),
                Invoice.id.label("inv_id"),
                Invoice.year.label("inv_year"),
                Invoice.month.label("inv_month"),
            )
            .select_from(Payment)
            .outerjoin(Member, Member.id == Payment.member_id)
            .outerjoin(Invoice, Invoice.id == Payment.invoice_id)
        )

        if direction_filter in ("INBOUND", "OUTBOUND"):
            q = q.where(Payment.direction == direction_filter)
        if member_id is not None:
            q = q.where(Payment.member_id == member_id)
        if invoice_id is not None:
            q = q.where(Payment.invoice_id == invoice_id)
        if search_text and str(search_text).strip():
            q = q.where(Payment.description.ilike(f"%{str(search_text).strip()}%"))

        q = q.order_by(Payment.date.desc(), Payment.id.desc()).offset(offset).limit(page_size)
        rows = db.execute(q).all()

    data = []
    for r in rows:
        inv_label = f"{r.inv_id} | {r.inv_year}-{r.inv_month}" if r.inv_id else ""
        data.append({
            "id": int(r.id),
            "date": r.date.isoformat(),
            "direction": r.direction,
            "member": r.member or "",
            "invoice": inv_label,
            "amount": float(r.amount or 0.0),
            "description": r.description or "",
        })

    return pd.DataFrame(data)

def _add_payment_v4(when, direction, member_pick, invoice_pick, amount, description):
    try:
        dt = date.fromisoformat(str(when))
    except Exception:
        return "❌ Date must be YYYY-MM-DD", pd.DataFrame()

    if direction not in ("INBOUND", "OUTBOUND"):
        return "❌ Direction must be INBOUND or OUTBOUND", pd.DataFrame()

    member_id = _parse_id(member_pick)
    invoice_id = _parse_id(invoice_pick)

    if direction == "INBOUND" and member_id is None:
        return "❌ Select a member for INBOUND payments", pd.DataFrame()

    if direction == "OUTBOUND":
        member_id = None

    amt = float(amount or 0.0)
    if amt <= 0:
        return "❌ Amount must be > 0", pd.DataFrame()

    with SessionLocal() as db:
        p = crud.add_payment(
            db,
            when=dt,
            amount=amt,
            direction=direction,
            description=(description or "").strip() or None,
            member_id=member_id,
            invoice_id=invoice_id,  # optional
        )

        preview_df = pd.DataFrame()
        if direction == "INBOUND":
            rows, remainder = auto_apply_payment_fifo(db, p.id)
            preview_df = pd.DataFrame([{
                "invoice": r.invoice_label,
                "due": round(r.due, 2),
                "prev_paid": round(r.previously_applied, 2),
                "paid_now": round(r.applied_now, 2),
                "remaining": round(r.remaining_after, 2),
            } for r in rows])

            if remainder > 0:
                # show credit row
                preview_df = pd.concat([preview_df, pd.DataFrame([{
                    "invoice": "UNAPPLIED CREDIT",
                    "due": "",
                    "prev_paid": "",
                    "paid_now": round(remainder, 2),
                    "remaining": "",
                }])], ignore_index=True)

        db.commit()

    return "✅ Payment saved", preview_df

def _load_payment_by_pick(payment_pick):
    pid = _parse_id(payment_pick)
    if not pid:
        return None, "Pick a payment and click **Load**.", None, "", "INBOUND", None, None, 0.0, ""

    with SessionLocal() as db:
        p = db.get(Payment, pid)
        if not p:
            return None, "Payment not found.", None, "", "INBOUND", None, None, 0.0, ""

        member_val = None
        invoice_val = None

        if p.member_id:
            m = db.get(Member, p.member_id)
            if m:
                member_val = f"{m.id} | {m.name}"

        if p.invoice_id:
            inv = db.get(Invoice, p.invoice_id)
            if inv:
                invoice_val = f"{inv.id} | {inv.year}-{inv.month}"

        return (
            pid,
            f"Loaded payment **{pid}** for editing.",
            pid,
            p.date.isoformat(),
            p.direction,
            member_val,
            invoice_val,
            float(p.amount or 0.0),
            p.description or "",
        )

def _save_payment_edits_v4(payment_id, when, direction, member_pick, invoice_pick, amount, description):
    if not payment_id:
        return "❌ No payment selected"

    try:
        dt = date.fromisoformat(str(when))
    except Exception:
        return "❌ Date must be YYYY-MM-DD"

    if direction not in ("INBOUND", "OUTBOUND"):
        return "❌ Direction must be INBOUND or OUTBOUND"

    member_id = _parse_id(member_pick)
    invoice_id = _parse_id(invoice_pick)

    if direction == "INBOUND" and member_id is None:
        return "❌ INBOUND requires a member"

    if direction == "OUTBOUND":
        member_id = None

    amt = float(amount or 0.0)
    if amt <= 0:
        return "❌ Amount must be > 0"

    with SessionLocal() as db:
        crud.update_payment(
            db,
            payment_id=int(payment_id),
            when=dt,
            amount=amt,
            direction=direction,
            description=(description or "").strip() or None,
            member_id=member_id,
            invoice_id=invoice_id,
        )
        db.commit()

    return "✅ Payment updated"


def _delete_payment_v4(payment_id):
    if not payment_id:
        return ("❌ No payment selected", None, "Pick a payment and click **Load**.", None, "", "INBOUND", None, None, 0.0, "")

    with SessionLocal() as db:
        crud.delete_payment(db, int(payment_id))
        db.commit()

    return ("✅ Payment deleted", None, "Pick a payment and click **Load**.", None, "", "INBOUND", None, None, 0.0, "")

def _reconcile_member(member_pick):
    mid = _parse_id(member_pick)
    if not mid:
        return "❌ Select a member", pd.DataFrame()

    from app.services.payment_apply import reconcile_member_fifo

    with SessionLocal() as db:
        res = reconcile_member_fifo(db, mid)
        db.commit()

    df = pd.DataFrame([res])
    msg = (
        f"✅ Reconciled {res['member']} | inbound=${res['inbound_total']} | "
        f"applied=${res['applied_total']} | credit=${res['unapplied_credit']}"
    )
    return msg, df


def _reconcile_all():
    from app.services.payment_apply import reconcile_all_members_fifo

    with SessionLocal() as db:
        results = reconcile_all_members_fifo(db)
        db.commit()

    df = pd.DataFrame(results)
    return f"✅ Reconciled {len(results)} members", df
    
def ui_payments(demo):
    with gr.Column():
        gr.Markdown("## Payments")

        # ---------------- Dropdown loaders ----------------
        def _load_dropdowns():
            member_choices = _member_choice_list()
            invoice_choices = _invoice_choice_list()
            payment_choices = _payment_choice_list(limit=200)
            return (
                gr.update(choices=member_choices),
                gr.update(choices=invoice_choices),
                gr.update(choices=member_choices),
                gr.update(choices=invoice_choices),
                gr.update(choices=payment_choices, value=None),
            )

        # ---------------- Add payment ----------------
        with gr.Accordion("➕ Record a payment", open=True):
            with gr.Row():
                add_when = gr.Textbox(label="Date (YYYY-MM-DD)", value=date.today().isoformat())
                add_direction = gr.Dropdown(["INBOUND", "OUTBOUND"], value="INBOUND", label="Direction")

            with gr.Row():
                add_member = gr.Dropdown(label="Member (required for INBOUND)", choices=[], value=None)
                add_invoice = gr.Dropdown(label="Link to invoice (optional)", choices=[], value=None)

            with gr.Row():
                add_amount = gr.Number(label="Amount", value=0)
                add_desc = gr.Textbox(label="Description", value="")

            add_btn = gr.Button("Save payment")
            add_status = gr.Textbox(label="Status", interactive=False)
            applied_preview = gr.Dataframe(label="Applied (FIFO preview)", interactive=False)
        add_direction.change(fn=_toggle_member_visibility, inputs=[add_direction], outputs=[add_member])

        gr.Markdown("### Reconcile (build applications from existing payments)")

        reconcile_member_pick = gr.Dropdown(label="Member to reconcile", choices=_member_choice_list(), value=None)
        reconcile_btn = gr.Button("Reconcile member (FIFO)")
        reconcile_all_btn = gr.Button("Reconcile ALL members (FIFO)")

        reconcile_status = gr.Textbox(label="Reconcile status", interactive=False)
        reconcile_table = gr.Dataframe(value=pd.DataFrame(), interactive=False)

        reconcile_btn.click(fn=_reconcile_member, inputs=[reconcile_member_pick], outputs=[reconcile_status, reconcile_table])
        reconcile_all_btn.click(fn=_reconcile_all, inputs=[], outputs=[reconcile_status, reconcile_table])
        # ---------------- Ledger controls ----------------
        gr.Markdown("### Payments ledger")

        with gr.Row():
            f_dir = gr.Dropdown(["All", "INBOUND", "OUTBOUND"], value="All", label="Direction filter")
            f_member = gr.Dropdown(label="Member filter", choices=[], value=None)
            f_invoice = gr.Dropdown(label="Invoice filter", choices=[], value=None)
            f_search = gr.Textbox(label="Search description", value="", placeholder="zelle / tmobile / jan ...")

        with gr.Row():
            page = gr.Number(label="Page", value=1, precision=0)
            page_size = gr.Dropdown([10, 20, 30, 50, 100], value=30, label="Page size")
            refresh = gr.Button("🔄 Refresh")

        payments_table = gr.Dataframe(
            value=_payments_page_df(1, 30, "All", None, None, ""),
            interactive=False
        )

        # ---------------- Edit/Delete ----------------
        gr.Markdown("### Edit / delete a payment")

        payment_pick = gr.Dropdown(label="Select a payment to edit", choices=[], value=None)
        load_btn = gr.Button("Load")

        selected_payment_id = gr.State(None)

        with gr.Accordion("✏️ Edit selected payment", open=True):
            edit_info = gr.Markdown("Pick a payment and click **Load**.")

            with gr.Row():
                edit_id = gr.Number(label="Payment ID", precision=0, interactive=False)
                edit_when = gr.Textbox(label="Date (YYYY-MM-DD)")
                edit_direction = gr.Dropdown(["INBOUND", "OUTBOUND"], label="Direction")

            with gr.Row():
                edit_member = gr.Dropdown(label="Member (required for INBOUND)", choices=[], value=None)
                edit_invoice = gr.Dropdown(label="Linked invoice (optional)", choices=[], value=None)

            with gr.Row():
                edit_amount = gr.Number(label="Amount")
                edit_desc = gr.Textbox(label="Description")

            with gr.Row():
                save_btn = gr.Button("💾 Save changes")
                delete_btn = gr.Button("🗑️ Delete payment")

            edit_status = gr.Textbox(label="Edit status", interactive=False)

        edit_direction.change(fn=_toggle_member_visibility, inputs=[edit_direction], outputs=[edit_member])

        # ---------------- Initial load ----------------
        gr.on(
            triggers=[demo.load],
            fn=_load_dropdowns,
            inputs=[],
            outputs=[add_member, add_invoice, f_member, f_invoice, payment_pick],
        )
        gr.on(
            triggers=[demo.load],
            fn=_load_dropdowns,
            inputs=[],
            outputs=[edit_member, edit_invoice, f_member, f_invoice, payment_pick],
        )

        # ---------------- Add payment handlers ----------------
        add_btn.click(
            fn=_add_payment_v4,
            inputs=[add_when, add_direction, add_member, add_invoice, add_amount, add_desc],
            outputs=[add_status,applied_preview],
        )

        # Refresh table + payment dropdown after adding
        add_btn.click(
            fn=_payments_page_df,
            inputs=[page, page_size, f_dir, f_member, f_invoice, f_search],
            outputs=[payments_table],
        )
        add_btn.click(
            fn=_payment_pick_update,
            inputs=[],
            outputs=[payment_pick],
        )

        # ---------------- Refresh handlers ----------------
        refresh.click(
            fn=_payments_page_df,
            inputs=[page, page_size, f_dir, f_member, f_invoice, f_search],
            outputs=[payments_table],
        )
        refresh.click(fn=_payment_pick_update, inputs=[], outputs=[payment_pick])

        # Auto refresh on filters/pagination
        for c in (f_dir, f_member, f_invoice, f_search, page, page_size):
            c.change(
                fn=_payments_page_df,
                inputs=[page, page_size, f_dir, f_member, f_invoice, f_search],
                outputs=[payments_table],
            )

        # ---------------- Load selected payment ----------------
        load_btn.click(
            fn=_load_payment_by_pick,
            inputs=[payment_pick],
            outputs=[selected_payment_id, edit_info, edit_id, edit_when, edit_direction, edit_member, edit_invoice, edit_amount, edit_desc],
        )

        # ---------------- Save edits ----------------
        save_btn.click(
            fn=_save_payment_edits_v4,
            inputs=[selected_payment_id, edit_when, edit_direction, edit_member, edit_invoice, edit_amount, edit_desc],
            outputs=[edit_status],
        )
        save_btn.click(
            fn=_payments_page_df,
            inputs=[page, page_size, f_dir, f_member, f_invoice, f_search],
            outputs=[payments_table],
        )
        save_btn.click(fn=_payment_pick_update, inputs=[], outputs=[payment_pick])

        # ---------------- Delete payment ----------------
        delete_btn.click(
            fn=_delete_payment_v4,
            inputs=[selected_payment_id],
            outputs=[edit_status, selected_payment_id, edit_info, edit_id, edit_when, edit_direction, edit_member, edit_invoice, edit_amount, edit_desc],
        )
        delete_btn.click(
            fn=_payments_page_df,
            inputs=[page, page_size, f_dir, f_member, f_invoice, f_search],
            outputs=[payments_table],
        )
        delete_btn.click(fn=_payment_pick_update, inputs=[], outputs=[payment_pick])

    return



def _reminder_logs_df(limit=50, member_filter=None, success_filter="All"):
    """
    success_filter: "All" | "Success" | "Failed"
    member_filter: "id | Name" or None
    """
    member_id = None
    if member_filter:
        try:
            member_id = int(str(member_filter).split("|")[0].strip())
        except Exception:
            member_id = None

    with SessionLocal() as db:
        q = (
            select(
                ReminderLog.created_at,
                Member.name.label("member"),
                ReminderLog.email,
                ReminderLog.amount,
                ReminderLog.success,
                ReminderLog.error,
                ReminderLog.subject,
            )
            .join(Member, Member.id == ReminderLog.member_id)
            .order_by(ReminderLog.created_at.desc())
            .limit(int(limit))
        )

        # Apply filters
        if member_id is not None:
            q = q.where(ReminderLog.member_id == member_id)

        if success_filter == "Success":
            q = q.where(ReminderLog.success == 1)
        elif success_filter == "Failed":
            q = q.where(ReminderLog.success == 0)

        rows = db.execute(q).all()

    df = pd.DataFrame([{
        "sent_at": (r.created_at.isoformat() if r.created_at else ""),
        "member": r.member,
        "email": r.email,
        "amount": float(r.amount or 0.0),
        "status": "SUCCESS" if int(r.success) == 1 else "FAILED",
        "error": r.error or "",
        "subject": r.subject or "",
    } for r in rows])

    return df


def _reminder_member_filter_choices():
    with SessionLocal() as db:
        rows = db.execute(select(Member.id, Member.name).order_by(Member.name)).all()
    return [f"{r.id} | {r.name}" for r in rows if r.name and str(r.name).strip().lower() != "nan"]

def ui_reminders():
    with gr.Column():
        gr.Markdown("## Reminder Logs")

        with gr.Row():
            member_filter = gr.Dropdown(
                label="Filter by member",
                choices=_reminder_member_filter_choices(),
                value=None,
                allow_custom_value=False,
            )
            success_filter = gr.Dropdown(
                label="Status",
                choices=["All", "Success", "Failed"],
                value="All",
            )
            limit = gr.Number(label="Show last N", value=50, precision=0)

        with gr.Row():
            refresh = gr.Button("🔄 Refresh")
            clear_filter = gr.Button("🧹 Clear filters")

        logs_table = gr.Dataframe(value=_reminder_logs_df(), interactive=False)

        # Refresh button
        refresh.click(
            fn=_reminder_logs_df,
            inputs=[limit, member_filter, success_filter],
            outputs=[logs_table],
        )

        # Filter changes auto-refresh
        member_filter.change(fn=_reminder_logs_df, inputs=[limit, member_filter, success_filter], outputs=[logs_table])
        success_filter.change(fn=_reminder_logs_df, inputs=[limit, member_filter, success_filter], outputs=[logs_table])
        limit.change(fn=_reminder_logs_df, inputs=[limit, member_filter, success_filter], outputs=[logs_table])

        def _clear():
            return None, "All", 50, _reminder_logs_df(50, None, "All")

        clear_filter.click(fn=_clear, inputs=[], outputs=[member_filter, success_filter, limit, logs_table])



MONTH_NUM = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
             "Jul": 7, "Aug": 8, "Sept": 9, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

def ui_applications(demo):
    with gr.Column():
        gr.Markdown("## Payment Applications (verify allocations)")

        member_pick = gr.Dropdown(label="Member", choices=[], value=None)
        refresh_members = gr.Button("🔄 Refresh members")

        credit_box = gr.Textbox(label="Unapplied credit", interactive=False)

        gr.Markdown("### By invoice (due vs applied vs remaining)")
        by_invoice = gr.Dataframe(value=pd.DataFrame(), interactive=False)

        gr.Markdown("### Raw application rows (splits)")
        app_rows = gr.Dataframe(value=pd.DataFrame(), interactive=False)

        refresh = gr.Button("🔄 Refresh tables")

        def _load_members():
            return gr.update(choices=_member_choice_list(), value=None)

        refresh_members.click(fn=_load_members, inputs=[], outputs=[member_pick])
        gr.on(triggers=[demo.load], fn=_load_members, inputs=[], outputs=[member_pick])

        def _refresh_all(mpick):
            return (
                _member_credit(mpick),
                _member_applications_by_invoice_df(mpick),
                _member_application_rows_df(mpick),
            )

        refresh.click(fn=_refresh_all, inputs=[member_pick], outputs=[credit_box, by_invoice, app_rows])
        member_pick.change(fn=_refresh_all, inputs=[member_pick], outputs=[credit_box, by_invoice, app_rows])

def _invoice_month_case():
    return case({k: v for k, v in MONTH_NUM.items()}, value=Invoice.month, else_=99)


def _member_credit(member_pick):
    mid = _parse_id(member_pick)
    if not mid:
        return "—"
    from app.services.payment_apply import member_unapplied_credit
    with SessionLocal() as db:
        credit = member_unapplied_credit(db, mid)
    return f"${credit:.2f}"

def _member_applications_by_invoice_df(member_pick):
    mid = _parse_id(member_pick)
    if not mid:
        return pd.DataFrame()

    with SessionLocal() as db:
        # 1) Due per invoice (from allocations only)
        due_sq = (
            select(
                Allocation.invoice_id.label("invoice_id"),
                func.coalesce(func.sum(Allocation.amount_due), 0.0).label("due"),
            )
            .where(Allocation.member_id == mid)
            .group_by(Allocation.invoice_id)
            .subquery()
        )

        # 2) Applied per invoice (from applications only)
        app_sq = (
            select(
                PaymentApplication.invoice_id.label("invoice_id"),
                func.coalesce(func.sum(PaymentApplication.amount_applied), 0.0).label("applied"),
            )
            .where(PaymentApplication.member_id == mid)
            .group_by(PaymentApplication.invoice_id)
            .subquery()
        )

        # 3) Join invoice + due + applied
        rows = db.execute(
            select(
                Invoice.year,
                Invoice.month,
                due_sq.c.due,
                func.coalesce(app_sq.c.applied, 0.0).label("applied"),
            )
            .join(due_sq, due_sq.c.invoice_id == Invoice.id)
            .outerjoin(app_sq, app_sq.c.invoice_id == Invoice.id)
            .order_by(Invoice.year.asc(), _invoice_month_case().asc())
        ).all()

    data = []
    running = 0.0
    for r in rows:
        due = float(r.due or 0.0)
        applied = float(r.applied or 0.0)
        net = due - applied              # can be negative if they overpaid that month
        running += net                   # cumulative balance (can go down with overpayments)

        data.append({
            "invoice": f"{r.year}-{r.month}",
            "due": round(due, 2),
            "applied": round(applied, 2),
            "net_due": round(net, 2),            # per-invoice delta
            "running_balance": round(running, 2) # cumulative outstanding as of this invoice
        })

    return pd.DataFrame(data)

def _member_application_rows_df(member_pick, limit=500):
    mid = _parse_id(member_pick)
    if not mid:
        return pd.DataFrame()

    with SessionLocal() as db:
        rows = db.execute(
            select(
                PaymentApplication.id,
                PaymentApplication.created_at,
                PaymentApplication.amount_applied,
                Payment.id.label("payment_id"),
                Payment.date.label("payment_date"),
                Invoice.year.label("inv_year"),
                Invoice.month.label("inv_month"),
            )
            .join(Payment, Payment.id == PaymentApplication.payment_id)
            .join(Invoice, Invoice.id == PaymentApplication.invoice_id)
            .where(PaymentApplication.member_id == mid)
            .order_by(Payment.date.asc(), Payment.id.asc(), PaymentApplication.id.asc())
            .limit(int(limit))
        ).all()

    data = []
    for r in rows:
        data.append({
            "app_id": int(r.id),
            "payment_id": int(r.payment_id),
            "payment_date": r.payment_date.isoformat() if r.payment_date else "",
            "invoice": f"{r.inv_year}-{r.inv_month}",
            "amount_applied": round(float(r.amount_applied or 0.0), 2),
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })
    return pd.DataFrame(data)