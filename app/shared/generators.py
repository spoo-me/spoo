"""
Random code and token generators — pure, side-effect-free functions.

All generators use cryptographically secure sources (``secrets`` module) or
the system PRNG where security is not required (alias generation).
"""

from __future__ import annotations

import random
import secrets
import string

from app.shared.emoji_policy import DEFAULT_GENERATE_MAX_VERSION, generation_pool


def generate_short_code() -> str:
    """Generate a 6-character alphanumeric short code (legacy v1 schema)."""
    letters = string.ascii_lowercase + string.ascii_uppercase + string.digits
    return "".join(random.choice(letters) for _ in range(6))


def generate_short_code_v2(length: int = 7) -> str:
    """Generate an alphanumeric short code of configurable length (v2 schema).

    Args:
        length: Number of characters (default 7, must be 1-255).

    Returns:
        Random alphanumeric string of the requested length.
    """
    if length < 1 or length > 255:
        raise ValueError("length must be between 1 and 255")
    letters = string.ascii_lowercase + string.ascii_uppercase + string.digits
    return "".join(random.choice(letters) for _ in range(length))


def generate_emoji_alias_v2(
    length: int = 3,
    *,
    max_version: float = DEFAULT_GENERATE_MAX_VERSION,
) -> str:
    """Generate an emoji alias from the policy-derived safe pool.

    Args:
        length:      Number of emoji graphemes (default 3, must be 1-15).
        max_version: Newest Unicode emoji version to draw from (default
            pins to the Windows 10 rendering ceiling).

    Returns:
        Random emoji string of *length* graphemes, every one of which
        passes ``shared.emoji_policy.check_emoji_alias``.
    """
    if length < 1 or length > 15:
        raise ValueError("length must be between 1 and 15")
    pool = generation_pool(max_version)
    return "".join(random.choice(pool) for _ in range(length))


def generate_emoji_alias() -> str:
    """Generate a 3-emoji alias (legacy entry point, safe pool)."""
    return generate_emoji_alias_v2(3)


def generate_otp_code(length: int = 6) -> str:
    """Generate a cryptographically secure numeric OTP.

    Args:
        length: Number of digits (default 6, must be 1-128).

    Returns:
        String of random decimal digits.
    """
    if length < 1 or length > 128:
        raise ValueError("length must be between 1 and 128")
    return "".join(secrets.choice(string.digits) for _ in range(length))


def generate_secure_token(length: int = 32) -> str:
    """Generate a cryptographically secure URL-safe random token.

    Args:
        length: Number of random bytes before base64 encoding (default 32).
            The resulting string will be longer than *length* characters.

    Returns:
        URL-safe base64-encoded token string.
    """
    return secrets.token_urlsafe(length)
