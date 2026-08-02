"""SMS Gateway for Android (capcom6) transport.

The phone runs an HTTP server on :8080 with basic auth. The call itself is a
single POST — the value this wrapper adds is that a failure lands in a retry
queue instead of a log line, and that it distinguishes "never going to work"
from "try again in a minute".
"""

from __future__ import annotations

import logging

import httpx

from sms_relay.models import MessageStatus
from sms_relay.providers.base import Provider, ProviderError, SendResult

logger = logging.getLogger(__name__)

# The gateway's own vocabulary, mapped onto ours.
_STATE_MAP = {
    "Pending": MessageStatus.SENT,
    "Processed": MessageStatus.SENT,
    "Sent": MessageStatus.SENT,
    "Delivered": MessageStatus.DELIVERED,
    "Failed": MessageStatus.FAILED,
}


class AndroidGatewayProvider(Provider):
    name = "android_gateway"

    def __init__(self, base_url: str, username: str, password: str, timeout: float = 15.0):
        if not base_url:
            raise ValueError("SMS_RELAY_GATEWAY_URL is required for the android_gateway provider")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(username, password),
            timeout=timeout,
        )

    async def send(self, to_number: str, body: str) -> SendResult:
        payload = {"textMessage": {"text": body}, "phoneNumbers": [to_number]}
        try:
            response = await self._client.post("/message", json=payload)
        except httpx.HTTPError as exc:
            # Network-level: the phone is asleep, off wifi, or the IP moved.
            # Always worth retrying.
            raise ProviderError(f"gateway unreachable: {exc}") from exc

        if response.status_code >= 400:
            # 4xx means the gateway understood and refused — retrying sends the
            # identical payload to the identical rejection. 429 is the one
            # exception: it is explicitly a "later" signal.
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            raise ProviderError(
                f"gateway returned {response.status_code}: {response.text[:200]}",
                permanent=permanent,
            )

        provider_id = None
        try:
            provider_id = (response.json() or {}).get("id")
        except ValueError:
            logger.warning("gateway accepted the message but returned a non-JSON body")

        return SendResult(provider_message_id=provider_id)

    async def fetch_status(self, provider_message_id: str) -> str | None:
        try:
            response = await self._client.get(f"/message/{provider_message_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            state = (response.json() or {}).get("state")
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("could not fetch status for %s: %s", provider_message_id, exc)
            return None
        return _STATE_MAP.get(state)

    async def aclose(self) -> None:
        await self._client.aclose()
