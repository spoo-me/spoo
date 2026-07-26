"""Resolve Host header → TenantInfo on every request.

Lands `request.state.tenant` for downstream handlers. Redirect route
reads it to scope the URL lookup to the right tenant.

Routing policy for custom tenants is a strict allowlist:
  - `GET /<alias>` and `POST /<alias>/password` → redirect router
  - `GET /favicon.ico`                         → static router (generic favicon)
  - `GET /robots.txt`                          → tenant's `custom_robots_txt` or default `Disallow: /`
  - `GET /`                                    → tenant's `root_redirect` (302) or 404
  - Disallowed paths                           → tenant's `not_found_redirect` (302) or 404

Operator surface (`/api/*`, `/dashboard/*`, `/auth/*`, `/oauth/*`, `/health`)
and brand pages (`/about`, `/contact`, `/api-docs`, `/<alias>+`, `/report`)
all 404 on custom tenants (or get the configured `not_found_redirect`).

Per-domain routing config is only honored when ``tenant.status == ACTIVE``.
Revoked/suspended domains 404 like the PR4 baseline so a stale redirect
doesn't keep firing after the owner takes a domain down.

System-default tenant behaves exactly as before — full app surface.

Every custom-tenant response carries `X-Robots-Tag: noindex, nofollow,
noarchive` (post-handler). Short URLs are pure redirects with no indexable
content; this header is unconditional and not user-configurable. The
`custom_robots_txt` field changes what's served at `/robots.txt`, but
`X-Robots-Tag` on alias responses stays put — well-behaved crawlers respect
both signals.
"""

from __future__ import annotations

import re

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from infrastructure.logging import get_logger
from infrastructure.templates import templates
from schemas.enums.domain_status import DomainStatus
from services.tenant_resolver.protocol import TenantInfo, TenantResolver
from shared.url_utils import normalise_host

log = get_logger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "app"})

_CUSTOM_TENANT_ROBOTS_BODY = "User-agent: *\nDisallow: /\n"
_NOINDEX_HEADER = "noindex, nofollow, noarchive"


def _tenant_not_found(request: Request, *, tenant: TenantInfo | None) -> Response:
    """Render the shared minimal 404 page for custom-tenant rejections.

    Same template the redirect route uses on alias misses, so the unknown-
    path and unknown-alias surfaces feel like one product. Self-contained
    HTML/CSS — `/static/*` would 404 on a custom domain, so the page must
    not reference any external asset.
    """
    fqdn = tenant.fqdn if tenant is not None else None
    message = (
        f"This URL doesn't exist on {fqdn}." if fqdn else "This URL doesn't exist."
    )
    return templates.TemplateResponse(
        request,
        "tenant_error.html",
        {
            "error_code": "404",
            "error_title": "Not found",
            "error_message": message,
        },
        status_code=404,
        headers={"X-Robots-Tag": _NOINDEX_HEADER},
    )


# Allowed exact paths on custom tenants (besides the alias pattern).
_ALLOWED_EXACT_PATHS = frozenset({"/favicon.ico"})

# Reserved path prefixes — match these *before* the alias allowlist so
# operator surface (`/dashboard/*`, `/api/*`, …) and brand pages
# (`/about`, `/contact`, …) cannot be exposed through the alias namespace.
# A path is reserved if it equals one of these strings exactly or starts
# with one followed by `/`. Bare alias collisions (e.g. a customer creating
# an alias literally named `dashboard`) are sacrificed for tenant isolation.
_RESERVED_PREFIXES = (
    "/api",
    "/dashboard",
    "/auth",
    "/oauth",
    "/health",
    "/report",
    "/about",
    "/contact",
    "/privacy",
    "/api-docs",
    "/api-reference",
)

# Alias paths allowed on custom tenants. Match `/<alias>` and
# `/<alias>/password` only. Stats suffix (`+`) is intentionally NOT matched
# so `/<alias>+` falls through to 404 — analytics surface stays on spoo.me.
#
# This is a deliberately COARSE gate: "plausibly a short code", nothing
# more. The emoji acceptance policy lives in `shared.emoji_policy` and runs
# at creation; unknown/over-broad aliases matched here still 404 in the
# route. Coverage, broader than the policy by design:
#   - `[A-Za-z0-9_-]` per `shared.validators.validate_alias`
#   - `%XX` percent-encoded runs (browsers encode emoji paths)
#   - U+2190-2BFF: arrows through Misc Symbols, Dingbats, and the 2B00
#     block (⭐ ⬛) — decoded-unicode emoji outside plane 1
#   - U+1F000-1FAFF: every plane-1 pictograph block (mahjong 1F004, cards
#     1F0CF, enclosed 1F170-1F251, pictographs, transport, supplemental,
#     Extended-A/B, skin tones 1F3FB-1F3FF, regional indicators)
#   - singleton emoji outside those ranges (00A9 ©, 00AE ®, 2122 ™, 203C,
#     2049, 2139, 3030, 303D, 3297, 3299) and ZWJ/VS15/VS16/keycap
#     combiners plus tag chars (E0020-E007F), so legacy-lenient forms
#     *reach* the router and 404 gracefully instead of being
#     middleware-policed
_ALIAS_PATTERN = re.compile(
    r"^/"
    r"(?:[A-Za-z0-9_\-]"
    r"|%[0-9A-Fa-f]{2}"
    r"|[\u00A9\u00AE\u200D\u203C\u2049\u20E3\u2122\u2139"
    r"\u2190-\u2BFF\u3030\u303D\u3297\u3299\uFE0E\uFE0F]"
    r"|[\U0001F000-\U0001FAFF\U000E0020-\U000E007F])+"
    r"(?:/password)?$"
)


def _is_reserved_path(path: str) -> bool:
    for prefix in _RESERVED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _is_allowed_on_custom_tenant(path: str, method: str) -> bool:
    """Custom-tenant allowlist gate. Path-and-method check so disallowed
    methods on allowed paths (e.g. ``DELETE /<alias>``) return our 404
    instead of Starlette's 405, preserving the strict deny policy."""
    if path == "/":
        return False
    if _is_reserved_path(path):
        return False
    if path in _ALLOWED_EXACT_PATHS:
        # Static assets (favicon) are read-only.
        return method in {"GET", "HEAD"}
    if _ALIAS_PATTERN.match(path):
        # `/<alias>/password` is a form POST; everything else under the alias
        # namespace (the redirect) is GET/HEAD only.
        if path.endswith("/password"):
            return method == "POST"
        return method in {"GET", "HEAD"}
    return False


class TenantMiddleware(BaseHTTPMiddleware):
    """Populates request.state.tenant from the request Host header.

    On custom tenants additionally enforces the allowlist routing policy
    documented at the top of this module and stamps the noindex header on
    every response.

    ``custom_domains_enabled=False`` (self-host default) relaxes exactly one
    rule: an unknown host serves the full app surface instead of a sitewide
    404. Self-hosters reach their instance through IPs, internal DNS names,
    or domains the app was never told about — rejecting unknown hosts is a
    SaaS-mode concern. Existing custom-domain rows keep resolving either
    way, so flipping the switch never breaks live redirects.
    """

    def __init__(self, app, *, custom_domains_enabled: bool = True) -> None:
        super().__init__(app)
        self._custom_domains_enabled = custom_domains_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        resolver: TenantResolver | None = getattr(
            request.app.state, "tenant_resolver", None
        )
        if resolver is None:
            return await call_next(request)

        host = normalise_host(request.headers.get("host", ""))
        if not host or host in _LOOPBACK_HOSTS:
            request.state.tenant = None
            return await call_next(request)

        tenant: TenantInfo | None = await resolver.resolve(host)
        request.state.tenant = tenant

        if tenant is None:
            if not self._custom_domains_enabled:
                return await call_next(request)
            log.info("tenant_unknown_host", host=host)
            return _tenant_not_found(request, tenant=None)

        if tenant.is_system_default:
            return await call_next(request)

        path = request.url.path
        # Routing config only takes effect on ACTIVE tenants. PENDING /
        # SUSPENDED / REVOKED domains behave like the PR4 baseline (404 /
        # default robots.txt) so a stale redirect on a taken-down domain
        # can't keep firing.
        config_active = tenant.status == DomainStatus.ACTIVE

        if path == "/robots.txt":
            if request.method not in {"GET", "HEAD"}:
                return _tenant_not_found(request, tenant=tenant)
            body = (
                tenant.custom_robots_txt
                if config_active and tenant.custom_robots_txt
                else _CUSTOM_TENANT_ROBOTS_BODY
            )
            return PlainTextResponse(
                body,
                headers={"X-Robots-Tag": _NOINDEX_HEADER},
            )

        # `/` is its own surface — either honors root_redirect or 404s. It
        # does NOT fall through to not_found_redirect; the two fields are
        # deliberately separate so an owner who configures only one doesn't
        # accidentally activate the other on the root.
        if path == "/":
            if (
                request.method in {"GET", "HEAD"}
                and config_active
                and tenant.root_redirect
            ):
                return RedirectResponse(
                    tenant.root_redirect,
                    status_code=302,
                    headers={"X-Robots-Tag": _NOINDEX_HEADER},
                )
            return _tenant_not_found(request, tenant=tenant)

        if not _is_allowed_on_custom_tenant(path, request.method):
            # not_found_redirect only fires on GET/HEAD — redirecting a
            # POST/PUT/DELETE to an arbitrary URL silently drops the body
            # and looks broken to the caller.
            if (
                config_active
                and tenant.not_found_redirect
                and request.method in {"GET", "HEAD"}
            ):
                return RedirectResponse(
                    tenant.not_found_redirect,
                    status_code=302,
                    headers={"X-Robots-Tag": _NOINDEX_HEADER},
                )
            log.info(
                "tenant_path_denied",
                host=host,
                path=path,
                method=request.method,
            )
            return _tenant_not_found(request, tenant=tenant)

        response = await call_next(request)
        response.headers["X-Robots-Tag"] = _NOINDEX_HEADER
        return response
