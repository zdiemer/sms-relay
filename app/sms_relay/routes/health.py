"""Liveness/readiness. Deliberately unauthenticated — kubelet has no API key."""

from __future__ import annotations

from fastapi import APIRouter

from sms_relay.config import settings
from sms_relay.db import healthy
from sms_relay.schemas import HealthResponse
from sms_relay.worker import queue_depth

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report DB reachability and queue depth.

    Note this does NOT probe the phone gateway: the handset being briefly
    unreachable is exactly the condition the retry queue exists to absorb, and
    failing readiness for it would restart the pod that owns the queue.
    """
    db_ok = await healthy()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        provider=settings.provider,
        database=db_ok,
        queued=await queue_depth() if db_ok else -1,
    )
