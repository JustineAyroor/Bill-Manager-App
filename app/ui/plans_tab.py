from __future__ import annotations

import pandas as pd
import gradio as gr
from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Member, Invoice
from app.services import crud
from app.services import plans as plans_service
from app.services import authz
from app.services.accounting import all_plans_totals


def _role(role) -> str:
    return (role or "").strip().upper()


def _accessible_plans(db, role, member_id):
    role = _role(role)
    if role == "OWNER":
        return plans_service.list_plans(db)
    if role == "MEMBER" and member_id:
        return plans_service.get_member_plans(db, int(member_id))
    return []


def _accessible_plan_choice_list(role, member_id) -> list[str]:
    with SessionLocal() as db:
        return [f"{p.id} | {p.name}" for p in _accessible_plans(db, role, member_id)]


def _plans_df(role=None, member_id=None) -> pd.DataFrame:
    role = _role(role)
    with SessionLocal() as db:
        rows = []
        for plan in _accessible_plans(db, role, member_id):
            owner = plans_service.get_plan_owner(db, plan.id)
            member_count = len(plans_service.get_plan_members(db, plan.id))
            invoice_count = db.execute(
                select(Invoice.id).where(Invoice.plan_id == plan.id)
            ).scalars().all()
            you_manage = role == "OWNER" or (
                member_id is not None and plan.owner_member_id == int(member_id)
            )
            rows.append({
                "id": plan.id,
                "name": plan.name,
                "carrier_type": plan.carrier_type or "",
                "owner": owner.name if owner else "(not set)",
                "you_manage": "YES" if you_manage else "view only",
                "members": member_count,
                "invoices": len(invoice_count),
            })
    return pd.DataFrame(rows)


def _all_plans_totals_df() -> pd.DataFrame:
    with SessionLocal() as db:
        rows = all_plans_totals(db)
    return pd.DataFrame(rows)


def _member_choice_list() -> list[str]:
    with SessionLocal() as db:
        members = crud.list_members(db)
    return [f"{m.id} | {m.name}" for m in members]


def _parse_id(choice: str | None) -> int | None:
    if not choice:
        return None
    try:
        return int(str(choice).split("|", 1)[0].strip())
    except Exception:
        return None


def _plan_members_df(plan_choice: str | None) -> pd.DataFrame:
    plan_id = plans_service.parse_plan_choice(plan_choice)
    if not plan_id:
        return pd.DataFrame(columns=["member_id", "member"])
    with SessionLocal() as db:
        members = plans_service.get_plan_members(db, plan_id)
    return pd.DataFrame([{"member_id": m.id, "member": m.name} for m in members])


def _plan_member_choices(plan_choice: str | None):
    plan_id = plans_service.parse_plan_choice(plan_choice)
    if not plan_id:
        return gr.update(choices=[], value=None)
    with SessionLocal() as db:
        members = plans_service.get_plan_members(db, plan_id)
    choices = [f"{m.id} | {m.name}" for m in members]
    return gr.update(choices=choices, value=None)


def _active_plan_dropdown_update(role, member_id, current_choice=None):
    with SessionLocal() as db:
        choices = authz.accessible_plan_choices(db, role, member_id)
    value = current_choice if current_choice in choices else authz.default_plan_choice(choices, role)
    return gr.update(choices=choices, value=value)


def _refresh_all(role, member_id, active_plan_choice=None):
    plan_choices = _accessible_plan_choice_list(role, member_id)
    member_choices = _member_choice_list()
    return (
        _plans_df(role, member_id),
        _all_plans_totals_df(),
        gr.update(choices=plan_choices),    # manage_plan_pick
        gr.update(choices=member_choices),  # new_plan_owner
        gr.update(choices=member_choices),  # manage_owner_pick
        gr.update(choices=member_choices),  # add_member_pick
        _active_plan_dropdown_update(role, member_id, active_plan_choice),  # global active plan selector
    )


def _create_plan(role, member_id, active_plan_choice, name, carrier_type, owner_choice):
    role = _role(role)
    name = (name or "").strip()
    if not name:
        return "❌ Plan name is required.", *_refresh_all(role, member_id, active_plan_choice)

    if role == "MEMBER":
        # Members always become the owner of a plan they create.
        if not member_id:
            return "❌ Your login isn't linked to a member record.", *_refresh_all(role, member_id, active_plan_choice)
        owner_id = int(member_id)
    else:
        owner_id = _parse_id(owner_choice)

    with SessionLocal() as db:
        try:
            plan = plans_service.create_plan(db, name=name, carrier_type=carrier_type, owner_member_id=owner_id)
            plan_id, plan_name = plan.id, plan.name
            db.commit()
        except ValueError as exc:
            db.rollback()
            return f"❌ {exc}", *_refresh_all(role, member_id, active_plan_choice)

    return f"✅ Created plan '{plan_name}' (id={plan_id}).", *_refresh_all(role, member_id, active_plan_choice)


def _add_member_to_plan(role, member_id, active_plan_choice, plan_choice, member_choice, new_member_name):
    plan_id = plans_service.parse_plan_choice(plan_choice)
    if not plan_id:
        return "❌ Pick a plan first.", *_refresh_all(role, member_id, active_plan_choice)

    with SessionLocal() as db:
        if not authz.can_manage_plan(db, role, member_id, plan_id):
            return "❌ You can only manage plans you own.", *_refresh_all(role, member_id, active_plan_choice)

        chosen_member_id = _parse_id(member_choice)
        new_member_name = (new_member_name or "").strip()
        if not chosen_member_id and new_member_name:
            creator_id = int(member_id) if _role(role) == "MEMBER" and member_id else None
            m = crud.get_or_create_member(db, new_member_name, created_by_member_id=creator_id)
            chosen_member_id = m.id
        if not chosen_member_id:
            return "❌ Pick an existing member or type a new member name.", *_refresh_all(role, member_id, active_plan_choice)
        plans_service.add_member_to_plan(db, plan_id, chosen_member_id)
        db.commit()

    return "✅ Member added to plan.", *_refresh_all(role, member_id, active_plan_choice)


def _remove_member_from_plan(role, member_id, active_plan_choice, plan_choice, member_choice):
    if not authz.can_delete_plan_member(role):
        return "❌ Only the application owner can remove a member from a plan.", *_refresh_all(role, member_id, active_plan_choice)

    plan_id = plans_service.parse_plan_choice(plan_choice)
    chosen_member_id = _parse_id(member_choice)
    if not plan_id or not chosen_member_id:
        return "❌ Pick both a plan and a member.", *_refresh_all(role, member_id, active_plan_choice)

    with SessionLocal() as db:
        plans_service.remove_member_from_plan(db, plan_id, chosen_member_id)
        db.commit()

    return "✅ Member removed from plan.", *_refresh_all(role, member_id, active_plan_choice)


def _set_plan_owner(role, member_id, active_plan_choice, plan_choice, owner_choice):
    if _role(role) != "OWNER":
        return "❌ Only the application owner can transfer plan ownership.", *_refresh_all(role, member_id, active_plan_choice)

    plan_id = plans_service.parse_plan_choice(plan_choice)
    owner_id = _parse_id(owner_choice)
    if not plan_id:
        return "❌ Pick a plan first.", *_refresh_all(role, member_id, active_plan_choice)

    with SessionLocal() as db:
        try:
            plans_service.set_plan_owner(db, plan_id, owner_id)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return f"❌ {exc}", *_refresh_all(role, member_id, active_plan_choice)

    return "✅ Plan owner updated.", *_refresh_all(role, member_id, active_plan_choice)


def ui_plans(demo, current_role, current_member_id, active_plan_pick):
    def _owner_section_visibility(role):
        return gr.update(visible=_role(role) == "OWNER")

    with gr.Column():
        gr.Markdown(
            "## Plans\n"
            "Create and manage billing plans (e.g. a second mobile plan with different members). "
            "MEMBER users manage the plans they own; the app owner can see every plan. "
            "See `docs/decisions/2026-07-04-roadmap/04-multi-plan-schema.md` for background."
        )

        gr.Markdown("### Your plans")
        plans_table = gr.Dataframe(value=pd.DataFrame(), interactive=False)
        refresh_btn = gr.Button("🔄 Refresh")

        with gr.Column(visible=False) as owner_analytics_section:
            gr.Markdown(
                "### Cross-plan spend analytics (OWNER view)\n"
                "_The \"All plans (combined)\" row is most accurate when the same person "
                "(\"Justine\") is the owner across every plan. Outbound bill payments that "
                "aren't linked to a specific invoice can also only be attributed to the "
                "combined total, not to an individual plan._"
            )
            totals_table = gr.Dataframe(value=pd.DataFrame(), interactive=False)

        gr.Markdown("### Create a new plan")
        with gr.Row():
            new_plan_name = gr.Textbox(label="Plan name", placeholder="e.g. Verizon - Roommates")
            new_plan_carrier = gr.Textbox(label="Carrier type (optional)", placeholder="e.g. Verizon")
            new_plan_owner = gr.Dropdown(label="Owner (optional)", choices=[], value=None, visible=False)
        create_plan_btn = gr.Button("➕ Create plan", variant="primary")
        create_plan_status = gr.Textbox(label="Status", interactive=False)
        gr.Markdown("_As a MEMBER, you automatically become the owner of any plan you create._")

        gr.Markdown("### Manage plan membership")
        gr.Markdown(
            "_Adding members only works for plans you manage. Removing a member from a plan and "
            "transferring ownership are restricted to the application owner._"
        )
        with gr.Row():
            manage_plan_pick = gr.Dropdown(label="Plan", choices=[], value=None)
            with gr.Column(visible=False) as owner_transfer_section:
                manage_owner_pick = gr.Dropdown(label="Set owner (OWNER only)", choices=[], value=None)
                set_owner_btn = gr.Button("Set owner")

        plan_members_table = gr.Dataframe(value=pd.DataFrame(), interactive=False)

        with gr.Row():
            add_member_pick = gr.Dropdown(label="Add existing member", choices=[], value=None)
            add_new_member_name = gr.Textbox(label="...or add a brand-new member by name")
            add_member_btn = gr.Button("➕ Add to plan")

        with gr.Row(visible=False) as remove_member_section:
            remove_member_pick = gr.Dropdown(label="Remove member from plan (OWNER only)", choices=[], value=None)
            remove_member_btn = gr.Button("➖ Remove from plan")

        membership_status = gr.Textbox(label="Membership status", interactive=False)

        refresh_outputs = [
            plans_table,
            totals_table,
            manage_plan_pick,
            new_plan_owner,
            manage_owner_pick,
            add_member_pick,
            active_plan_pick,
        ]
        # remove_member_pick and members table need plan-scoped refresh separately.

        refresh_btn.click(
            fn=_refresh_all,
            inputs=[current_role, current_member_id, active_plan_pick],
            outputs=refresh_outputs,
        )
        create_plan_btn.click(
            fn=_create_plan,
            inputs=[current_role, current_member_id, active_plan_pick, new_plan_name, new_plan_carrier, new_plan_owner],
            outputs=[create_plan_status, *refresh_outputs],
        ).then(fn=lambda: ("", "", None), inputs=[], outputs=[new_plan_name, new_plan_carrier, new_plan_owner])

        manage_plan_pick.change(fn=_plan_members_df, inputs=[manage_plan_pick], outputs=[plan_members_table])
        manage_plan_pick.change(fn=_plan_member_choices, inputs=[manage_plan_pick], outputs=[remove_member_pick])

        add_member_btn.click(
            fn=_add_member_to_plan,
            inputs=[current_role, current_member_id, active_plan_pick, manage_plan_pick, add_member_pick, add_new_member_name],
            outputs=[membership_status, *refresh_outputs],
        ).then(
            fn=_plan_members_df, inputs=[manage_plan_pick], outputs=[plan_members_table]
        ).then(
            fn=_plan_member_choices, inputs=[manage_plan_pick], outputs=[remove_member_pick]
        )

        remove_member_btn.click(
            fn=_remove_member_from_plan,
            inputs=[current_role, current_member_id, active_plan_pick, manage_plan_pick, remove_member_pick],
            outputs=[membership_status, *refresh_outputs],
        ).then(
            fn=_plan_members_df, inputs=[manage_plan_pick], outputs=[plan_members_table]
        ).then(
            fn=_plan_member_choices, inputs=[manage_plan_pick], outputs=[remove_member_pick]
        )

        set_owner_btn.click(
            fn=_set_plan_owner,
            inputs=[current_role, current_member_id, active_plan_pick, manage_plan_pick, manage_owner_pick],
            outputs=[membership_status, *refresh_outputs],
        )

        gr.on(
            triggers=[demo.load],
            fn=_refresh_all,
            inputs=[current_role, current_member_id, active_plan_pick],
            outputs=refresh_outputs,
        )
        gr.on(
            triggers=[demo.load],
            fn=_owner_section_visibility,
            inputs=[current_role],
            outputs=[owner_analytics_section],
        )
        current_role.change(fn=_owner_section_visibility, inputs=[current_role], outputs=[owner_analytics_section])
        gr.on(
            triggers=[demo.load],
            fn=_owner_section_visibility,
            inputs=[current_role],
            outputs=[owner_transfer_section],
        )
        current_role.change(fn=_owner_section_visibility, inputs=[current_role], outputs=[owner_transfer_section])
        gr.on(
            triggers=[demo.load],
            fn=_owner_section_visibility,
            inputs=[current_role],
            outputs=[remove_member_section],
        )
        current_role.change(fn=_owner_section_visibility, inputs=[current_role], outputs=[remove_member_section])
        gr.on(
            triggers=[demo.load],
            fn=lambda role: gr.update(visible=_role(role) == "OWNER"),
            inputs=[current_role],
            outputs=[new_plan_owner],
        )
        current_role.change(
            fn=lambda role: gr.update(visible=_role(role) == "OWNER"),
            inputs=[current_role],
            outputs=[new_plan_owner],
        )
        current_member_id.change(
            fn=_refresh_all,
            inputs=[current_role, current_member_id, active_plan_pick],
            outputs=refresh_outputs,
        )

    return
