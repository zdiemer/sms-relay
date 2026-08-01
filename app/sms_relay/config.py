"""Runtime configuration, read once from the environment at import time.

Everything is env-driven so the Helm chart is the single source of truth: plain
values land as `value:`, credentials as `secretKeyRef:`. There is no config
file to keep in sync.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; falling back to %d", key, raw, default)
        return default


def _env_json(key: str, default):
    """Parse a JSON-valued env var, tolerating an empty/unset value.

    A malformed value is logged and treated as absent rather than crashing the
    process: a typo in the subscriber list should degrade fan-out, not take the
    send path down with it.
    """
    raw = _env(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("%s is not valid JSON; ignoring it", key)
        return default


@dataclass(frozen=True)
class Subscriber:
    """A downstream service that wants inbound messages pushed to it."""

    name: str
    url: str
    secret: str = ""
    events: tuple[str, ...] = ("message.received",)


@dataclass(frozen=True)
class Settings:
    db_path: str = field(default_factory=lambda: _env("SMS_RELAY_DB_PATH", "/data/sms-relay.db"))
    provider: str = field(default_factory=lambda: _env("SMS_RELAY_PROVIDER", "android_gateway"))

    gateway_url: str = field(default_factory=lambda: _env("SMS_RELAY_GATEWAY_URL").rstrip("/"))
    gateway_user: str = field(default_factory=lambda: _env("SMS_RELAY_GATEWAY_USER"))
    gateway_password: str = field(default_factory=lambda: _env("SMS_RELAY_GATEWAY_PASSWORD"))

    default_region: str = field(default_factory=lambda: _env("SMS_RELAY_DEFAULT_REGION", "US"))
    rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("SMS_RELAY_RATE_LIMIT_PER_MINUTE", 30)
    )
    max_attempts: int = field(default_factory=lambda: _env_int("SMS_RELAY_MAX_ATTEMPTS", 5))

    webhook_secret: str = field(default_factory=lambda: _env("SMS_RELAY_WEBHOOK_SECRET"))
    public_url: str = field(default_factory=lambda: _env("SMS_RELAY_PUBLIC_URL").rstrip("/"))

    dev_output_dir: str = field(
        default_factory=lambda: _env("SMS_RELAY_DEV_OUTPUT_DIR", "/tmp/sms-relay")
    )

    # How long a message may sit in `sending` before the worker assumes the
    # pod died mid-send and re-queues it. Must exceed the provider timeout.
    stuck_after_seconds: int = field(
        default_factory=lambda: _env_int("SMS_RELAY_STUCK_AFTER_SECONDS", 120)
    )
    retention_days: int = field(
        default_factory=lambda: _env_int("SMS_RELAY_RETENTION_DAYS", 90)
    )

    @property
    def api_keys(self) -> dict[str, str]:
        """Map of API key -> owning service name.

        Configured the other way round (name -> key) because that reads better
        in values.local.yaml; inverted here since lookup is always by key.
        """
        configured = _env_json("SMS_RELAY_API_KEYS", {})
        if not isinstance(configured, dict):
            logger.error("SMS_RELAY_API_KEYS must be a JSON object of name -> key")
            return {}
        return {str(key): str(name) for name, key in configured.items() if key}

    @property
    def subscribers(self) -> list[Subscriber]:
        raw = _env_json("SMS_RELAY_SUBSCRIBERS", [])
        if not isinstance(raw, list):
            logger.error("SMS_RELAY_SUBSCRIBERS must be a JSON array")
            return []
        out: list[Subscriber] = []
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("url"):
                logger.error("skipping malformed subscriber entry: %r", entry)
                continue
            out.append(
                Subscriber(
                    name=str(entry.get("name") or entry["url"]),
                    url=str(entry["url"]),
                    secret=str(entry.get("secret", "")),
                    events=tuple(entry.get("events") or ("message.received",)),
                )
            )
        return out


settings = Settings()
