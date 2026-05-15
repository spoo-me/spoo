"""Unit tests for common response DTOs."""

from __future__ import annotations

from schemas.dto.responses.common import ErrorResponse, HealthResponse


class TestErrorResponse:
    def test_minimal(self):
        d = ErrorResponse(error="not found", code="not_found").model_dump(
            exclude_none=True
        )
        assert d == {"error": "not found", "code": "not_found"}

    def test_with_field_and_details(self):
        r = ErrorResponse(
            error="bad input",
            code="validation_error",
            field="email",
            details={"hint": "must be valid email"},
        )
        d = r.model_dump()
        assert d["field"] == "email"
        assert d["details"] == {"hint": "must be valid email"}

    def test_without_optional_fields(self):
        d = ErrorResponse(error="fail", code="err").model_dump()
        assert d["field"] is None
        assert d["details"] is None


class TestHealthResponse:
    def test_serialization(self):
        r = HealthResponse(status="ok")
        d = r.model_dump()
        assert d["status"] == "ok"
