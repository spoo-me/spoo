"""Unit tests for bulk operation DTOs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pydantic import ValidationError as PydanticValidationError

from app.schemas.dto.requests.bulk import (
    BULK_MAX_IDS,
    BulkDeleteUrlsRequest,
    BulkIdsRequest,
    BulkUpdateExpiryRequest,
    BulkUpdateStatusRequest,
)
from app.schemas.dto.responses.bulk import (
    BulkOperationSummary,
    BulkUrlOperationResponse,
    BulkUrlResultRow,
)
from app.schemas.models.url import UrlStatus

VALID_ID = "665f0c2f9e7a4b1d2c3d4e5f"


class TestBulkIdsRequest:
    def test_valid_ids_accepted(self):
        req = BulkIdsRequest(ids=[VALID_ID])
        assert req.object_ids() == [ObjectId(VALID_ID)]

    def test_empty_ids_rejected(self):
        with pytest.raises(PydanticValidationError):
            BulkIdsRequest(ids=[])

    def test_over_cap_rejected(self):
        ids = [f"{i:024x}" for i in range(BULK_MAX_IDS + 1)]
        with pytest.raises(PydanticValidationError):
            BulkIdsRequest(ids=ids)

    def test_cap_boundary_accepted(self):
        ids = [f"{i:024x}" for i in range(BULK_MAX_IDS)]
        assert len(BulkIdsRequest(ids=ids).ids) == BULK_MAX_IDS

    def test_malformed_id_rejects_whole_envelope(self):
        with pytest.raises(PydanticValidationError) as exc:
            BulkIdsRequest(ids=[VALID_ID, "not-hex"])
        assert "not-hex" in str(exc.value)

    def test_uppercase_hex_rejected(self):
        # Path params on the single-item routes are lowercase-hex only;
        # the bulk envelope matches.
        with pytest.raises(PydanticValidationError):
            BulkIdsRequest(ids=[VALID_ID.upper()])

    def test_duplicates_pass_validation_and_preserve_order(self):
        # Dedupe is service-side (first occurrence wins) so the report
        # can still answer every requested id.
        req = BulkIdsRequest(ids=[VALID_ID, VALID_ID])
        assert req.object_ids() == [ObjectId(VALID_ID), ObjectId(VALID_ID)]


class TestBulkOpRequests:
    def test_delete_request_is_ids_only(self):
        assert BulkDeleteUrlsRequest(ids=[VALID_ID]).ids == [VALID_ID]

    def test_status_literal_enforced(self):
        req = BulkUpdateStatusRequest(ids=[VALID_ID], status="INACTIVE")
        assert req.status == UrlStatus.INACTIVE
        with pytest.raises(PydanticValidationError):
            BulkUpdateStatusRequest(ids=[VALID_ID], status="BLOCKED")
        with pytest.raises(PydanticValidationError):
            BulkUpdateStatusRequest(ids=[VALID_ID], status="EXPIRED")

    def test_status_is_required(self):
        with pytest.raises(PydanticValidationError):
            BulkUpdateStatusRequest(ids=[VALID_ID])

    def test_expiry_accepts_epoch_seconds(self):
        req = BulkUpdateExpiryRequest(ids=[VALID_ID], expire_after=1767225600)
        assert req.expire_after == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_expiry_accepts_iso8601(self):
        req = BulkUpdateExpiryRequest(
            ids=[VALID_ID], expire_after="2027-01-01T00:00:00+00:00"
        )
        assert req.expire_after is not None
        assert req.expire_after.year == 2027

    def test_expiry_null_clears(self):
        assert (
            BulkUpdateExpiryRequest(ids=[VALID_ID], expire_after=None).expire_after
            is None
        )

    def test_expiry_field_is_required(self):
        # Omitting expire_after is a malformed request, not an implicit
        # clear — clearing must be an explicit null.
        with pytest.raises(PydanticValidationError):
            BulkUpdateExpiryRequest(ids=[VALID_ID])

    def test_expiry_garbage_rejected(self):
        with pytest.raises(PydanticValidationError):
            BulkUpdateExpiryRequest(ids=[VALID_ID], expire_after="soonish")


class TestBulkResponseShape:
    def test_row_serializes_snake_case_error_code(self):
        row = BulkUrlResultRow(
            id=VALID_ID, alias="promo", ok=False, error_code="conflict", error="taken"
        )
        payload = row.model_dump()
        assert payload["error_code"] == "conflict"
        assert "errorCode" not in payload

    def test_ok_row_defaults(self):
        row = BulkUrlResultRow(id=VALID_ID, alias="promo", ok=True)
        assert row.error_code is None
        assert row.error is None

    def test_envelope_shape(self):
        resp = BulkUrlOperationResponse(
            summary=BulkOperationSummary(total=1, succeeded=1, failed=0),
            results=[BulkUrlResultRow(id=VALID_ID, alias="a", ok=True)],
        )
        payload = resp.model_dump()
        assert payload["summary"] == {"total": 1, "succeeded": 1, "failed": 0}
        assert len(payload["results"]) == 1


class TestBulkMoveDomainRequest:
    def test_domain_is_normalised(self):
        from app.schemas.dto.requests.bulk import BulkMoveDomainRequest

        req = BulkMoveDomainRequest(ids=[VALID_ID], domain="Links.ACME.com.")
        assert req.domain == "links.acme.com"

    def test_null_and_empty_mean_system_default(self):
        from app.schemas.dto.requests.bulk import BulkMoveDomainRequest

        assert BulkMoveDomainRequest(ids=[VALID_ID], domain=None).domain is None
        assert BulkMoveDomainRequest(ids=[VALID_ID], domain="").domain is None

    def test_domain_field_is_required(self):
        # Omitting the target is a malformed request, not an implicit
        # move-to-default — that must be an explicit null.
        from app.schemas.dto.requests.bulk import BulkMoveDomainRequest

        with pytest.raises(PydanticValidationError):
            BulkMoveDomainRequest(ids=[VALID_ID])
