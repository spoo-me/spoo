"""Unit tests for TenantMiddleware (PR4: strict allowlist routing)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from middleware.tenant import TenantMiddleware
from schemas.enums.domain_status import DomainStatus
from services.tenant_resolver.protocol import TenantInfo


def _app(
    resolver=None,
    omit_resolver: bool = False,
    custom_domains_enabled: bool = True,
) -> Starlette:
    async def root(request):
        tenant = getattr(request.state, "tenant", "<unset>")
        if tenant is None:
            return PlainTextResponse("none")
        if tenant == "<unset>":
            return PlainTextResponse("unset")
        return PlainTextResponse(tenant.fqdn)

    async def alias(request):
        tenant = getattr(request.state, "tenant", None)
        body = tenant.fqdn if tenant else "anon"
        return PlainTextResponse(body)

    app = Starlette(
        routes=[
            Route("/", root),
            Route("/{alias:str}", alias),
        ]
    )
    if not omit_resolver:
        app.state.tenant_resolver = resolver
    app.add_middleware(
        TenantMiddleware, custom_domains_enabled=custom_domains_enabled
    )
    return app


def _custom_tenant(
    fqdn: str = "links.acme.com",
    *,
    status: DomainStatus = DomainStatus.ACTIVE,
    root_redirect: str | None = None,
    not_found_redirect: str | None = None,
    custom_robots_txt: str | None = None,
) -> TenantInfo:
    return TenantInfo(
        domain_id=None,
        fqdn=fqdn,
        owner_id=None,
        status=status,
        is_system_default=False,
        root_redirect=root_redirect,
        not_found_redirect=not_found_redirect,
        custom_robots_txt=custom_robots_txt,
    )


def _system_tenant(fqdn: str = "spoo.me") -> TenantInfo:
    return TenantInfo(
        domain_id=None,
        fqdn=fqdn,
        owner_id=None,
        status=DomainStatus.ACTIVE,
        is_system_default=True,
    )


def _client(resolver):
    from starlette.testclient import TestClient

    return TestClient(_app(resolver))


class TestTenantMiddleware:
    def test_no_tenant_resolver_bypasses_middleware(self):
        from starlette.testclient import TestClient

        with TestClient(_app(omit_resolver=True)) as client:
            r = client.get("/", headers={"host": "example.com"})
        assert r.status_code == 200
        assert r.text == "unset"

    def test_unknown_host_returns_html_404(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=None)
        with _client(resolver) as client:
            r = client.get("/", headers={"host": "bogus.example.com"})
        assert r.status_code == 404
        assert "text/html" in r.headers.get("content-type", "")
        # Shared tenant-error page; unknown host has no tenant fqdn so the
        # message stays generic. Jinja auto-escapes the apostrophe.
        assert "This URL doesn" in r.text and "t exist" in r.text
        assert "Not found" in r.text

    def test_unknown_host_passes_through_when_custom_domains_disabled(self):
        """Self-host mode: hosts the app was never told about (IPs, internal
        DNS, reverse-proxy names) serve the full app surface — the sitewide
        404 is a SaaS-mode concern only."""
        from starlette.testclient import TestClient

        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=None)
        app = _app(resolver, custom_domains_enabled=False)
        with TestClient(app) as client:
            r = client.get("/", headers={"host": "my-vps.example.com"})
        assert r.status_code == 200
        assert r.text == "none"  # state.tenant is set to None, not left unset

    def test_known_custom_tenant_still_resolves_when_disabled(self):
        """Flipping the feature off must not break live redirects on
        already-registered domains."""
        from starlette.testclient import TestClient

        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        app = _app(resolver, custom_domains_enabled=False)
        with TestClient(app) as client:
            r = client.get("/abc123", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert r.text == "links.acme.com"

    def test_system_tenant_root_passes_through(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_system_tenant())
        with _client(resolver) as client:
            r = client.get("/", headers={"host": "spoo.me"})
        assert r.status_code == 200
        assert r.text == "spoo.me"
        # System default: no noindex stamp on responses (existing redirect
        # route handles its own header).
        assert "X-Robots-Tag" not in r.headers

    def test_system_tenant_alias_passes_through(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_system_tenant())
        with _client(resolver) as client:
            r = client.get("/abc", headers={"host": "spoo.me"})
        assert r.status_code == 200
        assert r.text == "spoo.me"

    def test_loopback_host_bypasses_resolver(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=AssertionError("should not be called"))
        with _client(resolver) as client:
            r = client.get("/", headers={"host": "localhost"})
        assert r.status_code == 200
        assert r.text == "none"

    def test_strips_port_before_resolving(self):
        resolver = MagicMock()
        captured: dict[str, str] = {}

        async def _capture(h):
            captured["host"] = h
            return _system_tenant(fqdn=h)

        resolver.resolve = _capture
        with _client(resolver) as client:
            client.get("/", headers={"host": "links.acme.com:8443"})
        assert captured["host"] == "links.acme.com"

    def test_ipv6_bracketed_host_is_normalised(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=AssertionError("should not be called"))
        with _client(resolver) as client:
            r = client.get("/", headers={"host": "[::1]:8000"})
        assert r.status_code == 200
        assert r.text == "none"


class TestCustomTenantRouting:
    """Allowlist routing on custom tenants — strict 404 on operator surface."""

    def test_custom_root_returns_404(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        with _client(resolver) as client:
            r = client.get("/", headers={"host": "links.acme.com"})
        assert r.status_code == 404
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_custom_alias_passes_through(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        with _client(resolver) as client:
            r = client.get("/abc123", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert r.text == "links.acme.com"
        # X-Robots-Tag stamped on the response by middleware
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_custom_alias_with_password_subpath_allowed(self):
        # Password verification is POST-only — GET on `/<alias>/password` must
        # 404 like any other disallowed method/path combination.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        from starlette.testclient import TestClient

        async def password_handler(request):
            return PlainTextResponse("password-ok")

        app = Starlette(
            routes=[Route("/{alias}/password", password_handler, methods=["POST"])]
        )
        app.state.tenant_resolver = resolver
        app.add_middleware(TenantMiddleware)

        with TestClient(app) as client:
            ok = client.post("/abc/password", headers={"host": "links.acme.com"})
            bad = client.get("/abc/password", headers={"host": "links.acme.com"})
        assert ok.status_code == 200
        assert bad.status_code == 404

    def test_custom_robots_txt_inline_disallow(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        with _client(resolver) as client:
            r = client.get("/robots.txt", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert "Disallow: /" in r.text
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_custom_favicon_passes_through(self):
        # Build app with explicit /favicon.ico BEFORE the catch-all /{alias}
        # so Starlette's left-to-right matcher resolves to the static handler.
        from starlette.testclient import TestClient

        async def favicon(request):
            return PlainTextResponse("favicon-bytes")

        async def alias(request):
            return PlainTextResponse("alias-fallback")

        app = Starlette(
            routes=[
                Route("/favicon.ico", favicon),
                Route("/{alias:str}", alias),
            ]
        )
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        app.state.tenant_resolver = resolver
        app.add_middleware(TenantMiddleware)

        with TestClient(app) as client:
            r = client.get("/favicon.ico", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert r.text == "favicon-bytes"

    def test_custom_stats_suffix_blocked(self):
        # `/<alias>+` is the stats page on spoo.me. Blocked on custom tenants.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        with _client(resolver) as client:
            r = client.get("/abc+", headers={"host": "links.acme.com"})
        assert r.status_code == 404

    def test_custom_operator_paths_blocked(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=_custom_tenant())
        blocked_paths = (
            "/api/v1/custom-domains",
            "/api/v1/shorten",
            "/dashboard",
            "/dashboard/links",
            "/auth/login",
            "/oauth/google/start",
            "/health",
            "/report",
            "/about",
            "/contact",
            "/api-docs",
        )
        with _client(resolver) as client:
            for path in blocked_paths:
                r = client.get(path, headers={"host": "links.acme.com"})
                assert r.status_code == 404, (
                    f"expected 404 on {path}, got {r.status_code}"
                )
                assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"


class TestAliasPatternEmojiCoverage:
    """The coarse alias gate must pass every emoji shape a code can arrive
    as (decoded unicode incl. combiners, and percent-encoded) — policy
    enforcement lives at creation, not in the middleware."""

    @pytest.mark.parametrize(
        "path",
        [
            "/⭐",  # 2B00 block (outside plane 1 — the old pattern missed it)
            "/☕",  # Misc Symbols (2600)
            "/✂",  # Dingbats (2700)
            "/🀄",  # Mahjong (1F004)
            "/🈚",  # Enclosed ideographs (1F200)
            "/🟧",  # Geometric Extended (1F7E0)
            "/👍🏽👍🏽",  # skin-tone modifier sequences
            "/🚀🔥🎉",
            "/🏳️‍🌈",  # ZWJ + VS16 (legacy-lenient forms must reach the router)
            "/1️⃣",  # keycap combiner
            "/🇺🇸",  # regional indicators
            "/🏴󠁧󠁢󠁥󠁮󠁧󠁿",  # tag sequence
            "/%F0%9F%91%8D",  # percent-encoded emoji
            "/⭐/password",
        ],
    )
    def test_emoji_paths_match(self, path):
        from middleware.tenant import _ALIAS_PATTERN

        assert _ALIAS_PATTERN.match(path), f"expected alias match for {path!r}"

    @pytest.mark.parametrize(
        "path",
        ["/", "/⭐+", "/a b", "/café", "/⭐/extra", "/mylink/password/x"],
    )
    def test_non_alias_paths_rejected(self, path):
        from middleware.tenant import _ALIAS_PATTERN

        assert not _ALIAS_PATTERN.match(path), f"expected no match for {path!r}"


class TestRoutingConfig:
    """PR4.5 per-domain routing config — root_redirect / not_found_redirect / custom_robots_txt."""

    def test_root_redirect_302_when_active_and_set(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(root_redirect="https://acme.com/landing")
        )
        with _client(resolver) as client:
            r = client.get(
                "/", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 302
        assert r.headers["location"] == "https://acme.com/landing"
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_root_redirect_ignored_on_non_active(self):
        # Suspended/pending/revoked domains 404 on /, not redirect — stops a
        # stale config from firing after the owner takes the domain down.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(
                status=DomainStatus.SUSPENDED,
                root_redirect="https://acme.com/landing",
            )
        )
        with _client(resolver) as client:
            r = client.get(
                "/", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 404

    def test_root_redirect_ignored_on_post(self):
        # `/` only redirects on GET/HEAD — POST/PUT/DELETE 404 to avoid
        # silently dropping a request body to an unrelated URL.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(root_redirect="https://acme.com/")
        )
        with _client(resolver) as client:
            r = client.post(
                "/", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 404

    def test_root_without_redirect_returns_404_not_not_found_redirect(self):
        # The `/` surface is distinct from "any other unmatched path". Owner
        # who configured only not_found_redirect must still see 404 on root.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(not_found_redirect="https://acme.com/404")
        )
        with _client(resolver) as client:
            r = client.get(
                "/", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 404

    def test_not_found_redirect_302_on_disallowed_path(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(not_found_redirect="https://acme.com/404")
        )
        with _client(resolver) as client:
            r = client.get(
                "/about", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 302
        assert r.headers["location"] == "https://acme.com/404"

    def test_not_found_redirect_ignored_on_non_get(self):
        # Body-bearing methods don't redirect — caller's payload would be
        # silently dropped, which looks broken.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(not_found_redirect="https://acme.com/404")
        )
        with _client(resolver) as client:
            r = client.post(
                "/about", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 404

    def test_not_found_redirect_ignored_on_non_active(self):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(
                status=DomainStatus.PENDING,
                not_found_redirect="https://acme.com/404",
            )
        )
        with _client(resolver) as client:
            r = client.get(
                "/about", headers={"host": "links.acme.com"}, follow_redirects=False
            )
        assert r.status_code == 404

    def test_custom_robots_txt_served_when_set(self):
        body = "User-agent: *\nAllow: /\nSitemap: https://acme.com/sitemap.xml\n"
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(custom_robots_txt=body)
        )
        with _client(resolver) as client:
            r = client.get("/robots.txt", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert r.text == body
        # Header stays put regardless of robots.txt content — short URLs are
        # pure redirects, never indexable.
        assert r.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"

    def test_custom_robots_txt_ignored_on_non_active(self):
        body = "User-agent: *\nAllow: /\n"
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(
                status=DomainStatus.SUSPENDED,
                custom_robots_txt=body,
            )
        )
        with _client(resolver) as client:
            r = client.get("/robots.txt", headers={"host": "links.acme.com"})
        assert r.status_code == 200
        assert "Disallow: /" in r.text
        assert "Allow: /" not in r.text

    def test_alias_still_passes_through_with_routing_config(self):
        # Routing config only changes behavior on / and non-alias paths.
        # Real alias requests must still flow to the redirect router.
        resolver = MagicMock()
        resolver.resolve = AsyncMock(
            return_value=_custom_tenant(
                root_redirect="https://acme.com/landing",
                not_found_redirect="https://acme.com/404",
                custom_robots_txt="User-agent: *\nAllow: /\n",
            )
        )
        with _client(resolver) as client:
            r = client.get(
                "/abc123",
                headers={"host": "links.acme.com"},
                follow_redirects=False,
            )
        assert r.status_code == 200
        assert r.text == "links.acme.com"
