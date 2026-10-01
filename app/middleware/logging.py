"""
Request logging middleware — generates request_id, logs request/response metadata.

Uses structlog contextvars for propagation so service/repo layers can access
request_id without explicit parameter passing.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.infrastructure.logging import get_logger, hash_ip
from app.shared.client_tag import parse_client_tag
from app.shared.ip_utils import get_client_ip

log = get_logger("spoo.request")

# Paths to skip detailed logging (high-volume, low-value)
_SKIP_PATHS = frozenset({"/health", "/favicon.ico"})


def _client_tag(request: Request) -> tuple[str | None, str | None]:
    """Parse the X-Spoo-Client header into (client, client_version)."""
    return parse_client_tag(request.headers.get("x-spoo-client"))


def _auth_kind(request: Request) -> str:
    """Coarse "who is calling" tag without leaking creds.

    Mirrors the token extraction in dependencies/auth.py (case-insensitive
    scheme, stripped token) so the tag can't disagree with how the request
    actually authenticates.
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        if token.startswith("spoo_"):
            return "api_key"
        if token.count(".") == 2:
            return "jwt"
        return "bearer_other"
    if request.cookies.get("access_token"):
        return "jwt_cookie"
    if request.cookies.get("session"):
        return "session_cookie"
    return "anonymous"


def _classify_route(path: str) -> str:
    """Coarse route bucket for filtering."""
    if path.startswith("/static/"):
        return "static"
    if path.startswith("/api/"):
        return "api"
    if path.startswith("/auth/") or path.startswith("/oauth/"):
        return "auth"
    if path.startswith("/dashboard"):
        return "dashboard"
    if path == "/" or path == "/contact" or path == "/report" or path == "/docs":
        return "page"
    if path == "/health" or path == "/favicon.ico":
        return "system"
    if path == "/metric" or path.startswith("/stats") or path.startswith("/export"):
        return "analytics"
    # everything else is likely a redirect short_code
    return "redirect"


def _generate_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = _generate_request_id()
        start = time.perf_counter()
        path = request.url.path

        ua = request.headers.get("user-agent", "")
        country = request.headers.get("cf-ipcountry")
        referrer = request.headers.get("referer")
        ip_hash = hash_ip(get_client_ip(request))
        auth_kind = _auth_kind(request)
        client, client_version = _client_tag(request)

        cf_ray = request.headers.get("cf-ray", "")
        # cf-ray format: <12-hex-id>-<3-letter-pop>, e.g. "9f80a96e7a07f934-SIN"
        cf_pop = cf_ray.rsplit("-", 1)[-1] if "-" in cf_ray else None

        # Param NAMES only — values may be sensitive (passwords, tokens).
        query_keys = list(request.query_params.keys()) or None

        accept_language = request.headers.get("accept-language", "")

        # Bind rich request context to structlog contextvars — every downstream
        # log call (services, repositories, etc.) inherits these fields.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            http_method=request.method,
            http_path=path,
            host=request.headers.get("host"),
            route_class=_classify_route(path),
            is_https=request.url.scheme == "https",
            ip_hash=ip_hash,
            country=country,
            referrer=referrer,
            user_agent=ua[:200] if ua else None,
            auth_kind=auth_kind,
            client=client,
            client_version=client_version,
            cf_ray=cf_ray or None,
            cf_pop=cf_pop,
            query_keys=query_keys,
            accept_language=accept_language[:50] or None,
        )

        response = await call_next(request)

        duration_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-ID"] = request_id

        if path not in _SKIP_PATHS:
            status = response.status_code
            log_fn = (
                log.error
                if status >= 500
                else (log.warning if status >= 400 else log.info)
            )
            # Identity resolved by the auth dependency. It arrives via
            # request.state because contextvars bound inside the handler's
            # task don't propagate back to this dispatch context. Absent
            # for anonymous requests and for requests rejected before auth
            # runs (e.g. rate-limited 429s).
            auth_ctx = getattr(request.state, "auth_ctx", None) or {}
            log_fn(
                "request_completed",
                status_code=status,
                duration_ms=duration_ms,
                content_length=response.headers.get("content-length"),
                content_type=response.headers.get("content-type"),
                cache_status=response.headers.get("cf-cache-status"),
                slow=duration_ms > 500,
                **auth_ctx,
            )

        return response
