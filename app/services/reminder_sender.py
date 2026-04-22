from __future__ import annotations

from dataclasses import dataclass

from app.services.email_service import FROM_EMAIL, send_email


@dataclass
class SendResult:
    channel: str
    recipient: str
    sender: str | None
    provider: str
    provider_message_id: str | None
    provider_status: str | None
    success: bool
    error: str | None = None
    error_code: str | None = None


async def send_reminder(
    *,
    channel: str,
    recipient: str,
    subject: str | None,
    text_body: str,
    html_body: str | None = None,
) -> SendResult:
    channel = (channel or "").strip().upper()

    if channel == "EMAIL":
        try:
            await send_email(recipient, subject or "", text_body, html_body or text_body)
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=FROM_EMAIL,
                provider="SMTP",
                provider_message_id=None,
                provider_status="QUEUED",
                success=True,
            )
        except Exception as exc:
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=FROM_EMAIL,
                provider="SMTP",
                provider_message_id=None,
                provider_status="FAILED",
                success=False,
                error=str(exc),
            )

    if channel in {"SMS", "WHATSAPP"}:
        from app.services.twilio_service import send_sms, send_whatsapp

        try:
            if channel == "SMS":
                return send_sms(recipient, text_body)
            return send_whatsapp(recipient, text_body)
        except Exception as exc:
            return SendResult(
                channel=channel,
                recipient=recipient,
                sender=None,
                provider="TWILIO",
                provider_message_id=None,
                provider_status="FAILED",
                success=False,
                error=str(exc),
            )

    return SendResult(
        channel=channel,
        recipient=recipient,
        sender=None,
        provider="UNKNOWN",
        provider_message_id=None,
        provider_status="FAILED",
        success=False,
        error=f"Unsupported reminder channel: {channel}",
    )
