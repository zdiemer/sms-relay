"""Fan-out of inbound messages to subscriber services.

Each subscriber gets its own `WebhookDelivery` row, so one dead consumer
retries on its own schedule without holding up the others or re-delivering to
services that already acked.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sms_relay.auth import sign
from sms_relay.config import Subscriber, settings
from sms_relay.models import Message, WebhookDelivery, utcnow

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 6


def backoff_seconds(attempts: int) -> int:
    """Exponential backoff capped at an hour: 30s, 1m, 2m, 4m, 8m, ... 60m."""
    return min(30 * (2 ** max(0, attempts - 1)), 3600)


def backoff(attempts: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))


async def enqueue_fanout(session: AsyncSession, message: Message) -> int:
    """Create a pending delivery row per interested subscriber."""
    subscribers = [s for s in settings.subscribers if "message.received" in s.events]
    if not subscribers:
        return 0

    existing = set(
        (
            await session.execute(
                select(WebhookDelivery.subscriber).where(
                    WebhookDelivery.message_id == message.id
                )
            )
        )
        .scalars()
        .all()
    )

    created = 0
    for sub in subscribers:
        if sub.name in existing:
            continue
        session.add(
            WebhookDelivery(
                id=str(uuid.uuid4()),
                message_id=message.id,
                subscriber=sub.name,
                url=sub.url,
                status="pending",
                next_attempt_at=utcnow(),
            )
        )
        created += 1
    return created


def _payload(message: Message) -> bytes:
    body = {
        "event": "message.received",
        "message": {
            "id": message.id,
            "from": message.from_number,
            "to": message.to_number,
            "body": message.body,
            "received_at": message.created_at.isoformat(),
        },
    }
    # Serialize once and sign these exact bytes — re-encoding for the signature
    # risks a different key order than the body actually sent.
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode()


def _subscriber(name: str) -> Subscriber | None:
    for sub in settings.subscribers:
        if sub.name == name:
            return sub
    return None


async def deliver(client: httpx.AsyncClient, delivery: WebhookDelivery, message: Message) -> None:
    """Attempt one delivery, updating the row in place.

    The caller owns the transaction.
    """
    sub = _subscriber(delivery.subscriber)
    if sub is None:
        # The subscriber was removed from config after the row was created.
        delivery.status = "cancelled"
        delivery.error = "subscriber no longer configured"
        return

    payload = _payload(message)
    headers = {
        "Content-Type": "application/json",
        "X-SMS-Relay-Event": "message.received",
        "X-SMS-Relay-Delivery": delivery.id,
    }
    if sub.secret:
        headers["X-Signature"] = sign(payload, sub.secret)

    delivery.attempts += 1
    try:
        response = await client.post(sub.url, content=payload, headers=headers, timeout=15.0)
        delivery.response_code = response.status_code
        if response.status_code < 300:
            delivery.status = "delivered"
            delivery.error = None
            return
        delivery.error = f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.HTTPError as exc:
        delivery.error = str(exc)[:500]

    if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
        delivery.status = "failed"
        logger.error(
            "giving up delivering %s to %s after %d attempts",
            delivery.message_id,
            delivery.subscriber,
            delivery.attempts,
        )
    else:
        delivery.status = "pending"
        delivery.next_attempt_at = backoff(delivery.attempts)
