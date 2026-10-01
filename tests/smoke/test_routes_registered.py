"""Smoke tests: route registration and ordering verification."""

from __future__ import annotations

import os
from typing import NamedTuple

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/")

from fastapi import FastAPI
from fastapi.routing import APIRoute


class _ResolvedRoute(NamedTuple):
    """A flattened API route with its fully-qualified path and HTTP methods."""

    path: str
    methods: frozenset[str]


def _get_api_routes(app: FastAPI) -> list[_ResolvedRoute]:
    """Return every API route as ``(full_path, methods)`` in registration order.

    FastAPI >= 0.137 no longer flattens ``include_router()`` routes into
    ``app.routes``; included routers appear as ``_IncludedRouter`` wrapper nodes
    whose real routes live under ``.original_router.routes`` and whose mount
    prefix lives on ``.include_context.prefix``. Walk that tree accumulating
    prefixes so full paths (e.g. ``/api/v1/shorten``) and methods (incl. the
    auto-added ``HEAD``) are reconstructed. Falls back to flat ``APIRoute``
    objects on older FastAPI, where they appear directly in ``app.routes``.
    """

    def _collect(routes, prefix: str = "") -> list[_ResolvedRoute]:
        found: list[_ResolvedRoute] = []
        for route in routes:
            if isinstance(route, APIRoute):
                found.append(
                    _ResolvedRoute(prefix + route.path, frozenset(route.methods))
                )
            elif hasattr(route, "original_router"):
                child_prefix = (
                    getattr(getattr(route, "include_context", None), "prefix", "") or ""
                )
                found.extend(
                    _collect(route.original_router.routes, prefix + child_prefix)
                )
        return found

    return _collect(app.routes)


def _get_path_method_pairs(app: FastAPI) -> set[tuple[str, str]]:
    """Return set of (path, METHOD) pairs for all API routes."""
    pairs = set()
    for route in _get_api_routes(app):
        for method in route.methods:
            pairs.add((route.path, method))
    return pairs


def test_all_expected_paths_registered(smoke_app: FastAPI) -> None:
    """Every expected endpoint path should be present in the app's routes."""
    paths = {r.path for r in _get_api_routes(smoke_app)}

    expected_paths = [
        # Health
        "/health",
        # Auth
        "/login",
        "/register",
        "/signup",
        "/auth/login",
        "/auth/register",
        "/auth/refresh",
        "/auth/logout",
        "/auth/me",
        "/auth/set-password",
        "/auth/verify",
        "/auth/send-verification",
        "/auth/verify-email",
        "/auth/request-password-reset",
        "/auth/reset-password",
        # OAuth
        "/oauth/providers",
        "/oauth/providers/{provider_name}/unlink",
        "/oauth/{provider}",
        "/oauth/{provider}/callback",
        "/oauth/{provider}/link",
        # API v1
        "/api/v1/shorten",
        "/api/v1/urls",
        "/api/v1/urls/{url_id}",
        "/api/v1/urls/{url_id}/status",
        "/api/v1/urls/bulk/delete",
        "/api/v1/urls/bulk/status",
        "/api/v1/urls/bulk/expiry",
        "/api/v1/urls/bulk/domain",
        "/api/v1/stats",
        "/api/v1/export",
        "/api/v1/keys",
        "/api/v1/keys/{key_id}",
        "/api/v1/reports",
        "/api/v1/contact",
        # Dashboard
        "/dashboard",
        "/dashboard/",
        "/dashboard/links",
        "/dashboard/keys",
        "/dashboard/statistics",
        "/dashboard/settings",
        "/dashboard/billing",
        "/dashboard/profile-pictures",
        # Static / SEO / legal
        "/robots.txt",
        "/sitemap.xml",
        "/humans.txt",
        "/security.txt",
        "/favicon.ico",
        "/api",
        "/docs",
        "/docs/",
        "/docs/privacy-policy",
        "/privacy-policy",
        "/privacy",
        "/tos",
        "/terms-of-service",
        "/refund",
        "/refund-policy",
        "/acceptable-use",
        "/acceptable-use-policy",
        "/aup",
        "/docs/{path:path}",
        "/contact",
        "/report",
        # Legacy
        "/",
        "/emoji",
        "/result/{short_code}",
        "/{short_code}+",
        "/metric",
        "/stats",
        "/stats/",
        "/stats/{short_code}",
        "/export/{short_code}/{fmt}",
        # Redirect (must be last)
        "/{short_code}",
        "/{short_code}/password",
    ]
    for path in expected_paths:
        assert path in paths, f"Missing route: {path}"


def test_route_methods_correct(smoke_app: FastAPI) -> None:
    """Verify HTTP methods for key endpoints."""
    pairs = _get_path_method_pairs(smoke_app)

    expected_methods = [
        ("/health", "GET"),
        ("/auth/login", "POST"),
        ("/auth/register", "POST"),
        ("/auth/refresh", "POST"),
        ("/auth/logout", "POST"),
        ("/auth/me", "GET"),
        ("/auth/set-password", "POST"),
        ("/api/v1/shorten", "POST"),
        ("/api/v1/urls", "GET"),
        ("/api/v1/urls/{url_id}", "PATCH"),
        ("/api/v1/urls/{url_id}", "DELETE"),
        ("/api/v1/urls/{url_id}/status", "PATCH"),
        ("/api/v1/urls/bulk/delete", "POST"),
        ("/api/v1/urls/bulk/status", "POST"),
        ("/api/v1/urls/bulk/expiry", "POST"),
        ("/api/v1/urls/bulk/domain", "POST"),
        ("/api/v1/stats", "GET"),
        ("/api/v1/export", "GET"),
        ("/api/v1/keys", "POST"),
        ("/api/v1/keys", "GET"),
        ("/api/v1/keys/{key_id}", "DELETE"),
        ("/api/v1/reports", "POST"),
        ("/api/v1/contact", "POST"),
        ("/oauth/providers", "GET"),
        ("/oauth/providers/{provider_name}/unlink", "DELETE"),
        ("/oauth/{provider}", "GET"),
        ("/oauth/{provider}/callback", "GET"),
        ("/oauth/{provider}/link", "GET"),
        ("/{short_code}", "GET"),
        ("/{short_code}", "HEAD"),
        ("/{short_code}/password", "POST"),
        ("/contact", "GET"),
        ("/contact", "POST"),
        ("/report", "GET"),
        ("/report", "POST"),
        ("/dashboard/profile-pictures", "GET"),
        ("/dashboard/profile-pictures", "POST"),
        ("/", "GET"),
        ("/", "POST"),
    ]
    for path, method in expected_methods:
        assert (path, method) in pairs, f"Missing ({method} {path})"


def test_redirect_route_registered_last(smoke_app: FastAPI) -> None:
    """The catch-all /{short_code} redirect route must be the LAST API route."""
    api_routes = _get_api_routes(smoke_app)
    # Find routes with path "/{short_code}" — the redirect (GET/HEAD) should be last
    _ = api_routes[-1]
    # The very last route could be /{short_code}/password or /{short_code}
    # Both redirect_routes endpoints should be at the end
    last_two_paths = [r.path for r in api_routes[-2:]]
    assert "/{short_code}" in last_two_paths, (
        f"/{'{short_code}'} not in last two routes: {last_two_paths}"
    )
