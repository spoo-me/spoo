"""Tests for POST /api/v1/shorten."""

from __future__ import annotations

import typing
from unittest.mock import AsyncMock

from bson import ObjectId
from fastapi.testclient import TestClient

from app.dependencies import get_current_user, get_url_service
from app.errors import ConflictError, ForbiddenError, ValidationError
from app.schemas.models.url import AbVariant

from .conftest import _build_test_app, _make_api_key_doc, _make_url_doc, _make_user


class TestShortenEmailVerification:
    """Unverified authenticated users must not be able to create URLs."""

    def test_shorten_unverified_email_returns_403(self):
        user = _make_user(email_verified=False)
        mock_svc = AsyncMock()

        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 403
        assert resp.json()["code"] == "EMAIL_NOT_VERIFIED"
        mock_svc.create.assert_not_called()

    def test_shorten_verified_email_returns_201(self):
        user = _make_user(email_verified=True)
        url_doc = _make_url_doc(owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 201
        mock_svc.create.assert_called_once()


class TestShorten:
    def test_shorten_anon_returns_201(self):
        url_doc = _make_url_doc()
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["alias"] == url_doc.alias
        assert "short_url" in body
        assert body["status"] == "ACTIVE"
        assert body["id"] == str(url_doc.id)

    def test_shorten_response_id_addresses_management_endpoints(self):
        """The create response must carry the ObjectId hex the {url_id}
        routes address — without it a consumer cannot manage what it just
        created except via a list round-trip."""
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 201
        body = resp.json()
        # Same serialization the list item uses: 24-char ObjectId hex.
        assert body["id"] == str(url_doc.id)
        assert ObjectId(body["id"]) == url_doc.id

    def test_shorten_with_alias(self):
        url_doc = _make_url_doc(alias="myalias")
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias": "myalias"},
            )

        assert resp.status_code == 201
        assert resp.json()["alias"] == "myalias"

    def test_shorten_api_key_missing_scope_returns_403(self):
        key_doc = _make_api_key_doc(scopes=["stats:read"])  # wrong scope
        user = _make_user(api_key_doc=key_doc)

        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 403

    def test_shorten_api_key_admin_scope(self):
        user_id = ObjectId()
        url_doc = _make_url_doc(owner_id=user_id)
        key_doc = _make_api_key_doc(user_id=user_id, scopes=["admin:all"])
        user = _make_user(user_id=user_id, api_key_doc=key_doc)

        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 201

    def test_shorten_missing_long_url_returns_422(self):
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: AsyncMock()}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/shorten", json={})

        assert resp.status_code == 422

    def test_shorten_validation_error_returns_400(self):
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(side_effect=ValidationError("invalid URL"))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_shorten_conflict_returns_409(self):
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(side_effect=ConflictError("alias taken"))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias": "taken"},
            )

        assert resp.status_code == 409


class TestShortenEmojiAlias:
    """Emoji aliases through the wire: DTO shape gate + raw-emoji short_url."""

    def test_emoji_alias_returns_201_with_raw_emoji_short_url(self):
        url_doc = _make_url_doc(alias="🚀🔥")
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias": "🚀🔥"},
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["alias"] == "🚀🔥"
        # Raw emoji, unencoded — clients percent-encode on use.
        assert body["short_url"].endswith("/🚀🔥")
        sent = mock_svc.create.call_args[0][0]
        assert sent.alias == "🚀🔥"

    def test_alias_type_emoji_passes_through_dto(self):
        url_doc = _make_url_doc(alias="😀🎯🍕")
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias_type": "emoji"},
            )

        assert resp.status_code == 201
        sent = mock_svc.create.call_args[0][0]
        assert sent.alias is None
        assert sent.alias_type == "emoji"

    def test_mixed_emoji_alias_returns_422(self):
        mock_svc = AsyncMock()
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias": "abc🎉"},
            )

        assert resp.status_code == 422
        mock_svc.create.assert_not_called()

    def test_policy_rejected_emoji_returns_400(self):
        # The service raises ValidationError for policy failures (flags,
        # ZWJ, keycaps) — wire contract is 400, not 422.
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(
            side_effect=ValidationError(
                "alias contains unsupported emoji or characters", field="alias"
            )
        )
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "alias": "🇺🇸"},
            )

        assert resp.status_code == 400
        assert resp.json()["field"] == "alias"

    def test_check_alias_emoji_policy_reason(self):
        mock_svc = AsyncMock()
        mock_svc.check_alias = AsyncMock(return_value="emoji_policy")
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/shorten/check-alias", params={"alias": "🇺🇸"})

        assert resp.status_code == 200
        assert resp.json() == {"available": False, "reason": "emoji_policy"}


class TestShortenWithCustomDomain:
    """``domain`` field on POST /shorten triggers owner+ACTIVE check."""

    def test_anonymous_user_cannot_use_custom_domain(self):
        from app.dependencies import get_custom_domain_service

        url_svc = AsyncMock()
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)
        application = _build_test_app(
            {
                get_current_user: lambda: None,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={
                    "long_url": "https://example.com",
                    "domain": "links.acme.com",
                },
            )
        assert resp.status_code == 401
        assert custom_svc.assert_owned_and_active.await_count == 0
        url_svc.create.assert_not_called()

    def test_authed_user_with_owned_active_domain_succeeds(self):
        from app.dependencies import get_custom_domain_service

        user = _make_user(email_verified=True)
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.domain = "links.acme.com"
        url_svc = AsyncMock()
        url_svc.create = AsyncMock(return_value=(url_doc, None))

        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        application = _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={
                    "long_url": "https://example.com",
                    "domain": "links.acme.com",
                },
            )
        assert resp.status_code == 201
        body = resp.json()
        # short_url is built off the custom host, not the system default.
        assert body["short_url"].startswith("https://links.acme.com/")
        # Owner check fired with (user, normalised fqdn). Pin both — the
        # contract isn't just "fired", it's "fired with the right args".
        assert custom_svc.assert_owned_and_active.await_count == 1
        own_args = custom_svc.assert_owned_and_active.call_args.args
        assert own_args[0].user_id == user.user_id
        assert own_args[1] == "links.acme.com"
        # Service got the domain on the create call.
        kwargs = url_svc.create.call_args.kwargs
        assert kwargs.get("domain") == "links.acme.com"


class TestCheckAliasWithCustomDomain:
    """`domain` query param on GET /shorten/check-alias triggers the same
    owner+ACTIVE gate as POST /shorten. Without this gate the create modal's
    live availability indicator would check against the wrong namespace
    once the user picks a custom domain in the picker."""

    def test_anonymous_user_cannot_check_against_custom_domain(self):
        from app.dependencies import get_custom_domain_service

        url_svc = AsyncMock()
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)
        application = _build_test_app(
            {
                get_current_user: lambda: None,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.get(
                "/api/v1/shorten/check-alias",
                params={"alias": "mylink", "domain": "links.acme.com"},
            )
        assert resp.status_code == 401
        url_svc.check_alias.assert_not_called()

    def test_authed_user_check_scopes_to_custom_domain(self):
        from app.dependencies import get_custom_domain_service

        user = _make_user(email_verified=True)
        url_svc = AsyncMock()
        url_svc.check_alias = AsyncMock(return_value="available")

        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        application = _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/v1/shorten/check-alias",
                params={"alias": "mylink", "domain": "links.acme.com"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"available": True, "reason": None}
        # Owner check fired and the service got the scope on the call.
        assert custom_svc.assert_owned_and_active.await_count == 1
        kwargs = url_svc.check_alias.call_args.kwargs
        assert kwargs.get("domain") == "links.acme.com"

    def test_omitted_domain_falls_back_to_system_default(self):
        """When `domain` isn't supplied (or is empty), no owner check fires
        and the service receives `domain=None` so it uses its default."""
        from app.dependencies import get_custom_domain_service

        url_svc = AsyncMock()
        url_svc.check_alias = AsyncMock(return_value="available")
        custom_svc = AsyncMock()
        custom_svc.assert_owned_and_active = AsyncMock(return_value=None)

        application = _build_test_app(
            {
                get_current_user: lambda: None,
                get_url_service: lambda: url_svc,
                get_custom_domain_service: lambda: custom_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.get("/api/v1/shorten/check-alias", params={"alias": "mylink"})
        assert resp.status_code == 200
        assert custom_svc.assert_owned_and_active.await_count == 0
        kwargs = url_svc.check_alias.call_args.kwargs
        assert kwargs.get("domain") is None


class TestShortenAbVariants:
    """ab_variants is authed-only and gated by the ab_testing feature flag."""

    VARIANTS: typing.ClassVar[list[dict]] = [
        {"url": "https://example.com/b", "weight": 40}
    ]
    BODY: typing.ClassVar[dict] = {
        "long_url": "https://example.com",
        "ab_variants": VARIANTS,
    }

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.is_enabled = AsyncMock(return_value=enabled)
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("A/B testing is not enabled for this account")
        )
        return svc

    def _app(self, user, mock_svc, flag_svc):
        from app.dependencies import get_feature_flag_service

        return _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )

    def test_anonymous_with_ab_variants_returns_401(self):
        mock_svc = AsyncMock()
        flag_svc = self._flag_svc(True)
        with TestClient(
            self._app(None, mock_svc, flag_svc), raise_server_exceptions=False
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 401
        mock_svc.create.assert_not_called()
        flag_svc.require.assert_not_awaited()

    def test_flag_off_returns_403(self):
        mock_svc = AsyncMock()
        with TestClient(
            self._app(_make_user(), mock_svc, self._flag_svc(False)),
            raise_server_exceptions=False,
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 403
        mock_svc.create.assert_not_called()

    def test_flag_on_returns_201_with_ab_variants_echoed(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.ab_variants = [AbVariant(url="https://example.com/b", weight=40)]
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        with TestClient(
            self._app(user, mock_svc, self._flag_svc(True)),
            raise_server_exceptions=True,
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 201
        assert resp.json()["ab_variants"] == self.VARIANTS
        sent = mock_svc.create.call_args.args[0]
        assert [v.model_dump() for v in sent.ab_variants] == self.VARIANTS

    def test_empty_list_flag_never_consulted(self):
        user = _make_user()
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(
            return_value=(_make_url_doc(owner_id=user.user_id), None)
        )
        flag_svc = self._flag_svc(False)

        with TestClient(
            self._app(user, mock_svc, flag_svc), raise_server_exceptions=True
        ) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={"long_url": "https://example.com", "ab_variants": []},
            )

        assert resp.status_code == 201
        assert resp.json()["ab_variants"] is None
        flag_svc.require.assert_not_awaited()


class TestShortenExpiredFallback:
    """expired_redirect_url is authed-only and gated by the expired_fallback flag."""

    BODY: typing.ClassVar[dict] = {
        "long_url": "https://example.com",
        "expired_redirect_url": "https://example.com/ended",
    }

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("Expired fallback is not enabled for this account")
        )
        return svc

    def _app(self, user, mock_svc, flag_svc):
        from app.dependencies import get_feature_flag_service

        return _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )

    def test_anonymous_returns_401(self):
        mock_svc = AsyncMock()
        flag_svc = self._flag_svc(True)
        with TestClient(
            self._app(None, mock_svc, flag_svc), raise_server_exceptions=False
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 401
        mock_svc.create.assert_not_called()
        flag_svc.require.assert_not_awaited()

    def test_flag_off_returns_403(self):
        mock_svc = AsyncMock()
        with TestClient(
            self._app(_make_user(), mock_svc, self._flag_svc(False)),
            raise_server_exceptions=False,
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 403
        mock_svc.create.assert_not_called()

    def test_flag_on_returns_201_and_echoes_field(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.expired_redirect_url = "https://example.com/ended"
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        with TestClient(
            self._app(user, mock_svc, self._flag_svc(True)),
            raise_server_exceptions=True,
        ) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 201
        assert resp.json()["expired_redirect_url"] == "https://example.com/ended"


class TestShortenGeoRules:
    """geo_rules is authed-only and gated by the geo_targeting feature flag."""

    GEO_BODY: typing.ClassVar[dict] = {
        "long_url": "https://example.com",
        "geo_rules": {"IN": "https://example.in/"},
    }

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.is_enabled = AsyncMock(return_value=enabled)
        # Mirror the real require(): no-op when enabled, 403 when not.
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("Geo targeting is not enabled for this account")
        )
        return svc

    def test_anonymous_with_geo_rules_returns_401(self):
        from app.dependencies import get_feature_flag_service

        mock_svc = AsyncMock()
        flag_svc = self._flag_svc(True)
        application = _build_test_app(
            {
                get_current_user: lambda: None,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/shorten", json=self.GEO_BODY)

        assert resp.status_code == 401
        mock_svc.create.assert_not_called()
        # Anonymity is decided before the flag is even consulted
        flag_svc.require.assert_not_awaited()

    def test_flag_off_returns_403(self):
        from app.dependencies import get_feature_flag_service

        user = _make_user()
        mock_svc = AsyncMock()
        application = _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: self._flag_svc(False),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/shorten", json=self.GEO_BODY)

        assert resp.status_code == 403
        mock_svc.create.assert_not_called()

    def test_flag_on_returns_201_with_geo_rules_echoed(self):
        from app.dependencies import get_feature_flag_service

        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        url_doc.geo_rules = {"IN": "https://example.in/"}
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))

        application = _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: self._flag_svc(True),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post("/api/v1/shorten", json=self.GEO_BODY)

        assert resp.status_code == 201
        assert resp.json()["geo_rules"] == {"IN": "https://example.in/"}

    def test_without_geo_rules_flag_never_consulted(self):
        from app.dependencies import get_feature_flag_service

        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))
        flag_svc = self._flag_svc(False)

        application = _build_test_app(
            {
                get_current_user: lambda: user,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post(
                "/api/v1/shorten", json={"long_url": "https://example.com"}
            )

        assert resp.status_code == 201
        flag_svc.require.assert_not_awaited()


class TestShortenClaimToken:
    def test_anonymous_shorten_returns_claim_token(self):
        url_doc = _make_url_doc()
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, "deed-token-123"))
        application = _build_test_app(
            {get_current_user: lambda: None, get_url_service: lambda: mock_svc}
        )
        with TestClient(application) as client:
            resp = client.post(
                "/api/v1/shorten", json={"url": "https://example.com/long"}
            )
        assert resp.status_code == 201
        assert resp.json()["claim_token"] == "deed-token-123"

    def test_authed_shorten_claim_token_is_null(self):
        user = _make_user()
        url_doc = _make_url_doc(owner_id=user.user_id)
        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(url_doc, None))
        application = _build_test_app(
            {get_current_user: lambda: user, get_url_service: lambda: mock_svc}
        )
        with TestClient(application) as client:
            resp = client.post(
                "/api/v1/shorten", json={"url": "https://example.com/long"}
            )
        assert resp.status_code == 201
        assert resp.json()["claim_token"] is None


class TestShortenScheduling:
    """starts_at / pre_start_url are authed-only and gated by link_scheduling."""

    BODY: typing.ClassVar[dict] = {
        "long_url": "https://example.com",
        "starts_at": 4_000_000_000,
    }

    @staticmethod
    def _flag_svc(enabled: bool) -> AsyncMock:
        svc = AsyncMock()
        svc.require = AsyncMock(
            side_effect=None
            if enabled
            else ForbiddenError("Link scheduling is not enabled for this account")
        )
        return svc

    def test_anonymous_returns_401_before_flag(self):
        from app.dependencies import get_feature_flag_service

        mock_svc = AsyncMock()
        flag_svc = self._flag_svc(True)
        application = _build_test_app(
            {
                get_current_user: lambda: None,
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: flag_svc,
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 401
        mock_svc.create.assert_not_called()
        flag_svc.require.assert_not_awaited()

    def test_flag_off_returns_403(self):
        from app.dependencies import get_feature_flag_service

        mock_svc = AsyncMock()
        application = _build_test_app(
            {
                get_current_user: lambda: _make_user(),
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: self._flag_svc(False),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 403
        mock_svc.create.assert_not_called()

    def test_pre_start_url_alone_is_gated_too(self):
        from app.dependencies import get_feature_flag_service

        mock_svc = AsyncMock()
        application = _build_test_app(
            {
                get_current_user: lambda: _make_user(),
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: self._flag_svc(False),
            }
        )
        with TestClient(application, raise_server_exceptions=False) as client:
            resp = client.post(
                "/api/v1/shorten",
                json={
                    "long_url": "https://example.com",
                    "pre_start_url": "https://example.org/soon",
                },
            )

        assert resp.status_code == 403

    def test_flag_on_returns_201(self):
        from app.dependencies import get_feature_flag_service

        mock_svc = AsyncMock()
        mock_svc.create = AsyncMock(return_value=(_make_url_doc("sched1"), None))
        application = _build_test_app(
            {
                get_current_user: lambda: _make_user(),
                get_url_service: lambda: mock_svc,
                get_feature_flag_service: lambda: self._flag_svc(True),
            }
        )
        with TestClient(application, raise_server_exceptions=True) as client:
            resp = client.post("/api/v1/shorten", json=self.BODY)

        assert resp.status_code == 201
        sent = mock_svc.create.call_args[0][0]
        assert sent.starts_at is not None
