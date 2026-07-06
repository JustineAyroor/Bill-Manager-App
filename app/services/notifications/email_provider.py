from __future__ import annotations

from typing import Optional

from app.services.email_service import FROM_EMAIL, is_email_configured, send_email
from app.services.notifications.base import SendResult


class EmailProvider:
    """Wraps SMTP email sending behind the NotificationProvider interface."""

    name = "SMTP"

    def supports(self, channel: str) -> bool:
        return (channel or "").strip().upper() == "EMAIL"

    def is_configured(self, channel: str | None = None) -> bool:
        return is_email_configured()

    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        subject: str | None,
        text_body: str,
        html_body: str | None = None,
    ) -> SendResult:
        if not self.is_configured(channel):
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=FROM_EMAIL,
                provider=self.name,
                provider_message_id=None,
                provider_status="FAILED",
                success=False,
                error="Email is not configured. Add SMTP_* keys to .env to enable this channel.",
            )

        try:
            await send_email(recipient, subject or "", text_body, html_body or text_body)
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=FROM_EMAIL,
                provider=self.name,
                provider_message_id=None,
                provider_status="QUEUED",
                success=True,
            )
        except Exception as exc:
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=FROM_EMAIL,
                provider=self.name,
                provider_message_id=None,
                provider_status="FAILED",
                success=False,
                error=str(exc),
            )

    def fetch_status(self, message_id: str) -> Optional[dict]:
        # SMTP has no delivery-status API in this app today.
        return None
