"""
Verification token document model.

Maps to the `verification-tokens` MongoDB collection.

Used for email verification OTPs, password reset OTPs, and device auth codes.
token_hash stores SHA-256(otp_code) — the plain OTP is never stored.
used_at is None until the token is consumed.
attempts tracks failed verification tries (max 5 before the token is dead).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from app.schemas.models.base import MongoBaseModel, PyObjectId


class TokenType(str, Enum):
    """Verification token types."""

    EMAIL_VERIFY = "email_verify"
    PASSWORD_RESET = "password_reset"
    DEVICE_AUTH = "extension_auth"
    # One-shot restore link mailed on deletion request — the only cancel
    # path for OAuth-only accounts (no password to restore with).
    DELETION_RESTORE = "deletion_restore"


# Backward-compat aliases for existing imports
TOKEN_TYPE_EMAIL_VERIFY = TokenType.EMAIL_VERIFY
TOKEN_TYPE_PASSWORD_RESET = TokenType.PASSWORD_RESET
TOKEN_TYPE_DEVICE_AUTH = TokenType.DEVICE_AUTH
TOKEN_TYPE_DELETION_RESTORE = TokenType.DELETION_RESTORE


class VerificationTokenDoc(MongoBaseModel):
    """Document model for the `verification-tokens` collection."""

    user_id: PyObjectId
    email: str
    token_hash: str
    token_type: TokenType
    expires_at: datetime
    created_at: datetime | None = None
    used_at: datetime | None = None
    attempts: int = Field(default=0, ge=0)
    app_id: str | None = None  # set for device_auth tokens (consent flow)
    # PKCE S256 code challenge (already a hash — stored verbatim).
    # Set for device_auth tokens; None for OTP-style tokens.
    code_challenge: str | None = None
