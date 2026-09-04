"""The JWT ``plan`` claim: issued from the resolver, never fatal."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest

from tests.unit.services.test_auth_service import make_jwt_settings, make_user_doc


def _decode(token: str) -> dict:
    s = make_jwt_settings()
    return pyjwt.decode(
        token,
        s.jwt_secret,
        algorithms=["HS256"],
        audience=s.jwt_audience,
        issuer=s.jwt_issuer,
    )


@pytest.mark.asyncio
async def test_plan_claim_comes_from_the_lookup():
    from services.token_factory import TokenFactory

    lookup = AsyncMock(return_value="pro")
    tf = TokenFactory(make_jwt_settings(), plan_of=lookup)
    user = make_user_doc()
    access, refresh = await tf.issue_tokens(user, "pwd")
    assert _decode(access)["plan"] == "pro"
    assert "plan" not in _decode(refresh)
    lookup.assert_awaited_once_with(user.id)


@pytest.mark.asyncio
async def test_without_a_lookup_the_claim_is_free():
    from services.token_factory import TokenFactory

    tf = TokenFactory(make_jwt_settings())
    access, _ = await tf.issue_tokens(make_user_doc(), "pwd")
    assert _decode(access)["plan"] == "free"


@pytest.mark.asyncio
async def test_lookup_failure_degrades_to_free():
    from services.token_factory import TokenFactory

    tf = TokenFactory(make_jwt_settings(), plan_of=AsyncMock(side_effect=RuntimeError))
    access, _ = await tf.issue_tokens(make_user_doc(), "pwd")
    assert _decode(access)["plan"] == "free"


@pytest.mark.asyncio
async def test_current_user_reads_the_claim():
    from dependencies.auth import get_current_user
    from services.token_factory import TokenFactory
    from tests.unit.test_auth_deps import make_request, make_settings

    tf = TokenFactory(make_jwt_settings(), plan_of=AsyncMock(return_value="pro"))
    access, _ = await tf.issue_tokens(make_user_doc(), "pwd")
    request = make_request(auth_header=f"Bearer {access}")
    with patch("dependencies.auth.get_settings", return_value=make_settings()):
        user = await get_current_user(request, db=AsyncMock())
    assert user is not None
    assert user.plan_claim == "pro"
