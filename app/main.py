import os
import asyncio
import gradio as gr
from app.ui.screens import ui_dashboard, ui_members, ui_invoices, ui_payments, ui_reminders, ui_applications
from app.ui.bill_import import ui_bill_import
from app.auth.service import authenticate_user, request_password_reset, reset_password_with_code
from app.services.account_email_templates import build_password_reset_email
from app.services.email_service import send_email

_BROWSER_STATE_KEY = "tmobile_bill_manager_user"
_BROWSER_STATE_SECRET = os.environ.get(
    "TM_BILL_BROWSER_STATE_SECRET",
    "dev-only-change-for-production",
)


def _empty_session():
    return {"logged_in": False, "email": "", "role": "", "member_id": None}

def restore_session(session_data):
    if not isinstance(session_data, dict):
        session_data = _empty_session()

    logged_in = bool(session_data.get("logged_in"))
    email = (session_data.get("email") or "").strip()
    role = (session_data.get("role") or "").strip().upper()
    member_id = session_data.get("member_id")

    if logged_in and email:
        return (
            session_data,
            f"Logged in as {email} ({role or 'UNKNOWN'})",
            gr.update(visible=False),
            gr.update(visible=True),
            "",
            "",
            role,
            member_id,
        )

    return (
        _empty_session(),
        "",
        gr.update(visible=True),
        gr.update(visible=False),
        "",
        "",
        "",
        None,
    )

def login_user(email, password):
    user = authenticate_user(email, password)
    if not user:
        return (
            gr.skip(),
            "Invalid credentials.",
            gr.update(visible=True),
            gr.update(visible=False),
            gr.skip(),
            "",
            "",
            None,
        )

    session_data = {
        "logged_in": True,
        "email": user.email,
        "role": (user.role or "").upper(),
        "member_id": user.member_id,
    }

    return (
        session_data,
        f"Logged in as {user.email} ({(user.role or '').upper()})",
        gr.update(visible=False),
        gr.update(visible=True),
        "",
        "",
        (user.role or "").upper(),
        user.member_id,
    )


def send_password_reset_email(email):
    result = request_password_reset(email)
    if not result.ok or not result.user_email or not result.reset_code:
        return result.message

    subject, text_body, html_body = build_password_reset_email(
        result.user_email,
        result.reset_code,
        result.expires_minutes,
    )
    try:
        asyncio.run(send_email(result.user_email, subject, text_body, html_body))
        return f"Reset code sent to {result.user_email}."
    except Exception as exc:
        return f"Reset code created, but email could not be sent: {exc}"


def complete_password_reset(email, reset_code, new_password, confirm_password):
    result = reset_password_with_code(email, reset_code, new_password, confirm_password)
    if result == "Password reset complete.":
        return result, "", "", "", ""
    return result, email, reset_code, new_password, confirm_password

def logout_user():
    return (
        _empty_session(),
        "Logged out.",
        gr.update(visible=True),
        gr.update(visible=False),
        "",
        "",
        "",
        None,
    )

def _refresh_tabs_if_logged_in(session_data):
    if isinstance(session_data, dict) and session_data.get("logged_in"):
        return gr.update(selected=0)
    return gr.skip()


def apply_role_visibility(role):
    role = (role or "").strip().upper()

    is_owner = role == "OWNER"
    is_member = role == "MEMBER"

    return (
        gr.update(visible=is_owner or is_member),  # dashboard_panel
        gr.update(visible=is_owner or is_member),  # members_panel
        gr.update(visible=is_owner or is_member),  # invoices_panel
        gr.update(visible=is_owner or is_member),  # payments_panel
        gr.update(visible=is_owner or is_member),  # reminders_panel
        gr.update(visible=is_owner or is_member),  # applications_panel
        gr.update(visible=is_owner),               # bill_import_panel
    )


def build_app():
    with gr.Blocks(title="T-Mobile Bill Manager") as demo:
        gr.Markdown("# T-Mobile Bill Manager (Local MVP)")

        current_user = gr.BrowserState(
            _empty_session(),
            storage_key=_BROWSER_STATE_KEY,
            secret=_BROWSER_STATE_SECRET,
        )
        current_member_id = gr.State(None)
        current_role = gr.State("")

        with gr.Column(visible=True) as login_panel:
            gr.Markdown("## Login")
            email = gr.Textbox(label="Email")
            password = gr.Textbox(label="Password", type="password")
            login_btn = gr.Button("Login")
            login_status = gr.Textbox(label="Status", interactive=False)
            gr.Markdown("### Forgot password")
            forgot_email = gr.Textbox(label="Account email")
            forgot_btn = gr.Button("Email reset code")
            forgot_status = gr.Textbox(label="Reset email status", interactive=False)
            reset_email = gr.Textbox(label="Reset email")
            reset_code = gr.Textbox(label="Reset code")
            reset_new_password = gr.Textbox(label="New password", type="password")
            reset_confirm_password = gr.Textbox(label="Confirm new password", type="password")
            reset_btn = gr.Button("Reset password")
            reset_status = gr.Textbox(label="Reset status", interactive=False)

        with gr.Column(visible=False) as app_panel:
            logout_btn = gr.Button("Logout")

            with gr.Tabs() as main_tabs:
                with gr.Tab("Dashboard"):
                    with gr.Column() as dashboard_panel:
                        ui_dashboard(demo,current_role, current_member_id)

                with gr.Tab("Members"):
                    with gr.Column() as members_panel:
                        ui_members(demo, current_role, current_member_id, current_user, login_status, login_panel, app_panel)

                with gr.Tab("Invoices & Allocations"):
                    with gr.Column() as invoices_panel:
                        ui_invoices(demo, current_role, current_member_id)

                with gr.Tab("Payments"):
                    with gr.Column() as payments_panel:
                        ui_payments(demo, current_role, current_member_id)

                with gr.Tab("Reminders"):
                    with gr.Column() as reminders_panel:
                        ui_reminders(current_role, current_member_id)

                with gr.Tab("Applications"):
                    with gr.Column() as applications_panel:
                        ui_applications(demo,current_role, current_member_id)

                with gr.Tab("Bill Import (LLM)"):
                    with gr.Column() as bill_import_panel:
                        ui_bill_import(demo)

        login_btn.click(
            fn=login_user,
            inputs=[email, password],
            outputs=[current_user, login_status, login_panel, app_panel, email, password, current_role,current_member_id],
        ).then(
            fn=_refresh_tabs_if_logged_in,
            inputs=[current_user],
            outputs=[main_tabs],
        ).then(
            fn=apply_role_visibility,
            inputs=[current_role],
            outputs=[
                dashboard_panel,
                members_panel,
                invoices_panel,
                payments_panel,
                reminders_panel,
                applications_panel,
                bill_import_panel,
            ],
        )

        forgot_btn.click(
            fn=send_password_reset_email,
            inputs=[forgot_email],
            outputs=[forgot_status],
        )

        reset_btn.click(
            fn=complete_password_reset,
            inputs=[reset_email, reset_code, reset_new_password, reset_confirm_password],
            outputs=[reset_status, reset_email, reset_code, reset_new_password, reset_confirm_password],
        )

        logout_btn.click(
            fn=logout_user,
            inputs=[],
            outputs=[current_user, login_status, login_panel, app_panel, email, password, current_role,current_member_id],
        ).then(
            fn=apply_role_visibility,
            inputs=[current_role],
            outputs=[
                dashboard_panel,
                members_panel,
                invoices_panel,
                payments_panel,
                reminders_panel,
                applications_panel,
                bill_import_panel,
            ],
        )

        demo.load(
            fn=restore_session,
            inputs=[current_user],
            outputs=[current_user, login_status, login_panel, app_panel, email, password, current_role,current_member_id],
        ).then(
            fn=_refresh_tabs_if_logged_in,
            inputs=[current_user],
            outputs=[main_tabs],
        ).then(
            fn=apply_role_visibility,
            inputs=[current_role],
            outputs=[
                dashboard_panel,
                members_panel,
                invoices_panel,
                payments_panel,
                reminders_panel,
                applications_panel,
                bill_import_panel,
            ],
        )

    return demo


if __name__ == "__main__":
    app = build_app()
    app.launch(
        server_name=os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0"),
        server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
    )
