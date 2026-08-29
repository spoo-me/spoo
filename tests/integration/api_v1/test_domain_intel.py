"""Tests for GET /api/v1/domain-intel (host records endpoint)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from dependencies import get_current_user
from infrastructure.cache.meta_fetch_cache import MetaFetchCache
from infrastructure.safe_fetch import FetchHardError
from services.domain_intel_service import DomainIntelService, _cert_summary

from .conftest import _build_test_app

# One registrar entity in vCard form, as the registries actually answer.
RDAP_BODY = {
    "events": [
        {"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"},
        {"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"},
    ],
    "entities": [
        {
            "roles": ["registrar"],
            "vcardArray": ["vcard", [["fn", {}, "text", "Example Registrar"]]],
        }
    ],
}

CERT = {
    "issuer": ((("organizationName", "Example CA"),),),
    "subject": ((("commonName", "dest.example"),),),
    "notBefore": "Jan  1 00:00:00 2026 GMT",
    "notAfter": "Jan  1 00:00:00 2027 GMT",
    "subjectAltName": (("DNS", "dest.example"), ("DNS", "www.dest.example")),
}


def _service(*, rdap_status: int = 200) -> DomainIntelService:
    """The real service over faked I/O — the RDAP and certificate parsing
    is the fiddly part and stays under test."""
    http = MagicMock()
    http.get = AsyncMock(
        return_value=MagicMock(status_code=rdap_status, json=lambda: RDAP_BODY)
    )
    return DomainIntelService(MetaFetchCache(None), http)


def _app(service: DomainIntelService):
    app = _build_test_app({get_current_user: lambda: None})
    app.state.domain_intel_service = service
    return app


def _patched(service: DomainIntelService, **kwargs):
    """Guard + DNS + TLS faked; everything else is the real code path."""
    defaults = {
        "resolve_public_ip": AsyncMock(return_value="93.184.216.34"),
        "_dns": AsyncMock(return_value={"a": ["93.184.216.34"], "mx": []}),
        "_tls": AsyncMock(return_value=None),
    }
    defaults.update(kwargs)
    return (
        patch(
            "services.domain_intel_service.resolve_public_ip",
            defaults["resolve_public_ip"],
        ),
        patch.object(DomainIntelService, "_dns", defaults["_dns"]),
        patch.object(DomainIntelService, "_tls", defaults["_tls"]),
    )


def test_domain_intel_parses_rdap_registrar_and_age():
    service = _service()
    guard, dns, tls = _patched(service)
    with guard, dns, tls, TestClient(_app(service)) as client:
        resp = client.get("/api/v1/domain-intel", params={"host": "dest.example"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["whois"]["registrar"] == "Example Registrar"
    assert data["whois"]["age_days"] > 2000
    assert data["dns"]["a"] == ["93.184.216.34"]


def test_certificate_summary_maps_the_wire_shape():
    summary = _cert_summary(CERT)
    assert summary["issuer"] == "Example CA"
    assert summary["subject"] == "dest.example"
    assert summary["sans"] == ["dest.example", "www.dest.example"]
    assert summary["valid_to"] == "Jan  1 00:00:00 2027 GMT"
    assert isinstance(summary["days_left"], int)


def test_domain_intel_rejects_host_resolving_into_private_space():
    """The SSRF guard runs before any probe: a public hostname pointing at
    private space must never reach the TLS handshake."""
    service = _service()
    tls = AsyncMock(return_value=None)
    with (
        patch(
            "services.domain_intel_service.resolve_public_ip",
            AsyncMock(
                side_effect=FetchHardError("host resolves to a non-public address")
            ),
        ),
        patch.object(DomainIntelService, "_tls", tls),
        TestClient(_app(service), raise_server_exceptions=False) as client,
    ):
        resp = client.get("/api/v1/domain-intel", params={"host": "127.0.0.1.nip.io"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "unfetchable"
    tls.assert_not_called()


def test_domain_intel_survives_an_rdap_miss():
    service = _service(rdap_status=404)
    guard, dns, tls = _patched(service)
    with guard, dns, tls, TestClient(_app(service)) as client:
        resp = client.get("/api/v1/domain-intel", params={"host": "dest.example"})
    assert resp.status_code == 200
    assert resp.json()["whois"] is None


def test_domain_intel_rejects_garbage_host():
    with TestClient(_app(_service()), raise_server_exceptions=False) as client:
        resp = client.get("/api/v1/domain-intel", params={"host": "not a host!"})
    assert resp.status_code == 400
