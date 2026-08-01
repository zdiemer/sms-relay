"""Background loops: outbound sends, delivery-status polling, webhook fan-out.

Runs in-process alongside the API (single replica, so there is no coordination
problem). Every loop is crash-safe because state lives in the database, not in
memory: a pod restart mid-send leaves a row in `sending`, which
`_requeue_stuck` picks back up.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sms_relay.config import settings
from sms_relay.db import get_sessionmaker
from sms_relay.models import (
    TERMINAL_STATUSES,
    Direction,
    Message,
    MessageStatus,
    WebhookDelivery,
    utcnow,
)
from sms_relay.providers import ProviderError, get_provider

logger = logging.getLogger(__name__)

SEND_POLL_SECONDS = 2
STATUS_POLL_SECONDS = 60
FANOUT_POLL_SECONDS = 5
PRUNE_INTERVAL_SECONDS = 6 * 3600


def backoff_seconds(attempts: int) -> int:
    """30s, 1m, 2m, 4m, 8m — capped at 15 minutes."""
    return min(30 * (2 ** max(0, attempts - 1)), 900)


def send_backoff(attempts: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts))


class RateLimiter:
    """Sliding-window limiter.

    A phone is not a carrier trunk: pushing a whole percolation batch at it in
    one burst gets messages silently dropped by the handset. Spacing them is
    the entire reason sends go through a queue rather than straight out.
    """

    def __init__(self, per_minute: int):
        self._per_minute = max(1, per_minute)
        self._sent: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = asyncio.get_running_loop().time()
            while self._sent and now - self._sent[0] >= 60:
                self._sent.popleft()
            if len(self._sent) < self._per_minute:
                self._sent.append(now)
                return
            await asyncio.sleep(max(0.1, 60 - (now - self._sent[0])))


async def _requeue_stuck(session: AsyncSession) -> None:
    """Return messages orphaned in `sending` by a pod restart to the queue."""
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.stuck_after_seconds)
    rows = (
        (
            await session.execute(
                select(Message).where(
                    Message.status == MessageStatus.SENDING,
                    Message.updated_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for message in rows:
        logger.warning("requeueing %s stuck in sending", message.id)
        message.status = MessageStatus.QUEUED
        message.next_attempt_at = utcnow()
    if rows:
        await session.commit()


async def _claim_next(session: AsyncSession) -> Message | None:
    """Take the oldest due message and mark it `sending`."""
    now = utcnow()
    message = (
        await session.execute(
            select(Message)
            .where(
                Message.direction == Direction.OUTBOUND,
                Message.status == MessageStatus.QUEUED,
                or_(Message.next_attempt_at.is_(None), Message.next_attempt_at <= now),
            )
            .order_by(Message.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()

    if message is None:
        return None

    message.status = MessageStatus.SENDING
    message.updated_at = now
    await session.commit()
    return message


async def send_loop(stop: asyncio.Event) -> None:
    limiter = RateLimiter(settings.rate_limit_per_minute)
    sessionmaker = get_sessionmaker()
    provider = get_provider()

    while not stop.is_set():
        try:
            async with sessionmaker() as session:
                await _requeue_stuck(session)
                message = await _claim_next(session)

                if message is None:
                    await _sleep_or_stop(stop, SEND_POLL_SECONDS)
                    continue

                await limiter.acquire()
                try:
                    result = await provider.send(message.to_number or "", message.body)
                except ProviderError as exc:
                    message.attempts += 1
                    message.error = str(exc)[:500]
                    exhausted = message.attempts >= settings.max_attempts
                    if exc.permanent or exhausted:
                        message.status = MessageStatus.FAILED
                        logger.error(
                            "message %s failed permanently after %d attempt(s): %s",
                            message.id,
                            message.attempts,
                            exc,
                        )
                    else:
                        message.status = MessageStatus.QUEUED
                        message.next_attempt_at = send_backoff(message.attempts)
                        logger.warning(
                            "message %s attempt %d failed, retrying at %s: %s",
                            message.id,
                            message.attempts,
                            message.next_attempt_at,
                            exc,
                        )
                else:
                    message.attempts += 1
                    message.status = MessageStatus.SENT
                    message.provider_message_id = result.provider_message_id
                    message.sent_at = utcnow()
                    message.error = None
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("send loop iteration failed")
            await _sleep_or_stop(stop, SEND_POLL_SECONDS)


async def status_loop(stop: asyncio.Event) -> None:
    """Upgrade `sent` to `delivered`/`failed` using provider receipts."""
    sessionmaker = get_sessionmaker()
    provider = get_provider()

    while not stop.is_set():
        await _sleep_or_stop(stop, STATUS_POLL_SECONDS)
        if stop.is_set():
            return
        try:
            async with sessionmaker() as session:
                # Only chase recent ones; a receipt that hasn't arrived within a
                # day is not going to.
                cutoff = datetime.now(UTC) - timedelta(days=1)
                pending = (
                    (
                        await session.execute(
                            select(Message).where(
                                Message.status == MessageStatus.SENT,
                                Message.provider_message_id.is_not(None),
                                Message.sent_at > cutoff,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                changed = False
                for message in pending:
                    status = await provider.fetch_status(message.provider_message_id or "")
                    if status and status != message.status:
                        message.status = status
                        changed = True
                if changed:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("status loop iteration failed")


async def fanout_loop(stop: asyncio.Event) -> None:
    """Push inbound messages to subscribers, retrying failures with backoff."""
    from sms_relay.webhooks import deliver

    sessionmaker = get_sessionmaker()
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                async with sessionmaker() as session:
                    now = utcnow()
                    due = (
                        (
                            await session.execute(
                                select(WebhookDelivery)
                                .where(
                                    WebhookDelivery.status == "pending",
                                    or_(
                                        WebhookDelivery.next_attempt_at.is_(None),
                                        WebhookDelivery.next_attempt_at <= now,
                                    ),
                                )
                                .order_by(WebhookDelivery.created_at)
                                .limit(20)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if not due:
                        await _sleep_or_stop(stop, FANOUT_POLL_SECONDS)
                        continue

                    for delivery in due:
                        message = await session.get(Message, delivery.message_id)
                        if message is None:
                            delivery.status = "cancelled"
                            continue
                        await deliver(client, delivery, message)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fanout loop iteration failed")
                await _sleep_or_stop(stop, FANOUT_POLL_SECONDS)


async def prune_loop(stop: asyncio.Event) -> None:
    """Drop terminal messages past the retention window.

    Without this the SQLite file grows forever on a 1Gi PVC.
    """
    sessionmaker = get_sessionmaker()
    while not stop.is_set():
        await _sleep_or_stop(stop, PRUNE_INTERVAL_SECONDS)
        if stop.is_set():
            return
        try:
            cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)
            async with sessionmaker() as session:
                result = await session.execute(
                    delete(Message).where(
                        Message.status.in_(TERMINAL_STATUSES),
                        Message.created_at < cutoff,
                    )
                )
                await session.commit()
                if result.rowcount:
                    logger.info("pruned %d message(s) older than %d days",
                                result.rowcount, settings.retention_days)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("prune loop iteration failed")


async def queue_depth() -> int:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.status == MessageStatus.QUEUED)
            )
        ).scalar_one()


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately on shutdown so the pod terminates promptly."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
