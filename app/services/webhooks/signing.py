"""Standard Webhooks signing — https://www.standardwebhooks.com/

Headers: ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``.
Signature: ``v1,`` + base64(HMAC-SHA256(secret, "{msg_id}.{timestamp}.{body}")).

The secret is ``whsec_`` + base64(24 random bytes); shown once at
creation, stored AES-GCM encrypted (infrastructure/crypto.py) because
the server signs with it at delivery time. The BODY is frozen across retries
but the timestamp — and therefore the signature — is fresh per
attempt: replay protection requires it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid

SECRET_PREFIX = "whsec_"
_SECRET_BYTES = 24
DISPLAY_PREFIX_LEN = 8  # chars of the secret shown for identification

HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"


def new_webhook_id() -> str:
    return f"msg_{uuid.uuid4().hex}"


def generate_signing_secret() -> str:
    return SECRET_PREFIX + base64.b64encode(secrets.token_bytes(_SECRET_BYTES)).decode()


def secret_display_prefix(secret: str) -> str:
    return secret[: len(SECRET_PREFIX) + DISPLAY_PREFIX_LEN]


def _secret_key_bytes(secret: str) -> bytes:
    """Per Standard Webhooks, the HMAC key is the base64-DECODED portion
    after the ``whsec_`` prefix."""
    return base64.b64decode(secret.removeprefix(SECRET_PREFIX))


def sign(msg_id: str, timestamp: int, body: str, secret: str) -> str:
    to_sign = f"{msg_id}.{timestamp}.{body}".encode()
    digest = hmac.new(_secret_key_bytes(secret), to_sign, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def verify(msg_id: str, timestamp: int, body: str, secret: str, signature: str) -> bool:
    """Constant-time verification — used by tests and by consumers' docs
    examples; the server itself only signs."""
    expected = sign(msg_id, timestamp, body, secret)
    # A receiver may get multiple space-delimited signatures during
    # secret-rotation grace; accept if ANY matches.
    return any(
        hmac.compare_digest(expected, candidate)
        for candidate in signature.split(" ")
        if candidate
    )
