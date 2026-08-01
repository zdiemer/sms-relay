from __future__ import annotations

import logging

from sms_relay.config import settings
from sms_relay.providers.android_gateway import AndroidGatewayProvider
from sms_relay.providers.base import Provider, ProviderError, SendResult
from sms_relay.providers.dev import DevProvider

logger = logging.getLogger(__name__)

_provider: Provider | None = None


def get_provider() -> Provider:
    global _provider
    if _provider is None:
        if settings.provider == "dev":
            _provider = DevProvider(settings.dev_output_dir)
        else:
            _provider = AndroidGatewayProvider(
                settings.gateway_url,
                settings.gateway_user,
                settings.gateway_password,
            )
        logger.info("using %s provider", _provider.name)
    return _provider


async def close_provider() -> None:
    global _provider
    if _provider is not None:
        await _provider.aclose()
        _provider = None


__all__ = [
    "Provider",
    "ProviderError",
    "SendResult",
    "close_provider",
    "get_provider",
]
