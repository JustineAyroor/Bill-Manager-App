from __future__ import annotations

from typing import Optional

from app.services.notifications.base import NotificationProvider
from app.services.notifications.email_provider import EmailProvider
from app.services.notifications.twilio_provider import TwilioProvider

# Single instances shared across the app. Adding a new channel/provider later
# (e.g. Telegram, Pushover, a different SMS API) means writing one class that
# satisfies NotificationProvider and adding one line here - no UI or
# reminder-eligibility code needs to change.
_email_provider = EmailProvider()
_twilio_provider = TwilioProvider()

_REGISTRY: dict[str, NotificationProvider] = {
    "EMAIL": _email_provider,
    "SMS": _twilio_provider,
    "WHATSAPP": _twilio_provider,
}

# All providers by their `provider` name (as stored on ReminderLog.provider),
# used to route status-sync requests without hardcoding a specific vendor.
_PROVIDERS_BY_NAME: dict[str, NotificationProvider] = {
    _email_provider.name: _email_provider,
    _twilio_provider.name: _twilio_provider,
}

ALL_CHANNELS: tuple[str, ...] = tuple(_REGISTRY.keys())


def get_provider(channel: str) -> Optional[NotificationProvider]:
    return _REGISTRY.get((channel or "").strip().upper())


def is_channel_configured(channel: str) -> bool:
    provider = get_provider(channel)
    if provider is None:
        return False
    return provider.is_configured(channel)


def configured_channels() -> list[str]:
    """Channels that are both known and currently usable given .env config."""
    return [c for c in ALL_CHANNELS if is_channel_configured(c)]


def sync_provider_status(provider_name: str, message_id: str) -> Optional[dict]:
    """
    Poll delivery status for a previously sent message, dispatched by the
    provider name stored on ReminderLog.provider (e.g. "TWILIO"), rather than
    the UI importing a specific vendor's status API directly.
    """
    provider = _PROVIDERS_BY_NAME.get((provider_name or "").strip().upper())
    if provider is None:
        return None
    return provider.fetch_status(message_id)
