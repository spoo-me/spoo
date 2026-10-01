"""Unit tests for AppError hierarchy."""

import pytest

from app.errors import (
    AuthenticationError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)


class TestAppErrorSubclasses:
    def test_validation_error(self):
        e = ValidationError("bad input")
        assert e.status_code == 400
        assert e.error_code == "validation_error"
        assert e.message == "bad input"

    def test_authentication_error(self):
        e = AuthenticationError("not authenticated")
        assert e.status_code == 401
        assert e.error_code == "authentication_error"

    def test_forbidden_error(self):
        e = ForbiddenError("not allowed")
        assert e.status_code == 403
        assert e.error_code == "forbidden"

    def test_not_found_error(self):
        e = NotFoundError("resource missing")
        assert e.status_code == 404
        assert e.error_code == "not_found"

    def test_conflict_error(self):
        e = ConflictError("already exists")
        assert e.status_code == 409
        assert e.error_code == "conflict"

    def test_rate_limit_error(self):
        e = RateLimitError("slow down")
        assert e.status_code == 429
        assert e.error_code == "rate_limit_exceeded"


class TestAppErrorToDict:
    def test_basic(self):
        e = NotFoundError("url not found")
        assert e.to_dict() == {"error": "url not found", "code": "not_found"}

    @pytest.mark.parametrize(
        "kwargs, key, value",
        [
            ({"field": "alias"}, "field", "alias"),
            ({"details": {"min": 1, "max": 10}}, "details", {"min": 1, "max": 10}),
        ],
        ids=["with_field", "with_details"],
    )
    def test_optional_key_present(self, kwargs, key, value):
        e = ValidationError("invalid", **kwargs)
        assert e.to_dict()[key] == value

    def test_no_optional_keys_when_absent(self):
        d = NotFoundError("missing").to_dict()
        assert "field" not in d
        assert "details" not in d
