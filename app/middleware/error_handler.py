"""
Global exception handlers with content negotiation.

API requests (JSON Accept, /api/, /auth/, /oauth/ prefixes) get JSON errors.
Browser/page requests get the error.html template.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError as PydanticValidationError
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, ErrorBody
from app.infrastructure.logging import get_logger
from app.infrastructure.templates import templates
from app.middleware.rate_limiter import rate_limit_key

log = get_logger(__name__)


def _field_loc(err: dict) -> str:
    """Extract a readable field name from a Pydantic error's loc tuple."""
    loc = err.get("loc", ())
    parts = [
        str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")
    ]
    return ".".join(parts) if parts else "input"


def _validation_error_response(errors: list[dict]) -> ErrorBody:
    """Build a JSON-serialisable error dict from Pydantic validation errors.

    Follows the same ``{error, code, field?, details?}`` shape used by
    ``AppError.to_dict`` so every error response is structurally identical.
    """
    details = [
        {"field": _field_loc(e), "error": e.get("msg", "invalid value")} for e in errors
    ]

    if len(details) == 1:
        return {
            "error": f"{details[0]['field']}: {details[0]['error']}",
            "code": "validation_error",
            "field": details[0]["field"],
        }

    fields = list(dict.fromkeys(d["field"] for d in details))
    return {
        "error": f"Validation failed for: {', '.join(fields)}",
        "code": "validation_error",
        "details": details,
    }


def _wants_json(request: Request) -> bool:
    """Determine if the request expects a JSON response."""
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    path = request.url.path
    return (
        "application/json" in accept
        or content_type.startswith("application/json")
        or path.startswith("/api/")
        or path.startswith("/auth/")
        or path.startswith("/oauth/")
    )


def _rate_limit_hint(request: Request) -> str | None:
    # Mirrors rate_limit_key on purpose: the hint must match the tier the
    # limiter actually applied, validated or not.
    if request.url.path.startswith("/api/v1"):
        if rate_limit_key(request).startswith(("apikey:", "jwt:")):
            return None
        return (
            "Anonymous requests get the lowest limits. Authenticate with "
            "a free account or an API key for 60 per minute and 5000 per day."
        )
    if request.method == "POST" and request.url.path in ("/", "/emoji"):
        return (
            "The legacy API is strictly rate limited. Use POST /api/v1/shorten "
            "for higher limits: https://docs.spoo.me/rate-limits"
        )
    return None


# Statuses that return EMPTY bodies under EDGE_COMPOSED_ERRORS — must stay
# a subset of what Caddy's handle_response matcher composes, or visitors
# get blank pages (verify at cutover; redirect ⊆ app-level is test-pinned).
# The redirect set is smaller on purpose: its 403 bot-block keeps its body.
EDGE_INTERCEPTED_STATUSES = {404, 410, 429, 451, 500}
REDIRECT_EDGE_INTERCEPTED_STATUSES = {404, 410, 451}

# Slugs for framework-raised HTTPExceptions (a path matching no route, a
# method the route doesn't take). Mirrors the AppError error_code
# vocabulary so the edge composes them identically.
_HTTP_STATUS_SLUGS = {404: "not_found", 405: "method_not_allowed"}


def _json_error(
    status_code: int, content: ErrorBody, headers: dict | None = None
) -> JSONResponse:
    """Build a JSON error response that self-describes via ``X-Error-Code``.

    The header always carries the exact slug from the body's ``code`` field,
    so clients and the edge can route on the header without parsing the body
    — same contract HTML errors already honour via ``_error_html``.
    """
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers={**(headers or {}), "X-Error-Code": content["code"]},
    )


def _error_html(
    request: Request, status_code: int, message: str, slug: str
) -> Response:
    """Render error.html template for browser requests.

    Every HTML error self-describes via ``X-Error-Code`` so the edge can
    route on it. With ``edge_composed_errors`` on, intercepted statuses skip
    the template entirely — Caddy throws the body away and composes the Next
    error page, so rendering it would be wasted work.
    """
    # settings can be absent (app built without lifespan) — default-off is
    # the fail-safe.
    settings = getattr(request.app.state, "settings", None)
    if (
        getattr(settings, "edge_composed_errors", False)
        and status_code in EDGE_INTERCEPTED_STATUSES
        and request.method in {"GET", "HEAD"}
    ):
        return Response(status_code=status_code, headers={"X-Error-Code": slug})

    host_url = str(request.base_url)
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "error_code": str(status_code),
            "error_message": message,
            "host_url": host_url,
        },
        status_code=status_code,
        headers={"X-Error-Code": slug},
    )


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers with content negotiation."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> Response:
        if _wants_json(request):
            return _json_error(exc.status_code, exc.to_dict())
        return _error_html(
            request, exc.status_code, exc.message.upper(), exc.error_code
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Validation errors only occur on typed API/auth routes — always JSON
        return _json_error(422, _validation_error_response(exc.errors()))

    @app.exception_handler(PydanticValidationError)
    async def pydantic_validation_error_handler(
        request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        return _json_error(422, _validation_error_response(exc.errors()))

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
        hint = _rate_limit_hint(request)
        if _wants_json(request):
            content: ErrorBody = {
                "error": "Too many requests",
                "code": "rate_limit_exceeded",
            }
            if hint:
                content["hint"] = hint
            return _json_error(
                429, content, headers={"X-Spoo-Hint": hint} if hint else None
            )
        response = _error_html(request, 429, "TOO MANY REQUESTS", "rate_limit_exceeded")
        if hint:
            response.headers["X-Spoo-Hint"] = hint
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        # Requests that match NO route (e.g. /foo/bar — deeper than any
        # pattern) raise Starlette's own HTTPException and would otherwise
        # bypass content negotiation entirely: raw {"detail": "Not Found"}
        # even for browsers, and no X-Error-Code for the edge to compose on.
        slug = _HTTP_STATUS_SLUGS.get(exc.status_code, f"http_{exc.status_code}")
        message = str(exc.detail) if exc.detail else "Error"
        if _wants_json(request):
            return _json_error(
                exc.status_code,
                {"error": message, "code": slug},
                headers=exc.headers,
            )
        return _error_html(request, exc.status_code, message.upper(), slug)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        log.error(
            "unhandled_exception",
            error=str(exc),
            error_type=type(exc).__name__,
            path=request.url.path,
        )
        if _wants_json(request):
            return _json_error(
                500,
                {
                    "error": "An internal server error occurred.",
                    "code": "internal_error",
                },
            )
        return _error_html(request, 500, "INTERNAL SERVER ERROR", "internal_error")
