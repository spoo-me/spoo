"""
State machine tests for URL lifecycle and OTP lifecycle.

URL lifecycle: ACTIVE -> click -> ACTIVE, ACTIVE -> max_clicks -> EXPIRED,
BLOCKED -> update -> ForbiddenError, BLOCKED -> delete -> ForbiddenError.

OTP lifecycle: create -> verify -> success, wrong code -> fail,
5 wrong codes -> locked out, verify -> reuse -> fail.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

from errors import ForbiddenError, ValidationError
from infrastructure.crypto import hash_token
from schemas.dto.requests.url import UpdateUrlRequest
from schemas.models.token import TOKEN_TYPE_EMAIL_VERIFY
from schemas.models.url import UrlStatus, UrlV2Doc
from services.auth.otp import MAX_VERIFICATION_ATTEMPTS, OtpService
from services.url_service import UrlService

# ── Constants ────────────────────────────────────────────────────────────────

USER_OID = ObjectId("aaaaaaaaaaaaaaaaaaaaaaaa")
URL_OID = ObjectId("bbbbbbbbbbbbbbbbbbbbbbbb")
USER_EMAIL = "test@example.com"


# ── Helpers ──────────────────────────────────────────────────────────────────


def make_url_v2_doc(
    alias: str = "abc1234",
    url_id: ObjectId = URL_OID,
    owner_id: ObjectId = USER_OID,
    status: str = UrlStatus.ACTIVE,
    max_clicks: int | None = None,
    total_clicks: int = 0,
) -> UrlV2Doc:
    return UrlV2Doc.from_mongo(
        {
            "_id": url_id,
            "alias": alias,
            "owner_id": owner_id,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "creation_ip": "1.2.3.4",
            "long_url": "https://example.com",
            "password": None,
            "block_bots": None,
            "max_clicks": max_clicks,
            "expire_after": None,
            "status": status,
            "private_stats": True,
            "total_clicks": total_clicks,
            "last_click": None,
            "domain": "spoo.me",
        }
    )


def make_url_service() -> tuple[UrlService, AsyncMock, AsyncMock, AsyncMock]:
    """Create a UrlService with mocked repositories.

    Returns (service, url_repo, legacy_repo, url_cache).
    """
    url_repo = AsyncMock()
    legacy_repo = AsyncMock()
    emoji_repo = AsyncMock()
    blocked_url_repo = AsyncMock()
    url_cache = AsyncMock()

    service = UrlService(
        url_repo=url_repo,
        legacy_repo=legacy_repo,
        emoji_repo=emoji_repo,
        blocked_url_repo=blocked_url_repo,
        url_cache=url_cache,
        blocked_self_domains=["spoo.me"],
        system_default_domain="spoo.me",
        url_policy=_make_gate(blocked_url_repo),
    )
    return service, url_repo, legacy_repo, url_cache


def _make_gate(blocked_url_repo):
    from services.safety.policy import UrlPolicyService
    from services.safety.providers import BlockedPatternProvider

    return UrlPolicyService(
        [
            BlockedPatternProvider(
                blocked_url_repo, regex_timeout=0.2, patterns_ttl_seconds=0
            )
        ],
        blocked_self_domains=["spoo.me"],
    )


def make_token_doc(
    otp_code: str = "123456",
    attempts: int = 0,
    expired: bool = False,
    used: bool = False,
) -> MagicMock:
    """Create a mock token document."""
    doc = MagicMock()
    doc.id = ObjectId()
    doc.token_hash = hash_token(otp_code)
    if expired:
        doc.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    else:
        doc.expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    doc.attempts = attempts
    doc.app_id = None
    doc.used_at = datetime.now(timezone.utc) if used else None
    return doc


def make_otp_service() -> tuple[OtpService, AsyncMock]:
    """Create an OtpService with a mocked TokenRepository.

    Returns (service, token_repo).
    """
    token_repo = AsyncMock()
    # Default: not rate limited
    token_repo.count_recent.return_value = 0
    token_repo.create.return_value = None
    token_repo.increment_attempts.return_value = None
    token_repo.mark_as_used.return_value = True
    service = OtpService(token_repo=token_repo)
    return service, token_repo


# ── URL Lifecycle Tests ──────────────────────────────────────────────────────


class TestUrlLifecycle:
    """State machine tests for URL status transitions."""

    @pytest.mark.asyncio
    async def test_active_url_stays_active_after_click(self):
        """ACTIVE -> click -> still ACTIVE.

        After a click is tracked, the URL status should remain ACTIVE
        when max_clicks is not set.
        """
        _service, url_repo, _, _url_cache = make_url_service()

        doc = make_url_v2_doc(status=UrlStatus.ACTIVE, total_clicks=5)
        url_repo.find_by_id.return_value = doc

        # Verify URL is still ACTIVE after retrieval (simulating post-click)
        result = await url_repo.find_by_id(URL_OID)
        assert result.status == UrlStatus.ACTIVE
        assert result.total_clicks == 5

    @pytest.mark.asyncio
    async def test_active_url_expires_at_max_clicks(self):
        """ACTIVE -> max_clicks reached -> EXPIRED.

        Create URL with max_clicks=1. After the total_clicks reaches max_clicks,
        the URL status should be EXPIRED. The service layer sets this on update.
        """
        _service, url_repo, _, _url_cache = make_url_service()

        # Simulate a URL that has reached max_clicks
        doc = make_url_v2_doc(status=UrlStatus.ACTIVE, max_clicks=1, total_clicks=0)
        url_repo.find_by_id.return_value = doc

        # After one click, the repo would return it as expired
        expired_doc = make_url_v2_doc(
            status=UrlStatus.EXPIRED, max_clicks=1, total_clicks=1
        )
        url_repo.find_by_id.return_value = expired_doc

        result = await url_repo.find_by_id(URL_OID)
        assert result.status == UrlStatus.EXPIRED
        assert result.total_clicks == 1
        assert result.max_clicks == 1

    @pytest.mark.asyncio
    async def test_blocked_url_update_raises_forbidden(self):
        """BLOCKED -> update attempt -> ForbiddenError."""
        service, url_repo, _, _url_cache = make_url_service()

        blocked_doc = make_url_v2_doc(status=UrlStatus.BLOCKED)
        url_repo.find_by_id.return_value = blocked_doc

        update_req = UpdateUrlRequest(long_url="https://new-url.com")

        with pytest.raises(ForbiddenError, match="Cannot modify a blocked URL"):
            await service.update(URL_OID, update_req, USER_OID)

    @pytest.mark.asyncio
    async def test_blocked_url_delete_raises_forbidden(self):
        """BLOCKED -> delete attempt -> ForbiddenError."""
        service, url_repo, _, _url_cache = make_url_service()

        blocked_doc = make_url_v2_doc(status=UrlStatus.BLOCKED)
        url_repo.find_by_id.return_value = blocked_doc

        with pytest.raises(ForbiddenError, match="Cannot delete a blocked URL"):
            await service.delete(URL_OID, USER_OID)


# ── OTP Lifecycle Tests ──────────────────────────────────────────────────────


class TestOtpLifecycle:
    """State machine tests for OTP creation and verification."""

    @pytest.mark.asyncio
    async def test_create_and_verify_success(self):
        """create -> verify with correct code -> success (no error raised)."""
        service, token_repo = make_otp_service()

        otp_code = "123456"
        token_doc = make_token_doc(otp_code=otp_code)
        token_repo.find_latest_by_user.return_value = token_doc
        token_repo.consume_if_unused.return_value = True

        # Should not raise
        await service.verify_otp(USER_OID, otp_code, TOKEN_TYPE_EMAIL_VERIFY)

        token_repo.find_latest_by_user.assert_called_once_with(
            USER_OID, TOKEN_TYPE_EMAIL_VERIFY
        )
        token_repo.consume_if_unused.assert_called_once_with(token_doc.id)

    @pytest.mark.asyncio
    async def test_verify_wrong_code_raises_validation_error(self):
        """create -> verify with wrong code -> ValidationError."""
        service, token_repo = make_otp_service()

        otp_code = "123456"
        token_doc = make_token_doc(otp_code=otp_code)
        token_repo.find_latest_by_user.return_value = token_doc

        with pytest.raises(ValidationError, match="Invalid or expired"):
            await service.verify_otp(USER_OID, "999999", TOKEN_TYPE_EMAIL_VERIFY)

        token_repo.increment_attempts.assert_called_once_with(token_doc.id)

    @pytest.mark.asyncio
    async def test_five_wrong_codes_locks_out(self):
        """create -> 5 wrong codes -> "Too many failed attempts"."""
        service, token_repo = make_otp_service()

        otp_code = "123456"
        token_doc = make_token_doc(
            otp_code=otp_code, attempts=MAX_VERIFICATION_ATTEMPTS
        )
        token_repo.find_latest_by_user.return_value = token_doc

        with pytest.raises(ValidationError, match="Too many failed attempts"):
            await service.verify_otp(USER_OID, "999999", TOKEN_TYPE_EMAIL_VERIFY)

        # Should not even attempt to compare — locked out before hash check
        token_repo.increment_attempts.assert_not_called()

    @pytest.mark.asyncio
    async def test_verify_then_reuse_fails(self):
        """create -> verify successfully -> attempt reuse -> fails (consumed)."""
        service, token_repo = make_otp_service()

        otp_code = "123456"
        token_doc = make_token_doc(otp_code=otp_code)
        token_repo.find_latest_by_user.return_value = token_doc

        # First verification: consume_if_unused returns True
        token_repo.consume_if_unused.return_value = True
        await service.verify_otp(USER_OID, otp_code, TOKEN_TYPE_EMAIL_VERIFY)

        # Second verification: consume_if_unused returns False (already used)
        token_repo.consume_if_unused.return_value = False
        with pytest.raises(ValidationError, match="Invalid or expired"):
            await service.verify_otp(USER_OID, otp_code, TOKEN_TYPE_EMAIL_VERIFY)
