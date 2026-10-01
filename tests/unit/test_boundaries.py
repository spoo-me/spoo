"""
Boundary tests for DTOs and service input validation.

Covers alias length, URL length, pagination edges, expire_after parsing,
and OTP code generation boundaries.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.dto.requests.auth import VerifyEmailRequest
from app.schemas.dto.requests.url import CreateUrlRequest, ListUrlsQuery
from app.shared.generators import generate_otp_code

# ── Alias Length Boundaries ──────────────────────────────────────────────────


class TestAliasLengthBoundaries:
    """CreateUrlRequest.alias min_length=3, max_length=16."""

    def test_alias_exactly_3_chars_accepted(self):
        req = CreateUrlRequest(long_url="https://example.com", alias="abc")
        assert req.alias == "abc"

    def test_alias_exactly_16_chars_accepted(self):
        alias = "a" * 16
        req = CreateUrlRequest(long_url="https://example.com", alias=alias)
        assert req.alias == alias

    def test_alias_2_chars_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest(long_url="https://example.com", alias="ab")

    def test_alias_17_chars_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest(long_url="https://example.com", alias="a" * 17)


# ── URL Length Boundaries ────────────────────────────────────────────────────


class TestUrlLengthBoundaries:
    """CreateUrlRequest.long_url max_length=8192."""

    def test_long_url_at_8192_chars_accepted(self):
        url = "https://example.com/" + "x" * (8192 - len("https://example.com/"))
        assert len(url) == 8192
        req = CreateUrlRequest(long_url=url)
        assert req.long_url == url

    def test_long_url_at_8193_chars_rejected(self):
        url = "https://example.com/" + "x" * (8193 - len("https://example.com/"))
        assert len(url) == 8193
        with pytest.raises(ValidationError):
            CreateUrlRequest(long_url=url)


# ── Pagination Edges ─────────────────────────────────────────────────────────


class TestPaginationEdges:
    """ListUrlsQuery page (ge=1) and page_size (ge=1, le=100)."""

    def test_page_1_accepted(self):
        q = ListUrlsQuery(page=1)
        assert q.page == 1

    def test_page_0_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery(page=0)

    def test_page_size_1_accepted(self):
        q = ListUrlsQuery(pageSize=1)
        assert q.page_size == 1

    def test_page_size_0_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery(pageSize=0)

    def test_page_size_100_accepted(self):
        q = ListUrlsQuery(pageSize=100)
        assert q.page_size == 100

    def test_page_size_101_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery(pageSize=101)


# ── expire_after Validation ──────────────────────────────────────────────────


class TestExpireAfterValidation:
    """CreateUrlRequest.expire_after accepts ISO 8601 strings and Unix timestamps."""

    def test_iso_8601_string_parsed_to_datetime(self):
        req = CreateUrlRequest(
            long_url="https://example.com",
            expire_after="2025-12-31T23:59:59Z",
        )
        assert isinstance(req.expire_after, datetime)
        assert req.expire_after.year == 2025
        assert req.expire_after.month == 12
        assert req.expire_after.day == 31

    def test_unix_timestamp_int_parsed_to_datetime(self):
        ts = 1735689599  # 2024-12-31T23:59:59Z
        req = CreateUrlRequest(long_url="https://example.com", expire_after=ts)
        assert isinstance(req.expire_after, datetime)
        assert req.expire_after.tzinfo is not None

    def test_invalid_string_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest(
                long_url="https://example.com",
                expire_after="not-a-date",
            )

    def test_none_accepted(self):
        req = CreateUrlRequest(long_url="https://example.com", expire_after=None)
        assert req.expire_after is None


# ── OTP Code Boundaries ─────────────────────────────────────────────────────


class TestOtpCodeBoundaries:
    """generate_otp_code length validation (1-128)."""

    def test_otp_length_6_accepted(self):
        code = generate_otp_code(length=6)
        assert len(code) == 6
        assert code.isdigit()

    def test_otp_length_0_rejected(self):
        with pytest.raises(ValueError, match="length must be between 1 and 128"):
            generate_otp_code(length=0)

    def test_otp_length_129_rejected(self):
        with pytest.raises(ValueError, match="length must be between 1 and 128"):
            generate_otp_code(length=129)


# ── VerifyEmailRequest Code Boundaries ───────────────────────────────────────


class TestVerifyEmailCodeBoundaries:
    """VerifyEmailRequest.code must be exactly 6 digits."""

    def test_valid_6_digit_code_accepted(self):
        req = VerifyEmailRequest(code="123456")
        assert req.code == "123456"

    def test_5_digit_code_rejected(self):
        with pytest.raises(ValidationError):
            VerifyEmailRequest(code="12345")

    def test_7_digit_code_rejected(self):
        with pytest.raises(ValidationError):
            VerifyEmailRequest(code="1234567")

    def test_non_numeric_code_rejected(self):
        with pytest.raises(ValidationError):
            VerifyEmailRequest(code="abcdef")
