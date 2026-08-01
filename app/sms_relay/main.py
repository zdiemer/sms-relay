"""FastAPI application + background worker lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from sms_relay.config import settings
from sms_relay.db import init_db
from sms_relay.providers import close_provider, get_provider
from sms_relay.routes import health, inbound, messages
from sms_relay.worker import fanout_loop, prune_loop, send_loop, status_loop

logging.basicConfig(
    level=os.environ.get("SMS_RELAY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    get_provider()

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(send_loop(stop), name="send"),
        asyncio.create_task(status_loop(stop), name="status"),
        asyncio.create_task(fanout_loop(stop), name="fanout"),
        asyncio.create_task(prune_loop(stop), name="prune"),
    ]
    logger.info(
        "sms-relay ready: provider=%s rate_limit=%d/min subscribers=%d",
        settings.provider,
        settings.rate_limit_per_minute,
        len(settings.subscribers),
    )
    try:
        yield
    finally:
        stop.set()
        # Give the loops a moment to notice the event and finish the in-flight
        # send before cancelling, so a rolling restart doesn't strand a row in
        # `sending` for stuck_after_seconds.
        _, pending = await asyncio.wait(tasks, timeout=10)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_provider()
        logger.info("sms-relay stopped")


app = FastAPI(
    title="sms-relay",
    description="Self-hosted SMS gateway — durable send/receive in front of an Android handset.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(messages.router)
app.include_router(inbound.router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> HTMLResponse:
    """The message log viewer.

    Served from the Authelia-gated root path, so it needs no auth of its own —
    but it fetches through the API, which does, so the page asks for a key.
    """
    with open(os.path.join(_STATIC, "index.html")) as fh:
        return HTMLResponse(fh.read())
