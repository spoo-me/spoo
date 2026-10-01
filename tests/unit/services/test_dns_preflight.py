"""Unit tests for the DNS preflight check + CF-DNS detector."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.dns_preflight import check_cname, uses_cloudflare_dns


def _stub_query(cname_per_resolver=None, a_per_fqdn=None):
    """Build a `_query` side_effect that returns canned CNAME / A answers."""
    cname_per_resolver = cname_per_resolver or {}
    a_per_fqdn = a_per_fqdn or {}

    async def _impl(fqdn, rtype, ns):
        if rtype == "CNAME":
            # Default: return whatever each resolver was configured for.
            ip = ns[0]
            return cname_per_resolver.get(ip, [])
        if rtype == "A":
            return list(a_per_fqdn.get(fqdn, []))
        return []

    return _impl


class TestCheckCname:
    @pytest.mark.asyncio
    async def test_pass_when_one_resolver_sees_expected_target(self):
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(
                cname_per_resolver={"1.1.1.1": ["customers.spoo.me"]},
            ),
        ):
            result = await check_cname("links.acme.com", "customers.spoo.me")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_normalises_trailing_dots(self):
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(
                cname_per_resolver={
                    "1.1.1.1": ["customers.spoo.me"],
                    "8.8.8.8": ["customers.spoo.me"],
                },
            ),
        ):
            result = await check_cname("links.acme.com.", "customers.spoo.me.")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_unknown_record_yields_friendly_propagation_message(self):
        # CNAME absent everywhere + no A records to fall back on.
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(),
        ):
            result = await check_cname("links.acme.com", "customers.spoo.me")
        assert result.ok is False
        assert "Try again" in result.reason

    @pytest.mark.asyncio
    async def test_wrong_target_yields_diagnostic_message(self):
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(
                cname_per_resolver={
                    "1.1.1.1": ["someoneelse.example"],
                    "8.8.8.8": ["someoneelse.example"],
                },
            ),
        ):
            result = await check_cname("links.acme.com", "customers.spoo.me")
        assert result.ok is False
        assert "someoneelse.example" in result.reason
        assert "customers.spoo.me" in result.reason

    @pytest.mark.asyncio
    async def test_transport_errors_treated_as_unpropagated(self):
        async def _all_none(fqdn, rtype, ns):
            return None

        with patch("app.services.dns_preflight._query", side_effect=_all_none):
            result = await check_cname("links.acme.com", "customers.spoo.me")
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_apex_flattening_passes_via_a_record_match(self):
        # CNAME hidden by the DNS provider's flattening; A records overlap
        # the expected target's A set — accept.
        cf_ips = ["104.21.84.220", "172.67.197.99"]
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(
                a_per_fqdn={
                    "acme.com": cf_ips,
                    "customers.spoo.me": cf_ips,
                },
            ),
        ):
            result = await check_cname("acme.com", "customers.spoo.me")
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_apex_a_records_mismatch_fails(self):
        with patch(
            "app.services.dns_preflight._query",
            side_effect=_stub_query(
                a_per_fqdn={
                    "acme.com": ["1.2.3.4"],
                    "customers.spoo.me": ["104.21.84.220"],
                },
            ),
        ):
            result = await check_cname("acme.com", "customers.spoo.me")
        assert result.ok is False
        assert "Try again" in result.reason


class TestUsesCloudflareDns:
    @pytest.mark.asyncio
    async def test_detects_cf_ns_at_apex(self):
        class _RData:
            def __init__(self, target: str) -> None:
                self.target = target

        async def _fake_resolve(name, rrtype):
            assert rrtype == "NS"
            if name == "acme.com":
                return [_RData("kara.ns.cloudflare.com.")]
            raise __import__("dns.resolver", fromlist=["NoAnswer"]).NoAnswer()

        with patch(
            "dns.asyncresolver.Resolver.resolve",
            side_effect=_fake_resolve,
        ):
            assert await uses_cloudflare_dns("links.acme.com") is True

    @pytest.mark.asyncio
    async def test_returns_false_for_non_cf_ns(self):
        class _RData:
            def __init__(self, target: str) -> None:
                self.target = target

        async def _fake_resolve(name, rrtype):
            if name == "acme.com":
                return [_RData("ns1.somewhere-else.net.")]
            raise __import__("dns.resolver", fromlist=["NoAnswer"]).NoAnswer()

        with patch(
            "dns.asyncresolver.Resolver.resolve",
            side_effect=_fake_resolve,
        ):
            assert await uses_cloudflare_dns("links.acme.com") is False
