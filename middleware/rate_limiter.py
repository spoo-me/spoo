"""
slowapi rate limiter — shared instance, Limits constants, and key resolution.

Storage backend: Redis if REDIS_URI env var is set, otherwise in-memory.
Key resolution: API key hash → JWT token hash → client IP.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import Request
from slowapi import Limiter

from shared.ip_utils import get_client_ip

# ── Limits ───────────────────────────────────────────────────────────────────


class Limits:
    """Single source of truth for all rate limit strings.

    Ported from blueprints/limits.py. All values use slowapi's "N per period"
    format. Semicolons combine multiple limits into one decorator.
    """

    # Global defaults (applied to every route unless overridden)
    DEFAULT_MINUTE = "10 per minute"
    DEFAULT_HOUR = "100 per hour"
    DEFAULT_DAY = "500 per day"

    # API v1 — authenticated vs anonymous tiers
    API_AUTHED = "60 per minute; 5000 per day"
    API_ANON = "20 per minute; 1000 per day"

    # Alias availability check — cheap read, UI debounces on each keystroke
    API_CHECK_AUTHED = "180 per minute; 10000 per day"
    API_CHECK_ANON = "60 per minute; 2000 per day"

    # Auth endpoints
    LOGIN = "5 per minute; 50 per day"
    SIGNUP = "5 per minute; 50 per day"
    LOGOUT = "60 per hour"
    TOKEN_REFRESH = "20 per minute"
    AUTH_READ = "60 per minute"
    ONBOARDING_WRITE = "30 per minute"
    SET_PASSWORD = "5 per minute"
    RESEND_VERIFICATION = "1 per minute; 3 per hour"
    EMAIL_VERIFY = "10 per hour"
    PASSWORD_RESET_REQUEST = "3 per hour"
    PASSWORD_RESET_CONFIRM = "5 per hour"

    # Device auth flow (extensions, apps, CLIs)
    DEVICE_AUTH = "10 per minute"
    DEVICE_TOKEN = "10 per minute"

    # OAuth
    OAUTH_INIT = "10 per minute"
    OAUTH_CALLBACK = "20 per minute"
    OAUTH_LINK = "5 per minute"
    OAUTH_DISCONNECT = "5 per minute"

    # Dashboard
    DASHBOARD_READ = "60 per minute"
    DASHBOARD_WRITE = "30 per minute"
    DASHBOARD_SENSITIVE = "5 per minute"

    # API keys
    API_KEY_CREATE = "5 per hour"
    API_KEY_READ = "60 per minute"
    API_KEY_DELETE = "30 per minute"

    # URL management
    URL_MANAGE = "120 per minute; 2000 per day"
    URL_DELETE = "60 per minute; 1000 per day"
    URL_BULK_DELETE = "5 per minute; 50 per day"

    # Custom domains
    DOMAIN_CREATE = "5 per hour"
    DOMAIN_VERIFY = "10 per minute"
    DOMAIN_READ = "60 per minute"
    DOMAIN_DELETE = "10 per minute"
    DOMAIN_WRITE = "30 per minute"

    # Dashboard — profile pictures
    PROFILE_PICTURE_SET = "10 per minute"

    # Contact / report
    CONTACT = "5 per minute; 20 per hour; 50 per day"

    # URL shortener (legacy endpoint)
    SHORTEN_LEGACY = "100 per minute"

    # Legacy stats / export pages
    STATS_LEGACY_PAGE = "20 per minute; 1000 per day"
    STATS_LEGACY_EXPORT = "10 per minute; 200 per day"

    # Export stats (auth vs anon tiers)
    API_EXPORT_AUTHED = "30 per minute; 1000 per day"
    API_EXPORT_ANON = "10 per minute; 200 per day"

    # Password-protected URL check
    PASSWORD_CHECK = "10 per minute; 30 per hour"


# ── Key resolution ───────────────────────────────────────────────────────────


def rate_limit_key(request: Request) -> str:
    """Three-tier rate limit key: API key hash → JWT hash → client IP.

    Lightweight header inspection only — no DB queries, no JWT verification.
    Provides consistent per-session bucketing for rate limiting purposes.
    """
    auth_header = request.headers.get("Authorization", "")

    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token.startswith("spoo_"):
            token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
            return f"apikey:{token_hash}"
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        return f"jwt:{token_hash}"

    access_token = request.cookies.get("access_token")
    if access_token:
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        return f"jwt:{token_hash}"

    return get_client_ip(request)


# ── Limiter singleton ────────────────────────────────────────────────────────

_redis_uri = os.environ.get("REDIS_URI")
_storage_uri = _redis_uri if _redis_uri else "memory://"

limiter = Limiter(
    key_func=rate_limit_key,
    default_limits=[Limits.DEFAULT_MINUTE, Limits.DEFAULT_HOUR, Limits.DEFAULT_DAY],
    storage_uri=_storage_uri,
    strategy="fixed-window",
)


# ── Dynamic limits ───────────────────────────────────────────────────────────


def dynamic_limit(authenticated: str, anonymous: str) -> tuple:
    """Return a (limit_fn, key_fn) pair for two-tier authenticated/anonymous rate limiting.

    Uses the same ``rate_limit_key`` as all other routes — no separate key format.
    The limit function inspects the key prefix (``jwt:``, ``apikey:``, or raw IP)
    to pick the appropriate tier.

    Usage::

        _limit, _key = dynamic_limit("60 per minute", "20 per minute")

        @router.get("/endpoint")
        @limiter.limit(_limit, key_func=_key)
        async def endpoint(request: Request, ...): ...
    """

    def _limit(key: str) -> str:
        if key.startswith("apikey:") or key.startswith("jwt:"):
            return authenticated
        return anonymous

    return _limit, rate_limit_key
