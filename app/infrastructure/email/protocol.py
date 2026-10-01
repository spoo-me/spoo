"""EmailProvider protocol — services depend on this, not the concrete implementation."""

from typing import Protocol


class EmailProvider(Protocol):
    async def send_verification_email(
        self, email: str, user_name: str | None, otp_code: str
    ) -> bool: ...

    async def send_welcome_email(self, email: str, user_name: str | None) -> bool: ...

    async def send_password_reset_email(
        self, email: str, user_name: str | None, otp_code: str
    ) -> bool: ...
