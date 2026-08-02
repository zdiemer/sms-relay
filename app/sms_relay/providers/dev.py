"""Filesystem provider for local development.

Writes each message to a file instead of touching a real handset, so the whole
queue/retry/fan-out path can be exercised without a phone.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sms_relay.providers.base import Provider, SendResult

logger = logging.getLogger(__name__)


class DevProvider(Provider):
    name = "dev"

    def __init__(self, output_dir: str):
        self._dir = Path(output_dir)

    async def send(self, to_number: str, body: str) -> SendResult:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe = re.sub(r"[^\w+-]", "_", to_number)[:32]
        path = self._dir / f"{stamp}_{safe}.txt"
        path.write_text(f"To: {to_number}\n\n{body}\n")
        logger.info("dev provider wrote %s", path)
        return SendResult(provider_message_id=f"dev-{uuid.uuid4()}")
