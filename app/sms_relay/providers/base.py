"""Provider interface.

The only implementation that matters today is the Android gateway, but keeping
the seam means adding a real carrier (or a second phone) later is a new file
rather than a rewrite of the worker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ProviderError(Exception):
    """A send failed.

    `permanent` distinguishes "this will never work" (bad number, rejected
    payload) from "try again later" (timeout, gateway down, phone asleep). The
    worker retries the latter and fails the former immediately, which is the
    difference between a queue that drains and one that spins.
    """

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


@dataclass
class SendResult:
    provider_message_id: str | None = None


class Provider(ABC):
    name: str = "provider"

    @abstractmethod
    async def send(self, to_number: str, body: str) -> SendResult: ...

    async def fetch_status(self, provider_message_id: str) -> str | None:
        """Return a MessageStatus value, or None if the provider can't say.

        Optional: providers that have no delivery-receipt concept inherit this
        and messages simply stay at `sent`.
        """
        return None

    async def aclose(self) -> None:
        return None
