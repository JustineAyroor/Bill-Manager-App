from __future__ import annotations

from app.services.notifications.base import SendResult
from app.services.notifications.registry import (
    configured_channels,
    get_provider,
    sync_provider_status,
)

__all__ = ["SendResult", "send_reminder", "configured_channels", "sync_provider_status"]


async def send_reminder(
    *,
    channel: str,
    recipient: str,
    subject: str | None,
    text_body: str,
    html_body: str | None = None,
) -> SendResult:
    channel = (channel or "").strip().upper()
    provider = get_provider(channel)

    if provider is None:
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

    try:
        return await provider.send(
            channel=channel,
            recipient=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except Exception as exc:
        return SendResult(
            channel=channel,
            recipient=recipient,
            sender=None,
            provider=getattr(provider, "name", "UNKNOWN"),
            provider_message_id=None,
            provider_status="FAILED",
            success=False,
            error=str(exc),
        )
