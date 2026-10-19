"""Liveness/readiness. Deliberately unauthenticated — kubelet has no API key."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

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


@router.get("/api/health/gateway")
async def gateway_health() -> JSONResponse:
    """Dependency availability for monitoring, never for kubelet probes.

    The handset's authenticated root endpoint works on versions whose /health
    endpoint returns 500. Do not send an SMS, follow redirects, or expose its
    response, credentials, address, or network exception to callers.
    """
    if settings.provider != "android_gateway" or not settings.gateway_url:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    try:
        async with asyncio.timeout(3):
            async with httpx.AsyncClient(
                auth=(settings.gateway_user, settings.gateway_password),
                timeout=2,
                follow_redirects=False,
            ) as client:
                response = await client.get(settings.gateway_url + "/")
                available = response.status_code == 200
    except (httpx.HTTPError, TimeoutError):
        available = False
    return JSONResponse(
        {"status": "ok" if available else "unavailable"},
        status_code=200 if available else 503,
    )
