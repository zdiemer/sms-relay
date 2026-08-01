"""Request/response models for the public API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from sms_relay.models import Message


class SendRequest(BaseModel):
    to: str | list[str] = Field(description="Recipient(s); any format libphonenumber accepts")
    body: str = Field(min_length=1, max_length=4000)
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Repeat POSTs with the same key return the original message",
    )


class MessageOut(BaseModel):
    id: str
    direction: str
    status: str
    to: str | None
    from_: str | None = Field(default=None, alias="from")
    body: str
    service: str
    attempts: int
    provider_message_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None

    model_config = {"populate_by_name": True}

    @classmethod
    def of(cls, message: Message) -> "MessageOut":
        return cls(
            id=message.id,
            direction=message.direction,
            status=message.status,
            to=message.to_number,
            from_=message.from_number,
            body=message.body,
            service=message.service,
            attempts=message.attempts,
            provider_message_id=message.provider_message_id,
            error=message.error,
            created_at=message.created_at,
            updated_at=message.updated_at,
            sent_at=message.sent_at,
        )


class SendResponse(BaseModel):
    messages: list[MessageOut]


class MessageList(BaseModel):
    messages: list[MessageOut]
    total: int


class InboundMessage(BaseModel):
    """Normalized inbound event, both as stored and as pushed to subscribers."""

    id: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None
    body: str
    received_at: datetime

    model_config = {"populate_by_name": True}


class WebhookEvent(BaseModel):
    event: str = "message.received"
    message: InboundMessage


class HealthResponse(BaseModel):
    status: str
    provider: str
    database: bool
    queued: int
