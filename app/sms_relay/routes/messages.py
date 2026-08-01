"""Send + query API. This is what other services call."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sms_relay.auth import require_api_key
from sms_relay.db import get_session
from sms_relay.models import Direction, Message, MessageStatus, utcnow
from sms_relay.phone import InvalidPhoneNumber, normalize, redact
from sms_relay.schemas import MessageList, MessageOut, SendRequest, SendResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["messages"])


async def _existing_by_idempotency_key(
    session: AsyncSession, service: str, key: str
) -> Message | None:
    return (
        await session.execute(
            select(Message).where(
                Message.service == service, Message.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


@router.post(
    "/messages", response_model=SendResponse, status_code=status.HTTP_202_ACCEPTED
)
async def send_message(
    request: SendRequest,
    service: str = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> SendResponse:
    """Queue one message per recipient and return immediately.

    202, not 200: the message is durably accepted, not yet sent. Callers that
    care about the outcome poll `GET /messages/{id}` or read the status in the
    log — they should never block on the handset.
    """
    recipients = [request.to] if isinstance(request.to, str) else list(request.to)
    if not recipients:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no recipients")

    try:
        normalized = [normalize(r) for r in recipients]
    except InvalidPhoneNumber as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # An idempotency key identifies one logical send. Fanning it across several
    # recipients would need a key per row, so scope it to single-recipient
    # sends rather than silently ignoring it.
    if request.idempotency_key and len(normalized) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "idempotency_key is only valid for a single recipient",
        )

    if request.idempotency_key:
        existing = await _existing_by_idempotency_key(
            session, service, request.idempotency_key
        )
        if existing is not None:
            logger.info(
                "idempotent replay of %s from %s", existing.id, service
            )
            return SendResponse(messages=[MessageOut.of(existing)])

    created = [
        Message(
            id=str(uuid.uuid4()),
            direction=Direction.OUTBOUND,
            status=MessageStatus.QUEUED,
            to_number=to_number,
            body=request.body,
            service=service,
            idempotency_key=request.idempotency_key,
            next_attempt_at=utcnow(),
        )
        for to_number in normalized
    ]
    session.add_all(created)

    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent requests raced on the same idempotency key; the loser
        # returns the winner's row, which is the whole contract.
        await session.rollback()
        if request.idempotency_key:
            existing = await _existing_by_idempotency_key(
                session, service, request.idempotency_key
            )
            if existing is not None:
                return SendResponse(messages=[MessageOut.of(existing)])
        raise

    logger.info(
        "queued %d message(s) from %s to %s",
        len(created),
        service,
        ", ".join(redact(m.to_number) for m in created),
    )
    return SendResponse(messages=[MessageOut.of(m) for m in created])


@router.get("/messages/{message_id}", response_model=MessageOut)
async def get_message(
    message_id: str,
    service: str = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    message = await session.get(Message, message_id)
    # Scope reads to the owning service so one API key can't enumerate another
    # service's message bodies. Inbound is readable by any authenticated
    # caller, since inbound has no single owner.
    if message is None or (
        message.direction == Direction.OUTBOUND and message.service != service
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    return MessageOut.of(message)


@router.get("/messages", response_model=MessageList)
async def list_messages(
    service: str = Depends(require_api_key),
    session: AsyncSession = Depends(get_session),
    direction: str | None = Query(default=None),
    message_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
) -> MessageList:
    filters = [
        (Message.service == service) | (Message.direction == Direction.INBOUND)
    ]
    if direction:
        filters.append(Message.direction == direction)
    if message_status:
        filters.append(Message.status == message_status)

    total = (
        await session.execute(select(func.count()).select_from(Message).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Message)
                .where(*filters)
                .order_by(Message.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return MessageList(messages=[MessageOut.of(m) for m in rows], total=total)
