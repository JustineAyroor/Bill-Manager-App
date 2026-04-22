from __future__ import annotations

import re

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.core.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_SMS_FROM,
    TWILIO_STATUS_CALLBACK_URL,
    TWILIO_WHATSAPP_FROM,
)
from app.services.reminder_sender import SendResult

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _validate_twilio_config(channel: str) -> str:
    missing = []
    if not TWILIO_ACCOUNT_SID:
        missing.append("TWILIO_ACCOUNT_SID")
    if not TWILIO_AUTH_TOKEN:
        missing.append("TWILIO_AUTH_TOKEN")

    if channel == "SMS":
        sender = TWILIO_SMS_FROM
        if not sender:
            missing.append("TWILIO_SMS_FROM")
    elif channel == "WHATSAPP":
        sender = TWILIO_WHATSAPP_FROM
        if not sender:
            missing.append("TWILIO_WHATSAPP_FROM")
    else:
        raise RuntimeError(f"Unsupported Twilio channel: {channel}")

    if missing:
        raise RuntimeError(f"Missing Twilio config keys in .env: {', '.join(missing)}")

    return sender


def _client() -> Client:
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _message_kwargs(to: str, sender: str, body: str) -> dict[str, str]:
    kwargs = {
        "to": to,
        "from_": sender,
        "body": body,
    }
    if TWILIO_STATUS_CALLBACK_URL:
        kwargs["status_callback"] = TWILIO_STATUS_CALLBACK_URL
    return kwargs


def _twilio_error_code(exc: Exception) -> str | None:
    return str(exc.code) if isinstance(exc, TwilioRestException) and exc.code else None


def _twilio_error_message(exc: Exception) -> str:
    message = ANSI_RE.sub("", str(exc)).strip()
    code = _twilio_error_code(exc)
    if code == "21660":
        message += (
            "\n\nCheck TWILIO_SMS_FROM/TWILIO_WHATSAPP_FROM: the sender must belong "
            "to the same Twilio account or subaccount as TWILIO_ACCOUNT_SID."
        )
    return message


def fetch_message_status(message_sid: str) -> dict[str, str | None]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Missing Twilio credentials for status sync.")

    sid = (message_sid or "").strip()
    if not sid:
        raise RuntimeError("Missing Twilio message SID.")

    try:
        message = _client().messages(sid).fetch()
        return {
            "provider_status": getattr(message, "status", None),
            "error_code": str(message.error_code) if getattr(message, "error_code", None) else None,
            "error": getattr(message, "error_message", None),
        }
    except Exception as exc:
        return {
            "provider_status": "SYNC_FAILED",
            "error_code": _twilio_error_code(exc),
            "error": _twilio_error_message(exc),
        }


def send_sms(to_phone: str, body: str) -> SendResult:
    channel = "SMS"
    sender = _validate_twilio_config(channel)
    recipient = (to_phone or "").strip()

    try:
        message = _client().messages.create(**_message_kwargs(recipient, sender, body))
        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=sender,
            provider="TWILIO",
            provider_message_id=message.sid,
            provider_status=message.status,
            success=True,
        )
    except Exception as exc:
        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=sender,
            provider="TWILIO",
            provider_message_id=None,
            provider_status="FAILED",
            success=False,
            error=_twilio_error_message(exc),
            error_code=_twilio_error_code(exc),
        )


def send_whatsapp(to_phone: str, body: str) -> SendResult:
    channel = "WHATSAPP"
    sender = _validate_twilio_config(channel)
    raw_recipient = (to_phone or "").strip()
    recipient = raw_recipient if raw_recipient.startswith("whatsapp:") else f"whatsapp:{raw_recipient}"
    whatsapp_sender = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"

    try:
        message = _client().messages.create(**_message_kwargs(recipient, whatsapp_sender, body))
        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=whatsapp_sender,
            provider="TWILIO",
            provider_message_id=message.sid,
            provider_status=message.status,
            success=True,
        )
    except Exception as exc:
        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=whatsapp_sender,
            provider="TWILIO",
            provider_message_id=None,
            provider_status="FAILED",
            success=False,
            error=_twilio_error_message(exc),
            error_code=_twilio_error_code(exc),
        )
