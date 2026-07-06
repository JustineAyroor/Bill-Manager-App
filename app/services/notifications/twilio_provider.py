from __future__ import annotations

from typing import Optional

from app.core.config import TWILIO_SMS_CONFIGURED, TWILIO_WHATSAPP_CONFIGURED
from app.services import twilio_service
from app.services.notifications.base import SendResult


class TwilioProvider:
    """
    Wraps Twilio SMS/WhatsApp sending behind the NotificationProvider
    interface. Kept fully intact and isolated so it can be re-enabled (or
    used as a template for a replacement SMS/WhatsApp provider) later without
    touching any UI or reminder-eligibility code - see
    docs/decisions/2026-07-04-roadmap/02-notifications-strategy.md.
    """

    name = "TWILIO"

    def supports(self, channel: str) -> bool:
        return (channel or "").strip().upper() in {"SMS", "WHATSAPP"}

    def is_configured(self, channel: str | None = None) -> bool:
        channel = (channel or "").strip().upper()
        if channel == "SMS":
            return TWILIO_SMS_CONFIGURED
        if channel == "WHATSAPP":
            return TWILIO_WHATSAPP_CONFIGURED
        return TWILIO_SMS_CONFIGURED or TWILIO_WHATSAPP_CONFIGURED

    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        subject: str | None,
        text_body: str,
        html_body: str | None = None,
    ) -> SendResult:
        channel = (channel or "").strip().upper()

        if not self.is_configured(channel):
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=None,
                provider=self.name,
                provider_message_id=None,
                provider_status="FAILED",
                success=False,
                error=(
                    f"Twilio is not configured for {channel}. Add the relevant TWILIO_* "
                    "keys to .env to re-enable this channel."
                ),
            )

        if channel == "SMS":
            return twilio_service.send_sms(recipient, text_body)
        if channel == "WHATSAPP":
            return twilio_service.send_whatsapp(recipient, text_body)

        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=None,
            provider=self.name,
            provider_message_id=None,
            provider_status="FAILED",
            success=False,
            error=f"TwilioProvider does not support channel: {channel}",
        )

    def fetch_status(self, message_id: str) -> Optional[dict]:
        if not (message_id or "").strip():
            return None
        if not self.is_configured():
            # Twilio credentials were removed/rejected after older messages
            # were sent - don't blow up the UI trying to poll them.
            return {
                "provider_status": "SYNC_SKIPPED",
                "error_code": None,
                "error": "Twilio is not configured; cannot sync delivery status.",
            }
        try:
            return twilio_service.fetch_message_status(message_id)
        except Exception as exc:
            return {"provider_status": "SYNC_FAILED", "error_code": None, "error": str(exc)}
