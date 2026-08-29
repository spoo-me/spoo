"""
slowapi rate limiter — shared instance, Limits constants, and key resolution.

Storage backend: Redis if REDIS_URI env var is set, otherwise in-memory.
Key resolution: API key hash → JWT token hash → client IP.
"""

from __future__ import annotations

import hashlib
import math
import os

from fastapi import Request
from slowapi import Limiter
from starlette.datastructures import MutableHeaders

from infrastructure.logging import get_logger
from shared.ip_utils import get_client_ip

log = get_logger(__name__)

# ── Limits ───────────────────────────────────────────────────────────────────


class Limits:
    """Single source of truth for all rate limit strings.

    Ported from blueprints/limits.py. All values use slowapi's "N per period"
    format. Semicolons combine multiple limits into one decorator.
    """

    # API v1 — authenticated vs anonymous tiers
    API_AUTHED = "60 per minute; 5000 per day"
    API_ANON = "20 per minute; 200 per day"

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
    PROFILE_UPDATE = "10 per minute"
    RESEND_VERIFICATION = "1 per minute; 3 per hour"
    EMAIL_VERIFY = "10 per hour"
    PASSWORD_RESET_REQUEST = "3 per hour"
    PASSWORD_RESET_CONFIRM = "5 per hour"

    # Device auth flow (extensions, apps, CLIs)
    DEVICE_AUTH = "10 per minute"
    DEVICE_TOKEN = "10 per minute"
    APP_GRANTS_READ = "60 per minute"

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

    # Per-user page layouts (client debounces writes)
    LAYOUT_READ = "120 per minute"
    LAYOUT_WRITE = "60 per minute"
    LAYOUT_DELETE = "30 per minute"

    # URL management
    URL_MANAGE = "120 per minute; 2000 per day"
    URL_DELETE = "60 per minute; 1000 per day"
    URL_BULK_DELETE = "5 per minute; 50 per day"

    # Bulk URL mutations (POST /api/v1/urls/bulk/*). Counted per REQUEST,
    # not per item — the reports-intake stance: one bulk call is one unit
    # of user intent, and per-item billing is what pushed the dashboard
    # into 429s at trivially reachable selection sizes. Requests are what
    # a management workflow actually spends (most real batches are small),
    # so the request budget must never make bulk scarcer than looping the
    # per-item routes — that would push clients back to the fan-out these
    # endpoints exist to kill. Per-minute is kept high for bursts (a mass
    # takedown chunks at 100 ids/request); the daily cap is a lid, not a
    # ration. Blast radius per request is bounded by the 100-id cap and
    # ownership scoping, and each batch is ~4 local calls plus at most
    # one CF call, so these are cheap requests. Delete stays the tighter
    # pair because it is irreversible.
    URL_BULK_STATUS = "60 per minute; 200 per day"
    URL_BULK_EXPIRY = "60 per minute; 200 per day"
    URL_BULK_DOMAIN = "60 per minute; 200 per day"
    URL_BULK_MUTATE_DELETE = "30 per minute; 100 per day"

    # Claim intake (POST /api/v1/urls/claim). Per submission, 16-item cap;
    # guessing is theater at 256 bits — this is belt-and-suspenders.
    URL_CLAIM = "30 per minute; 500 per day"

    # Destination metadata fetch — outbound fetches on our dime, but the
    # ~1h result cache means only novel URLs actually fetch.
    METADATA_FETCH = "60 per minute; 2000 per day"

    # Anonymous callers on GET /metadata (the link preview checker tool).
    # Generous on purpose — it's a free tool — but still per-IP bounded:
    # results cache ~1h, so only novel URLs cost an outbound fetch.
    METADATA_ANON = "15 per minute; 300 per day"

    # Custom domains. Create counts FAILED attempts too (slowapi increments
    # at route entry), so the budget must absorb typos, blocked TLDs, and
    # flag-gate 404s without stranding the user for long.
    DOMAIN_CREATE = "10 per hour"
    DOMAIN_VERIFY = "10 per minute"
    DOMAIN_READ = "60 per minute"
    DOMAIN_DELETE = "10 per minute"
    DOMAIN_WRITE = "30 per minute"

    # Dashboard — profile pictures. Uploads are tighter: each one is an
    # R2 PUT on our dime.
    PROFILE_PICTURE_SET = "10 per minute"
    PROFILE_PICTURE_UPLOAD = "5 per minute"

    # Contact / report
    CONTACT = "5 per minute; 20 per hour; 50 per day"

    # Report intake (POST /api/v1/reports). Counted per SUBMISSION, not per
    # item — one bulk POST of 100 codes is one unit of downstream triage
    # work, and per-item billing would push researchers back to
    # one-code-at-a-time filing (the exact friction bulk intake exists to
    # remove). Anonymous stays tight because it's also the abuse-of-abuse
    # budget: captcha + the 25-item cap bounds a day of anonymous garbage
    # at 1,000 codes. Authenticated is generous on purpose — an abuse desk
    # working a campaign files hundreds of codes in minutes, and the API
    # key gives us a reputation handle if a reporter turns out to be noise.
    REPORTS_AUTHED = "30 per minute; 500 per day"
    REPORTS_ANON = "5 per minute; 40 per day"

    # Webhooks. Creates are tight (each one is an outbound-call
    # authorization); test sends bounded because each is a real signed POST
    # from our servers; the catalog is public documentation-as-API.
    WEBHOOK_CREATE = "10 per hour"
    WEBHOOK_READ = "60 per minute"
    WEBHOOK_WRITE = "30 per minute"
    WEBHOOK_TEST = "10 per minute"
    WEBHOOK_RETRY = "30 per minute"
    WEBHOOK_EVENT_TYPES = "60 per minute"

    # URL shortener (legacy endpoints) — strict on purpose, pushes to v1
    SHORTEN_LEGACY = "5 per minute; 50 per day"

    # Legacy stats / export pages
    STATS_LEGACY_PAGE = "20 per minute; 1000 per day"
    STATS_LEGACY_EXPORT = "10 per minute; 200 per day"

    # Export stats (auth required)
    API_EXPORT_AUTHED = "30 per minute; 1000 per day"

    # Password-protected URL check
    PASSWORD_CHECK = "10 per minute; 30 per hour"

    # Public link preview (the /{code}+ wire). Like the redirect (which is
    # limiter-exempt) this endpoint is an existence oracle by design, so it
    # gets its own bounded budget instead of riding the anon API tier.
    PUBLIC_PREVIEW = "30 per minute; 2000 per day"

    # Public per-link stats (the /stats/{code} wire). Two reasons this
    # gets its own budget instead of riding the generic API tier: every
    # anonymous hit can run a 90-day $facet aggregation over a hot link's
    # clicks — far heavier than a typical API read — and the same bucket
    # is the password-guess budget (401s are billed like any request, and
    # v1 passwords compare server-side for cheap, so the limiter is the
    # only real brake on guessing). Authed keeps headroom: owners
    # re-checking their own private/password link ride this endpoint too.
    PUBLIC_STATS_AUTHED = "60 per minute; 2000 per day"
    PUBLIC_STATS_ANON = "20 per minute; 500 per day"


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


class _HeaderSafeLimiter(Limiter):
    """Limiter whose decorator never injects headers itself.

    ``RateLimitHeadersMiddleware`` is the single injection point. Disabling
    the decorator side entirely (rather than only when there is no Response
    to mutate) avoids a second ``get_window_stats`` storage read on endpoints
    that return a Response object, and keeps slowapi's decorator from raising
    on endpoints that return plain data.
    """

    def _inject_headers(self, response, current_limit):
        return response


limiter = _HeaderSafeLimiter(
    key_func=rate_limit_key,
    storage_uri=_storage_uri,
    strategy="fixed-window",
    headers_enabled=True,
)


class RateLimitHeadersMiddleware:
    """Inject X-RateLimit-* and Retry-After headers at the ASGI layer.

    The rate-limit decorator records the evaluated window on
    ``request.state.view_rate_limit`` for every limited route. Reading it
    from the scope state here covers all response shapes, including plain
    dict returns and 429s produced by the exception handler.

    slowapi's injection is post-processed: Retry-After only means something
    on a 429, and the reset timestamp must be integer epoch seconds (slowapi
    emits a float, which strict header parsers reject). Injection failures
    are swallowed — headers are cosmetic and must never fail a response
    whose work already completed.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                view_limit = scope.get("state", {}).get("view_rate_limit")
                if view_limit is not None:
                    headers = MutableHeaders(raw=message["headers"])
                    had_retry_after = "Retry-After" in headers
                    try:
                        limiter._inject_asgi_headers(headers, view_limit)
                    except Exception:
                        log.warning("rate_limit_header_injection_failed", exc_info=True)
                    else:
                        reset = headers.get("X-RateLimit-Reset")
                        if reset is not None:
                            headers["X-RateLimit-Reset"] = str(math.ceil(float(reset)))
                        if message["status"] != 429 and not had_retry_after:
                            del headers["Retry-After"]
            await send(message)

        await self.app(scope, receive, send_wrapper)


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
