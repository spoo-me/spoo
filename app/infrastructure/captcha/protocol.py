"""CaptchaProvider protocol — services depend on this, not the concrete implementation."""

from typing import Protocol


class CaptchaProvider(Protocol):
    async def verify(self, token: str) -> bool: ...
