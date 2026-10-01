"""
Request DTOs for account deletion.

DeleteAccountRequest   — DELETE /api/v1/me
RestoreAccountRequest  — POST /auth/restore
"""

from __future__ import annotations

from pydantic import EmailStr, Field, model_validator

from app.schemas.dto.base import RequestBase


class DeleteAccountRequest(RequestBase):
    """Request body for DELETE /api/v1/me.

    Exactly one re-auth proof applies per account: ``password`` for
    accounts with a password set, ``confirm_email`` (the exact account
    email, typed) for OAuth-only accounts. The wrong proof — or a missing
    one — fails re-authentication; the response never says which.
    """

    password: str | None = Field(
        default=None,
        max_length=255,
        description="Account password — re-auth for accounts with a password set",
        examples=["MySecurePass123!"],
    )
    confirm_email: str | None = Field(
        default=None,
        max_length=320,
        description=(
            "The exact account email, typed to confirm — re-auth for "
            "OAuth-only accounts (no password set)"
        ),
        examples=["user@example.com"],
    )


class RestoreAccountRequest(RequestBase):
    """Request body for POST /auth/restore.

    Exactly one restore proof: ``email`` + ``password`` for accounts with
    a password, or ``restore_token`` (the one-shot token from the
    deletion notice email — the only path for OAuth-only accounts).
    Mixing or omitting both is a validation error, not a 403.
    """

    email: EmailStr | None = Field(
        default=None,
        description="Account email address (credential restore)",
        examples=["user@example.com"],
    )
    password: str | None = Field(
        default=None,
        max_length=255,
        description="Account password (credential restore)",
        examples=["MySecurePass123!"],
    )
    restore_token: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        description=(
            "One-shot restore token from the deletion notice email "
            "(token restore — OAuth-only accounts)"
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_proof(self) -> RestoreAccountRequest:
        credentials = self.email is not None or self.password is not None
        if self.restore_token is not None:
            if credentials:
                raise ValueError(
                    "provide either email+password or restore_token, not both"
                )
        elif self.email is None or self.password is None:
            raise ValueError("provide either email+password or restore_token")
        return self
