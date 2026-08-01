"""API key auth for the send/query API, and HMAC verification for inbound.

Two different callers, two different mechanisms:

* Other services in the cluster present a bearer key. Keys are static, come
  from the chart's Secret, and map 1:1 to a service name so every message row
  records who sent it.
* The Android gateway (a phone, outside the cluster) POSTs to the un-gated
  inbound path and cannot hold a bearer token safely, so it signs the raw body
  with a shared secret instead.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from sms_relay.config import settings

logger = logging.getLogger(__name__)


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Resolve the caller's service name, or 401.

    Accepts either `Authorization: Bearer <key>` or `X-API-Key: <key>`.
    """
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    elif x_api_key:
        presented = x_api_key.strip()

    if not presented:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    keys = settings.api_keys
    # Constant-time over every configured key: a plain dict lookup would leak
    # key material through timing.
    matched = ""
    for key, service in keys.items():
        if hmac.compare_digest(presented, key):
            matched = service
    if not matched:
        logger.warning("rejected request with unknown API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of a raw request body, hex-encoded."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def verify_inbound_signature(request: Request) -> bytes:
    """Verify the inbound webhook signature and return the raw body.

    This path is deliberately outside Authelia (see the chart's
    ingress-webhook.yaml), so the signature IS the authentication. Refuse to
    run at all if no secret is configured rather than silently accepting
    anything that reaches the path.
    """
    secret = settings.webhook_secret
    if not secret:
        logger.error("inbound webhook received but SMS_RELAY_WEBHOOK_SECRET is unset")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound webhooks are not configured",
        )

    body = await request.body()
    presented = (
        request.headers.get("X-Signature")
        or request.headers.get("X-Hub-Signature-256")
        or ""
    ).strip()
    # Accept the `sha256=` prefix some senders use.
    presented = presented.removeprefix("sha256=")

    if not presented or not hmac.compare_digest(presented, sign(body, secret)):
        logger.warning("rejected inbound webhook with bad signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="bad signature"
        )
    return body
