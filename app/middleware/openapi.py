"""
OpenAPI schema configuration — tags, security schemes, and metadata.

Called once during app creation to configure the OpenAPI schema.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.schemas.dto.responses.common import ErrorResponse

# ── Shared response declarations for route decorators ─────────────────────────
# Import these in route files: `from middleware.openapi import ERROR_RESPONSES, ...`

ERROR_RESPONSES = {
    400: {"description": "Bad Request — invalid parameters", "model": ErrorResponse},
    401: {
        "description": "Unauthorized — missing or invalid credentials",
        "model": ErrorResponse,
    },
    403: {
        "description": "Forbidden — insufficient permissions or scope",
        "model": ErrorResponse,
    },
    404: {"description": "Not found", "model": ErrorResponse},
    429: {"description": "Rate limit exceeded", "model": ErrorResponse},
}

AUTH_RESPONSES = {
    **ERROR_RESPONSES,
    409: {"description": "Conflict — resource already exists", "model": ErrorResponse},
}

EXPORT_RESPONSES = {
    **ERROR_RESPONSES,
    500: {
        "description": "Internal server error — export generation failed",
        "model": ErrorResponse,
    },
}

NOT_CONFIGURED_RESPONSES = {
    **ERROR_RESPONSES,
    503: {
        "description": "Not configured — the required webhook is unset on this instance",
        "model": ErrorResponse,
    },
}

# ── Security overrides for route decorators ───────────────────────────────────
# Use these in openapi_extra to override the global security requirement.

# Public — no auth needed (health, OAuth callbacks)
PUBLIC_SECURITY: dict = {"security": []}

# Optional auth — works without auth but accepts it (shorten, stats, export)
OPTIONAL_AUTH_SECURITY: dict = {"security": [{}, {"ApiKeyAuth": []}, {"JWTAuth": []}]}

API_DESCRIPTION = (
    "REST API for spoo.me — free and open-source URL shortening service "
    "serving 400k+ redirects/day.\n\n"
    "Authenticate using either:\n"
    "- **API Key**: `Authorization: Bearer spoo_<your_key>`\n"
    "- **JWT Token**: `Authorization: Bearer <jwt>` (obtained via /auth/login)\n"
    "- **Session Cookie**: `access_token` cookie (set automatically on login)"
)

API_CONTACT = {
    "name": "spoo.me",
    "url": "https://spoo.me/contact",
    "email": "support@spoo.me",
}

API_LICENSE = {
    "name": "AGPL-3.0",
    "url": "https://github.com/spoo-me/spoo/blob/main/LICENSE",
}

OPENAPI_TAGS = [
    {
        "name": "URL Shortening",
        "description": "Create new shortened URLs",
    },
    {
        "name": "Me",
        "description": "Per-user preferences and dashboard layouts",
    },
    {
        "name": "Link Management",
        "description": "List, update, and delete your shortened URLs",
    },
    {
        "name": "Statistics",
        "description": "Click analytics and data export",
    },
    {
        "name": "API Keys",
        "description": "Create and manage API keys for programmatic access",
    },
    {
        "name": "Authentication",
        "description": "Login, register, password management, and email verification",
    },
    {
        "name": "OAuth",
        "description": "OAuth provider login, linking, and unlinking",
    },
    {
        "name": "Reports & Contact",
        "description": "Report abusive URLs and contact the site operators",
    },
    {
        "name": "System",
        "description": "Health checks and server metrics",
    },
]

SECURITY_SCHEMES = {
    "ApiKeyAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "spoo_<key>",
        "description": "API key authentication. Pass your key as: `Bearer spoo_<your_key>`",
    },
    "JWTAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT access token from /auth/login. Pass as: `Bearer <jwt_token>`",
    },
}


def configure_openapi(app: FastAPI, app_url: str = "https://spoo.me") -> None:
    """Attach a custom OpenAPI schema generator with security schemes and servers."""

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            contact=app.contact,
            license_info=app.license_info,
        )
        openapi_schema["servers"] = [
            {"url": app_url, "description": "Production"},
        ]
        openapi_schema["components"]["securitySchemes"] = SECURITY_SCHEMES
        openapi_schema["security"] = [
            {"ApiKeyAuth": []},
            {"JWTAuth": []},
        ]
        app.openapi_schema = openapi_schema
        return openapi_schema

    app.openapi = _custom_openapi
