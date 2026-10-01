"""Tests for the custom-domain DTOs (PR4 restructure)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.schemas.dto.requests.custom_domain import UpdateCustomDomainRequest
from app.schemas.dto.responses.custom_domain import (
    CustomDomainDeleteResponse,
    CustomDomainListResponse,
    CustomDomainResponse,
    DnsRecord,
)
from app.schemas.enums.domain_status import DomainStatus, VerificationMethod
from app.schemas.models.custom_domain import CustomDomainDoc


def _doc(**overrides) -> CustomDomainDoc:
    base = {
        "_id": ObjectId(),
        "fqdn": "links.acme.com",
        "owner_id": ObjectId(),
        "status": DomainStatus.PENDING,
        "verification_method": VerificationMethod.CF_HTTP_DCV,
        "verification_token": "tok-123",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "dns_instructions": [
            {"type": "CNAME", "name": "links.acme.com", "value": "spoo.me"},
            {
                "type": "TXT",
                "name": "_cf-custom-hostname.links.acme.com",
                "value": "abc-uuid",
            },
        ],
        "setup_notes": ["Set DNS-only (grey cloud) if using CF DNS."],
    }
    base.update(overrides)
    return CustomDomainDoc.from_mongo(base)


class TestCustomDomainResponse:
    def test_structured_dns_records(self):
        r = CustomDomainResponse.from_doc(_doc())
        assert len(r.dns_records) == 2
        assert r.dns_records[0].type == "CNAME"
        assert r.dns_records[0].name == "links.acme.com"
        assert r.dns_records[0].value == "spoo.me"
        assert r.dns_records[1].type == "TXT"

    def test_setup_notes_passthrough(self):
        r = CustomDomainResponse.from_doc(_doc())
        assert r.setup_notes == ["Set DNS-only (grey cloud) if using CF DNS."]

    def test_no_verification_token_in_response(self):
        # Internal field — must not be exposed in the public DTO.
        r = CustomDomainResponse.from_doc(_doc())
        assert not hasattr(r, "verification_token")

    def test_no_setup_instructions_in_response(self):
        # Replaced by dns_records + setup_notes.
        r = CustomDomainResponse.from_doc(_doc())
        assert not hasattr(r, "setup_instructions")

    def test_empty_dns_records_when_doc_has_none(self):
        r = CustomDomainResponse.from_doc(_doc(dns_instructions=[]))
        assert r.dns_records == []

    def test_status_and_method_propagate(self):
        r = CustomDomainResponse.from_doc(_doc(status=DomainStatus.ACTIVE))
        assert r.status == DomainStatus.ACTIVE
        assert r.verification_method == VerificationMethod.CF_HTTP_DCV

    def test_timestamps_serialize_with_explicit_utc_offset(self):
        # Wire regression guard for the move to the shared UtcDatetime
        # type: naive Mongo read-backs are stamped UTC and the textual
        # form stays `+00:00`, exactly as the old field_serializer emitted.
        r = CustomDomainResponse.from_doc(
            _doc(created_at=datetime(2025, 1, 1), updated_at=datetime(2025, 1, 2))
        )
        dumped = r.model_dump()
        assert dumped["created_at"] == "2025-01-01T00:00:00+00:00"
        assert dumped["updated_at"] == "2025-01-02T00:00:00+00:00"
        assert dumped["last_verified_at"] is None


class TestCustomDomainListResponse:
    def test_paginated_shape(self):
        r = CustomDomainListResponse(
            items=[CustomDomainResponse.from_doc(_doc())],
            page=1,
            pageSize=20,
            total=1,
            hasNext=False,
        )
        # camelCase serialisation via aliases (consistent with UrlListResponse).
        dumped = r.model_dump(by_alias=True)
        assert dumped["pageSize"] == 20
        assert dumped["hasNext"] is False


class TestCustomDomainDeleteResponse:
    def test_no_cascade_zero_count(self):
        r = CustomDomainDeleteResponse(
            id="x", fqdn="links.acme.com", cascade=False, urls_deleted=0
        )
        assert r.cascade is False
        assert r.urls_deleted == 0

    def test_cascade_with_count(self):
        r = CustomDomainDeleteResponse(
            id="x", fqdn="links.acme.com", cascade=True, urls_deleted=42
        )
        assert r.cascade is True
        assert r.urls_deleted == 42


class TestDnsRecord:
    def test_types_constrained_to_known(self):
        # Loose validation by Literal — pydantic raises on unknown
        with pytest.raises(ValidationError):
            DnsRecord(type="MX", name="x", value="y")  # type: ignore[arg-type]


class TestUpdateCustomDomainRequest:
    def test_empty_body_has_no_fields_set(self):
        # Empty body = no-op upstream — service must distinguish this from
        # "all fields explicitly null".
        req = UpdateCustomDomainRequest()
        assert req.model_fields_set == set()

    def test_partial_field_set_only_includes_that_field(self):
        req = UpdateCustomDomainRequest(root_redirect="https://acme.com/landing")
        assert req.model_fields_set == {"root_redirect"}

    def test_explicit_null_is_in_fields_set(self):
        # Explicit `null` clears the stored value; field omitted leaves it
        # alone. The two must be distinguishable.
        req = UpdateCustomDomainRequest(root_redirect=None)
        assert "root_redirect" in req.model_fields_set

    def test_http_url_rejects_non_url_string(self):
        with pytest.raises(ValidationError):
            UpdateCustomDomainRequest(root_redirect="not-a-url")  # type: ignore[arg-type]

    def test_http_url_rejects_javascript_scheme(self):
        # HttpUrl only accepts http/https; javascript: would otherwise be a
        # dashboard-controlled XSS vector via the Location header.
        with pytest.raises(ValidationError):
            UpdateCustomDomainRequest(root_redirect="javascript:alert(1)")  # type: ignore[arg-type]

    def test_http_url_accepts_https(self):
        req = UpdateCustomDomainRequest(root_redirect="https://acme.com/")
        assert "root_redirect" in req.model_fields_set

    def test_http_url_accepts_http(self):
        req = UpdateCustomDomainRequest(not_found_redirect="http://acme.com/404")
        assert "not_found_redirect" in req.model_fields_set

    def test_robots_txt_size_cap(self):
        # 4097 chars — one past the cap.
        oversized = "x" * 4097
        with pytest.raises(ValidationError):
            UpdateCustomDomainRequest(custom_robots_txt=oversized)

    def test_robots_txt_at_cap_allowed(self):
        # Exactly 4096 chars is fine.
        req = UpdateCustomDomainRequest(custom_robots_txt="x" * 4096)
        assert req.custom_robots_txt is not None
        assert len(req.custom_robots_txt) == 4096

    def test_empty_string_robots_normalised_to_none(self):
        # Empty body in a form submission means "clear" semantically.
        req = UpdateCustomDomainRequest(custom_robots_txt="")
        assert req.custom_robots_txt is None
        # Still counts as "field was set" so service issues the clear.
        assert "custom_robots_txt" in req.model_fields_set

    def test_whitespace_only_robots_normalised_to_none(self):
        req = UpdateCustomDomainRequest(custom_robots_txt="   \n\t  ")
        assert req.custom_robots_txt is None


class TestCustomDomainDocRoutingFields:
    def test_routing_fields_default_to_none(self):
        # Backwards compatibility: existing docs without these fields
        # deserialise cleanly with None defaults.
        doc = _doc()
        assert doc.root_redirect is None
        assert doc.not_found_redirect is None
        assert doc.custom_robots_txt is None

    def test_empty_string_redirect_normalised_to_none_at_doc_level(self):
        # Belt-and-braces: even if Mongo somehow has an empty string (legacy
        # row, manual edit), the doc model normalises to None so middleware
        # `if tenant.root_redirect:` short-circuits the way it should.
        doc = _doc(root_redirect="", custom_robots_txt="  ")
        assert doc.root_redirect is None
        assert doc.custom_robots_txt is None

    def test_response_round_trip_carries_routing_fields(self):
        doc = _doc(
            status=DomainStatus.ACTIVE,
            root_redirect="https://acme.com/landing",
            not_found_redirect="https://acme.com/404",
            custom_robots_txt="User-agent: *\nAllow: /\n",
        )
        r = CustomDomainResponse.from_doc(doc)
        assert r.root_redirect == "https://acme.com/landing"
        assert r.not_found_redirect == "https://acme.com/404"
        assert r.custom_robots_txt == "User-agent: *\nAllow: /\n"
