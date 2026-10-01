"""
Shared helpers for /api/v1/* integration tests.

All DB / Redis / external-service calls are eliminated via
dependency_overrides and a mock lifespan — no real infrastructure needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi import FastAPI

from app.dependencies import CurrentUser, get_custom_domain_service
from app.routes.api_v1 import router as api_v1_router
from app.schemas.models.api_key import ApiKeyDoc
from app.schemas.models.url import UrlV2Doc
from tests.conftest import build_test_app


def _build_test_app(overrides: dict) -> FastAPI:
    """Thin wrapper around the shared builder for api_v1 tests.

    Auto-injects a default ``custom_domain_service`` mock so tests that don't
    care about the domain selector don't have to wire one. Specific tests
    that need to assert against the service can pass their own override.
    """
    final_overrides = dict(overrides)
    if get_custom_domain_service not in final_overrides:
        final_overrides[get_custom_domain_service] = lambda: AsyncMock()
    return build_test_app(api_v1_router, overrides=final_overrides)


def _make_url_doc(alias: str = "testme", owner_id: ObjectId | None = None) -> UrlV2Doc:
    oid = owner_id or ObjectId()
    return UrlV2Doc(
        **{
            "_id": ObjectId(),
            "alias": alias,
            "owner_id": oid,
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "long_url": "https://example.com/long",
            "status": "ACTIVE",
            "private_stats": False,
            "domain": "spoo.me",
        }
    )


def _make_user(
    user_id: ObjectId | None = None,
    email_verified: bool = True,
    api_key_doc: ApiKeyDoc | None = None,
    email: str | None = None,
) -> CurrentUser:
    return CurrentUser(
        user_id=user_id or ObjectId(),
        email_verified=email_verified,
        api_key_doc=api_key_doc,
        email=email,
    )


def _make_api_key_doc(
    user_id: ObjectId | None = None, scopes: list | None = None
) -> ApiKeyDoc:
    return ApiKeyDoc(
        **{
            "_id": ObjectId(),
            "user_id": user_id or ObjectId(),
            "token_prefix": "AbCdEfGh",
            "token_hash": "testhash",
            "name": "Test Key",
            "scopes": scopes or ["shorten:create"],
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "revoked": False,
        }
    )
