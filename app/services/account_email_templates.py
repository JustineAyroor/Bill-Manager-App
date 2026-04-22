from __future__ import annotations

from app.core.config import APP_BASE_URL

def build_member_invite_email(login_email: str, temp_password: str) -> tuple[str, str, str]:
    subject = "Your T-Mobile Bill Manager account"
    login_url = APP_BASE_URL or "http://localhost:7860"
    text_body = (
        f"Hi,\n\n"
        f"An account was created for you in the T-Mobile Bill Manager.\n\n"
        f"Open the app: {login_url}\n"
        f"Login email: {login_email}\n"
        f"Temporary password: {temp_password}\n\n"
        f"Please log in and change your password.\n"
    )
    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;line-height:1.5;">
      <div style="max-width:560px;margin:0 auto;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
        <div style="background:#111827;color:#fff;padding:18px 20px;">
          <div style="font-size:18px;font-weight:700;">T-Mobile Bill Manager</div>
          <div style="font-size:12px;opacity:0.8;margin-top:4px;">Account setup</div>
        </div>
        <div style="padding:20px;">
          <p style="margin:0 0 12px 0;">Hi,</p>
          <p style="margin:0 0 16px 0;">An account was created for you in the T-Mobile Bill Manager.</p>

          <p style="margin:0 0 16px 0;">
            <a href="{login_url}" style="display:inline-block;background:#111827;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;">
              Open the app
            </a>
          </p>

          <div style="border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px;background:#f9fafb;">
            <div style="font-size:12px;color:#6b7280;">Login email</div>
            <div style="font-size:15px;font-weight:600;color:#111;margin-bottom:10px;">{login_email}</div>
            <div style="font-size:12px;color:#6b7280;">Temporary password</div>
            <div style="font-size:15px;font-weight:600;color:#111;">{temp_password}</div>
          </div>

          <p style="margin:16px 0 0 0;">Please log in and change your password after your first sign-in.</p>
          <p style="margin:12px 0 0 0;color:#6b7280;font-size:12px;">If the button does not work, open: {login_url}</p>
        </div>
      </div>
    </div>
    """
    return subject, text_body, html_body


def build_password_reset_email(login_email: str, reset_code: str, expires_minutes: int) -> tuple[str, str, str]:
    subject = "Reset your T-Mobile Bill Manager password"
    login_url = APP_BASE_URL or "http://localhost:7860"
    text_body = (
        f"Hi,\n\n"
        f"We received a request to reset the password for {login_email}.\n\n"
        f"Open the app: {login_url}\n"
        f"Reset code: {reset_code}\n"
        f"This code expires in {expires_minutes} minutes.\n\n"
        f"If you did not request this, you can ignore this email.\n"
    )
    html_body = f"""
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#111;line-height:1.5;">
      <div style="max-width:560px;margin:0 auto;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;">
        <div style="background:#0f766e;color:#fff;padding:18px 20px;">
          <div style="font-size:18px;font-weight:700;">Password reset</div>
          <div style="font-size:12px;opacity:0.8;margin-top:4px;">T-Mobile Bill Manager</div>
        </div>
        <div style="padding:20px;">
          <p style="margin:0 0 12px 0;">Hi,</p>
          <p style="margin:0 0 16px 0;">We received a request to reset the password for <b>{login_email}</b>.</p>

          <p style="margin:0 0 16px 0;">
            <a href="{login_url}" style="display:inline-block;background:#0f766e;color:#fff;text-decoration:none;padding:10px 14px;border-radius:8px;font-weight:600;">
              Open the app
            </a>
          </p>

          <div style="display:inline-block;border:1px solid #99f6e4;border-radius:10px;padding:12px 16px;background:#f0fdfa;">
            <div style="font-size:12px;color:#0f766e;">Reset code</div>
            <div style="font-size:24px;font-weight:800;letter-spacing:1px;color:#134e4a;">{reset_code}</div>
          </div>

          <p style="margin:16px 0 0 0;">This code expires in {expires_minutes} minutes.</p>
          <p style="margin:12px 0 0 0;">Open the app and use this code to finish resetting your password.</p>
          <p style="margin:12px 0 0 0;color:#6b7280;font-size:12px;">If the button does not work, open: {login_url}</p>
          <p style="margin:12px 0 0 0;color:#6b7280;">If you did not request this, you can ignore this email.</p>
        </div>
      </div>
    </div>
    """
    return subject, text_body, html_body
