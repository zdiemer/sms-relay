"""Inbound webhook: the Android gateway pushing received SMS at us.

This is the one route that Authelia does not gate (see the chart's
ingress-webhook.yaml) — a phone cannot complete an interactive login. The HMAC
signature over the raw body is the authentication instead, verified by
`verify_inbound_signature` before anything here runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sms_relay.auth import verify_inbound_signature
from sms_relay.db import get_session
from sms_relay.models import Direction, Message, MessageStatus
from sms_relay.phone import InvalidPhoneNumber, normalize, redact
from sms_relay.webhooks import enqueue_fanout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/inbound", tags=["inbound"])


def _extract(event: dict) -> tuple[str | None, str, str, str | None]:
    """Pull (from, body, provider_id, received_at) out of a gateway event.

    The gateway nests the interesting fields under `payload` and uses
    `phoneNumber`/`message`; older builds post them flat. Accept both rather
    than pinning ourselves to one firmware version.
    """
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    sender = payload.get("phoneNumber") or payload.get("from") or payload.get("sender")
    body = payload.get("message") or payload.get("text") or payload.get("body") or ""
    provider_id = str(
        event.get("id") or payload.get("messageId") or payload.get("id") or ""
    )
    received_at = payload.get("receivedAt") or event.get("receivedAt")
    return sender, body, provider_id, received_at


@router.post("/android-gateway", status_code=status.HTTP_202_ACCEPTED)
async def android_gateway_inbound(
    raw_body: bytes = Depends(verify_inbound_signature),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        event = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from exc

    if not isinstance(event, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected a JSON object")

    event_type = event.get("event", "sms:received")
    if event_type not in ("sms:received", "message.received", "sms:delivered", "sms:sent"):
        logger.info("ignoring gateway event %r", event_type)
        return {"status": "ignored"}

    if event_type in ("sms:delivered", "sms:sent"):
        # Delivery receipts pushed by the gateway; the status loop also polls
        # for these, so treat a push as a free early update.
        return await _apply_receipt(session, event, event_type)

    sender, body, provider_id, received_at = _extract(event)
    if not body:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "message body is empty")

    from_number: str | None = None
    if sender:
        try:
            from_number = normalize(sender)
        except InvalidPhoneNumber:
            # Shortcodes and alphanumeric senders are legitimate; keep the raw
            # value rather than dropping the message.
            from_number = str(sender)[:20]

    if provider_id:
        already = (
            await session.execute(
                select(Message).where(
                    Message.direction == Direction.INBOUND,
                    Message.provider_message_id == provider_id,
                )
            )
        ).scalar_one_or_none()
        if already is not None:
            # The gateway retries until it gets a 2xx, so duplicates are normal.
            logger.info("duplicate inbound %s ignored", provider_id)
            return {"status": "duplicate", "id": already.id}

    message = Message(
        id=str(uuid.uuid4()),
        direction=Direction.INBOUND,
        status=MessageStatus.RECEIVED,
        from_number=from_number,
        body=body,
        service="inbound",
        provider_message_id=provider_id or None,
        created_at=_parse_timestamp(received_at),
    )
    session.add(message)
    await session.flush()

    fanned = await enqueue_fanout(session, message)
    await session.commit()

    logger.info(
        "received inbound message %s from %s, queued for %d subscriber(s)",
        message.id,
        redact(from_number),
        fanned,
    )
    return {"status": "accepted", "id": message.id, "subscribers": fanned}


async def _apply_receipt(session: AsyncSession, event: dict, event_type: str) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
    provider_id = str(payload.get("messageId") or payload.get("id") or "")
    if not provider_id:
        return {"status": "ignored"}

    message = (
        await session.execute(
            select(Message).where(Message.provider_message_id == provider_id)
        )
    ).scalar_one_or_none()
    if message is None:
        return {"status": "unknown"}

    if event_type == "sms:delivered":
        message.status = MessageStatus.DELIVERED
        await session.commit()
    return {"status": "updated", "id": message.id}


def _parse_timestamp(value) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)
