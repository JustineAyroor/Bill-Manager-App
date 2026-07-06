from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


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


@runtime_checkable
class NotificationProvider(Protocol):
    """
    Common seam every notification backend (Email, Twilio, or a future
    replacement like Telegram/Pushover) implements. Business logic and the UI
    should only ever talk to providers through this interface, never import a
    specific provider's SDK directly.
    """

    name: str

    def supports(self, channel: str) -> bool:
        """Whether this provider can handle the given channel (e.g. 'SMS')."""
        ...

    def is_configured(self, channel: str | None = None) -> bool:
        """Whether the required credentials/config are present for this provider."""
        ...

    async def send(
        self,
        *,
        channel: str,
        recipient: str,
        subject: str | None,
        text_body: str,
        html_body: str | None = None,
    ) -> SendResult:
        ...

    def fetch_status(self, message_id: str) -> Optional[dict]:
        """Poll delivery status for a previously sent message, if the provider supports it."""
        ...
