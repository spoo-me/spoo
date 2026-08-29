"""
Security middleware — CORS configuration, security headers, and request body size limit.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from config import AppSettings

# ── Path-based CORS classification ──────────────────────────────────────────

_PRIVATE_PREFIXES = ("/auth", "/oauth", "/dashboard")
_PUBLIC_PREFIXES = ("/api/v1", "/auth/device", "/stats", "/export", "/metric")

_ALLOWED_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
_ALLOWED_HEADERS = "Authorization, Content-Type, Accept, X-Request-ID, X-Spoo-Client"
# Response headers cross-origin JS may read. Retry-After is included because
# only the CORS-safelisted set (Cache-Control, Content-Language, Content-Length,
# Content-Type, Expires, Last-Modified, Pragma) is readable without it.
_EXPOSED_HEADERS = (
    "X-Request-ID, X-Error-Code, X-Spoo-Hint, X-RateLimit-Limit, "
    "X-RateLimit-Remaining, X-RateLimit-Reset, Retry-After"
)


def _classify_path(path: str) -> str:
    """Return 'public', 'private', or 'none' based on the request path."""
    for prefix in _PUBLIC_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "public"
    for prefix in _PRIVATE_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return "private"
    # Legacy root shortener endpoint (POST /) is public API
    if path == "/":
        return "public"
    return "none"


class SplitCORSMiddleware(BaseHTTPMiddleware):
    """Apply different CORS policies based on the request path.

    - Public routes (``/api/v1/*``, legacy API): ``Access-Control-Allow-Origin: *``,
      no credentials.
    - Private routes (``/auth/*``, ``/oauth/*``, ``/dashboard/*``): origin checked
      against *private_origins* allowlist, credentials allowed.
    - All other routes: no CORS headers.
    """

    def __init__(self, app, *, private_origins: list[str]) -> None:
        super().__init__(app)
        self._private_origins: set[str] = {o.rstrip("/") for o in private_origins}

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        origin = request.headers.get("origin")
        path = request.url.path
        classification = _classify_path(path)

        # Handle preflight — only intercept routes that need CORS
        if request.method == "OPTIONS" and origin and classification != "none":
            return self._preflight(origin, classification)

        response = await call_next(request)

        if origin and classification != "none":
            self._set_cors_headers(response, origin, classification)

        return response

    def _preflight(self, origin: str, classification: str) -> Response:
        """Return a 204 preflight response with appropriate CORS headers."""
        response = PlainTextResponse("", status_code=204)
        if classification != "none":
            self._set_cors_headers(response, origin, classification)
            response.headers["Access-Control-Allow-Methods"] = _ALLOWED_METHODS
            response.headers["Access-Control-Allow-Headers"] = _ALLOWED_HEADERS
            response.headers["Access-Control-Max-Age"] = "86400"
        return response

    def _set_cors_headers(
        self, response: Response, origin: str, classification: str
    ) -> None:
        """Set CORS headers based on route classification."""
        if classification == "public":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Expose-Headers"] = _EXPOSED_HEADERS
        elif classification == "private" and origin in self._private_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Expose-Headers"] = _EXPOSED_HEADERS
            response.headers["Vary"] = "Origin"


def configure_cors(app: FastAPI, settings: AppSettings) -> None:
    """Add split CORS middleware — different policies for public vs private routes."""
    app.add_middleware(
        SplitCORSMiddleware,
        private_origins=settings.cors_private_origins,
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Set standard security headers on all responses."""

    def __init__(self, app, *, hsts_enabled: bool = True) -> None:
        super().__init__(app)
        self.hsts_enabled = hsts_enabled

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if self.hsts_enabled:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy-Report-Only"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response


class StaticCacheHeadersMiddleware(BaseHTTPMiddleware):
    """Set long-lived immutable Cache-Control on /static/* responses.

    Templates cache-bust via ?v=N query strings, so each asset URL is
    effectively content-addressed and safe to cache for a year.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class MaxContentLengthMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured limit."""

    def __init__(self, app, max_content_length: int = 1_048_576) -> None:
        super().__init__(app)
        self.max_content_length = max_content_length

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        content_length = request.headers.get("content-length")
        try:
            parsed = int(content_length) if content_length else None
        except ValueError:
            parsed = None
        if parsed is not None and parsed > self.max_content_length:
            # Rejected before the exception handlers run, so the response
            # must self-describe via X-Error-Code itself.
            return JSONResponse(
                status_code=413,
                content={
                    "error": "Request body too large",
                    "code": "payload_too_large",
                },
                headers={"X-Error-Code": "payload_too_large"},
            )
        return await call_next(request)
