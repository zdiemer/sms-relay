"""Persistence model.

The whole point of this service is that a send survives a pod restart, so the
queue *is* the table: `POST /messages` writes a row and returns, and the worker
loop is the only thing that talks to the provider. There is no in-memory queue
to lose.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Direction(enum.StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(enum.StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"  # handed to the provider
    DELIVERED = "delivered"  # provider confirmed handset delivery
    FAILED = "failed"  # retries exhausted, or a permanent error
    RECEIVED = "received"  # inbound only


# Statuses a message can never leave. Used to keep the worker's claim query
# cheap and to make retention pruning safe.
TERMINAL_STATUSES = (MessageStatus.DELIVERED, MessageStatus.FAILED, MessageStatus.RECEIVED)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), default=Direction.OUTBOUND)
    status: Mapped[str] = mapped_column(String(16), default=MessageStatus.QUEUED)

    # E.164. `sender` is the owning service (API key holder) for outbound and
    # the originating handset for inbound.
    to_number: Mapped[str | None] = mapped_column(String(20))
    from_number: Mapped[str | None] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)

    service: Mapped[str] = mapped_column(String(64), index=True)

    # Callers may supply an idempotency key; a repeat POST with the same
    # (service, key) returns the original message instead of sending twice.
    idempotency_key: Mapped[str | None] = mapped_column(String(128))

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Partial-unique so the many NULL-keyed messages don't collide.
        UniqueConstraint("service", "idempotency_key", name="uq_messages_service_idem"),
        Index("ix_messages_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_messages_created_at", "created_at"),
    )


class WebhookDelivery(Base):
    """One fan-out attempt of an inbound message to one subscriber.

    Kept separate from `messages` so a subscriber being down retries on its own
    schedule without touching the message row every time.
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), index=True
    )
    subscriber: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    message: Mapped[Message] = relationship(back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("message_id", "subscriber", name="uq_delivery_message_subscriber"),
        Index("ix_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )
