"""Recipient-scoped ntfy routing, shared by existing API callers."""
import asyncio
import re

import httpx

from sms_relay.config import settings
from sms_relay.phone import normalize


def topic_for(service, recipient, body):
    route = settings.notification_routes.get(service, {})
    if not route or not settings.ntfy_url:
        return None
    # Never publish another user's private messages to the owner's ntfy topic.
    if normalize(route["to"]) != recipient:
        return None
    pattern = route.get("bodyPattern")
    if pattern and not re.search(pattern, body):
        return None
    topic = route.get("topic", "")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", topic):
        raise ValueError("invalid notification topic")
    return topic


async def publish(message):
    async with asyncio.timeout(8):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.post(
                settings.ntfy_url.rstrip("/") + "/",
                auth=(settings.ntfy_user, settings.ntfy_password),
                json={"topic": message.notification_topic, "message": message.body,
                      "title": message.service, "tags": ["bell"],
                      "priority": getattr(message, "notification_priority", 3)},
            )
            response.raise_for_status()
            receipt = response.json()
            if not isinstance(receipt, dict) or receipt.get("event") != "message" or not isinstance(receipt.get("id"), str):
                raise ValueError("ntfy did not acknowledge publication")
            return receipt["id"]
