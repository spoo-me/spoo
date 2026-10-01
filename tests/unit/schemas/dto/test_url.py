"""Unit tests for URL request and response DTOs."""

from __future__ import annotations

import json
import typing
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.schemas.dto.requests.url import (
    CreateUrlRequest,
    ListUrlsQuery,
    UpdateUrlRequest,
)
from app.schemas.dto.responses.url import (
    UpdateUrlResponse,
    UrlListItem,
    UrlListResponse,
    UrlResponse,
)
from app.schemas.models.url import UrlV2Doc

# ── CreateUrlRequest ───────────────────────────────────────────────────────────


class TestCreateUrlRequest:
    def test_accepts_long_url(self):
        req = CreateUrlRequest.model_validate({"long_url": "https://example.com"})
        assert req.long_url == "https://example.com"

    def test_accepts_url_alias(self):
        """``url`` field is accepted as an alias for ``long_url``."""
        req = CreateUrlRequest.model_validate({"url": "https://example.com"})
        assert req.long_url == "https://example.com"

    def test_requires_long_url(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate({"alias": "abc"})

    def test_max_clicks_must_be_positive(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "max_clicks": 0}
            )

    def test_max_clicks_negative_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "max_clicks": -5}
            )

    def test_max_clicks_positive_accepted(self):
        req = CreateUrlRequest.model_validate(
            {"long_url": "https://example.com", "max_clicks": 10}
        )
        assert req.max_clicks == 10

    def test_optional_fields_default_none(self):
        req = CreateUrlRequest.model_validate({"long_url": "https://example.com"})
        assert req.alias is None
        assert req.password is None
        assert req.block_bots is None
        assert req.max_clicks is None
        assert req.expire_after is None
        assert req.private_stats is None

    def test_all_optional_fields(self):
        req = CreateUrlRequest.model_validate(
            {
                "long_url": "https://example.com",
                "alias": "mylink",
                "password": "p@ssw0rd",
                "block_bots": True,
                "max_clicks": 100,
                "expire_after": 9999999999,
                "private_stats": False,
            }
        )
        assert req.alias == "mylink"
        assert req.block_bots is True
        assert req.max_clicks == 100


class TestCreateUrlRequestExpiredRedirectUrl:
    def test_defaults_none(self):
        assert CreateUrlRequest(long_url="https://x.com").expired_redirect_url is None

    def test_accepts_url(self):
        req = CreateUrlRequest(
            long_url="https://x.com", expired_redirect_url="https://y.com/ended"
        )
        assert req.expired_redirect_url == "https://y.com/ended"

    def test_update_null_lands_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({"expired_redirect_url": None})
        assert "expired_redirect_url" in req.model_fields_set
        assert req.expired_redirect_url is None


class TestCreateUrlRequestAliasShape:
    """DTO-level alias gate: structural failures are 422; the emoji
    *policy* (qualification/version/grapheme caps) is service-enforced."""

    @pytest.mark.parametrize("alias", ["abc", "my-link_9", "a" * 16])
    def test_alphanumeric_in_bounds_accepted(self, alias):
        req = CreateUrlRequest.model_validate(
            {"long_url": "https://example.com", "alias": alias}
        )
        assert req.alias == alias

    @pytest.mark.parametrize("alias", ["ab", "a" * 17])
    def test_alphanumeric_out_of_bounds_rejected(self, alias):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "alias": alias}
            )

    @pytest.mark.parametrize(
        "alias",
        [
            "🚀🔥",
            "⭐",
            "👍🏽👍🏽",
            # Policy-rejected but emoji-shaped: pass the DTO, fail in service
            "🏳️‍🌈",
            "🇺🇸",
            "1️⃣",
        ],
    )
    def test_emoji_shaped_accepted(self, alias):
        req = CreateUrlRequest.model_validate(
            {"long_url": "https://example.com", "alias": alias}
        )
        assert req.alias == alias

    @pytest.mark.parametrize("alias", ["abc🎉", "🎉 🎉", "café", "a b", "🎉x"])
    def test_mixed_or_garbage_rejected(self, alias):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "alias": alias}
            )

    def test_alias_type_defaults_alphanumeric(self):
        req = CreateUrlRequest.model_validate({"long_url": "https://example.com"})
        assert req.alias_type == "alphanumeric"

    def test_alias_type_emoji_accepted(self):
        req = CreateUrlRequest.model_validate(
            {"long_url": "https://example.com", "alias_type": "emoji"}
        )
        assert req.alias_type == "emoji"

    def test_alias_type_invalid_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "alias_type": "hieroglyphs"}
            )


class TestUpdateUrlRequestAliasShape:
    def test_emoji_alias_accepted(self):
        req = UpdateUrlRequest.model_validate({"alias": "🚀🔥"})
        assert req.alias == "🚀🔥"

    def test_mixed_alias_rejected(self):
        with pytest.raises(ValidationError):
            UpdateUrlRequest.model_validate({"alias": "abc🎉"})

    def test_short_alphanumeric_rejected(self):
        with pytest.raises(ValidationError):
            UpdateUrlRequest.model_validate({"alias": "ab"})


# ── UpdateUrlRequest ───────────────────────────────────────────────────────────


class TestUpdateUrlRequest:
    def test_all_fields_optional(self):
        req = UpdateUrlRequest.model_validate({})
        assert req.long_url is None
        assert req.alias is None
        assert req.status is None

    @pytest.mark.parametrize(
        "status",
        ["ACTIVE", "INACTIVE"],
        ids=["active", "inactive"],
    )
    def test_accepts_valid_status(self, status):
        assert UpdateUrlRequest.model_validate({"status": status}).status == status

    @pytest.mark.parametrize(
        "status",
        ["DELETED", "UNKNOWN", "invalid"],
        ids=["deleted", "unknown", "invalid"],
    )
    def test_rejects_invalid_status(self, status):
        with pytest.raises(ValidationError):
            UpdateUrlRequest.model_validate({"status": status})

    def test_url_alias_maps_to_long_url(self):
        req = UpdateUrlRequest.model_validate({"url": "https://new.example.com"})
        assert req.long_url == "https://new.example.com"

    def test_max_clicks_zero_allowed(self):
        """0 is allowed in update to signal removing the limit."""
        assert UpdateUrlRequest.model_validate({"max_clicks": 0}).max_clicks == 0

    def test_max_clicks_negative_rejected(self):
        with pytest.raises(ValidationError):
            UpdateUrlRequest.model_validate({"max_clicks": -1})


# ── ListUrlsQuery ──────────────────────────────────────────────────────────────


class TestListUrlsQuery:
    def test_defaults(self):
        q = ListUrlsQuery.model_validate({})
        assert q.page == 1
        assert q.page_size == 20
        assert q.sort_by == "created_at"
        assert q.sort_order == "descending"

    def test_camelcase_aliases(self):
        q = ListUrlsQuery.model_validate(
            {"pageSize": 50, "sortBy": "total_clicks", "sortOrder": "ascending"}
        )
        assert q.page_size == 50
        assert q.sort_by == "total_clicks"

    def test_page_must_be_ge_1(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"page": 0})

    def test_page_size_max_100(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"pageSize": 101})

    def test_invalid_sort_by_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"sortBy": "invalid_field"})

    def test_valid_filter_json_parsed(self):
        filter_json = json.dumps({"status": "ACTIVE", "passwordSet": True})
        q = ListUrlsQuery.model_validate({"filter": filter_json})
        assert q.parsed_filter.status == "ACTIVE"
        assert q.parsed_filter.password_set is True

    def test_invalid_filter_json_raises(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"filter": "{not valid json"})

    def test_filter_non_object_rejected(self):
        with pytest.raises(ValidationError):
            ListUrlsQuery.model_validate({"filter": "[1, 2, 3]"})

    def test_filter_by_alias_also_works(self):
        filter_json = json.dumps({"search": "example"})
        q = ListUrlsQuery.model_validate({"filterBy": filter_json})
        assert q.parsed_filter is not None
        assert q.parsed_filter.search == "example"

    def test_no_filter_leaves_parsed_filter_none(self):
        assert ListUrlsQuery.model_validate({}).parsed_filter is None


# ── Response DTO serialization shapes ─────────────────────────────────────────


class TestUrlResponse:
    def test_serialization(self):
        r = UrlResponse(
            id="661f1f77bcf86cd799439099",
            alias="abc1234",
            short_url="https://spoo.me/abc1234",
            long_url="https://example.com",
            owner_id="507f1f77bcf86cd799439011",
            created_at=1704067200,
            status="ACTIVE",
            private_stats=True,
        )
        d = r.model_dump()
        assert d["id"] == "661f1f77bcf86cd799439099"
        assert d["alias"] == "abc1234"
        assert d["created_at"] == 1704067200
        assert d["status"] == "ACTIVE"

    def test_owner_id_optional(self):
        r = UrlResponse(
            id="661f1f77bcf86cd799439099",
            alias="abc1234",
            short_url="https://spoo.me/abc1234",
            long_url="https://example.com",
            created_at=1704067200,
            status="ACTIVE",
        )
        assert r.owner_id is None


class TestUrlListResponse:
    def test_camelcase_keys_in_output(self):
        r = UrlListResponse(
            items=[],
            page=1,
            pageSize=20,
            total=0,
            hasNext=False,
            sortBy="created_at",
            sortOrder="descending",
        )
        d = r.model_dump()
        assert "pageSize" in d
        assert "hasNext" in d
        assert "sortBy" in d
        assert "sortOrder" in d
        assert d["pageSize"] == 20
        assert d["hasNext"] is False


# ── from_doc() factory method tests ──────────────────────────────────────────


def _make_doc(**overrides) -> UrlV2Doc:
    """Build a UrlV2Doc with sensible defaults; override via kwargs."""
    base = {
        "_id": ObjectId(),
        "alias": "test123",
        "owner_id": ObjectId(),
        "domain": "spoo.me",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "long_url": "https://example.com/long",
        "password": None,
        "block_bots": False,
        "max_clicks": None,
        "expire_after": None,
        "status": "ACTIVE",
        "private_stats": False,
        "total_clicks": 42,
        "last_click": datetime(2025, 6, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2025, 3, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return UrlV2Doc.from_mongo(base)


class TestUrlResponseFromDoc:
    def test_basic_fields(self):
        doc = _make_doc()
        r = UrlResponse.from_doc(doc, "https://spoo.me")
        # Same serialization the list item uses — the ObjectId hex the
        # management endpoints address.
        assert r.id == str(doc.id)
        assert r.alias == "test123"
        assert r.short_url == "https://spoo.me/test123"
        assert r.long_url == "https://example.com/long"
        assert r.status == "ACTIVE"

    def test_owner_id_stringified(self):
        oid = ObjectId()
        r = UrlResponse.from_doc(_make_doc(owner_id=oid), "https://spoo.me")
        assert r.owner_id == str(oid)

    def test_anonymous_owner_id_returns_none(self):
        """Anonymous URLs (sentinel ObjectId) should return null owner_id."""
        from app.schemas.models.base import ANONYMOUS_OWNER_ID

        r = UrlResponse.from_doc(
            _make_doc(owner_id=ANONYMOUS_OWNER_ID), "https://spoo.me"
        )
        assert r.owner_id is None

    def test_created_at_is_unix_timestamp(self):
        r = UrlResponse.from_doc(_make_doc(), "https://spoo.me")
        assert isinstance(r.created_at, int)
        assert r.created_at == int(
            datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
        )

    def test_app_url_trailing_slash_stripped(self):
        r = UrlResponse.from_doc(_make_doc(), "https://spoo.me/")
        assert r.short_url == "https://spoo.me/test123"


class TestUpdateUrlResponseFromDoc:
    def test_basic_fields(self):
        doc = _make_doc()
        r = UpdateUrlResponse.from_doc(doc)
        assert r.id == str(doc.id)
        assert r.alias == "test123"
        assert r.long_url == "https://example.com/long"
        assert r.status == "ACTIVE"

    def test_password_set_true_when_password_exists(self):
        r = UpdateUrlResponse.from_doc(_make_doc(password="$argon2id$hash"))
        assert r.password_set is True

    def test_password_set_false_when_no_password(self):
        r = UpdateUrlResponse.from_doc(_make_doc(password=None))
        assert r.password_set is False

    def test_max_clicks_preserved(self):
        r = UpdateUrlResponse.from_doc(_make_doc(max_clicks=100))
        assert r.max_clicks == 100

    def test_max_clicks_none(self):
        r = UpdateUrlResponse.from_doc(_make_doc(max_clicks=None))
        assert r.max_clicks is None

    def test_expire_after_as_unix_timestamp(self):
        dt = datetime(2025, 12, 31, tzinfo=timezone.utc)
        r = UpdateUrlResponse.from_doc(_make_doc(expire_after=dt))
        assert r.expire_after == int(dt.timestamp())

    def test_expire_after_none(self):
        r = UpdateUrlResponse.from_doc(_make_doc(expire_after=None))
        assert r.expire_after is None

    def test_updated_at_as_unix_timestamp(self):
        r = UpdateUrlResponse.from_doc(_make_doc())
        assert isinstance(r.updated_at, int)

    def test_updated_at_none_defaults_to_zero(self):
        r = UpdateUrlResponse.from_doc(_make_doc(updated_at=None))
        assert r.updated_at == 0


class TestUrlListItemFromDoc:
    def test_basic_fields(self):
        doc = _make_doc()
        r = UrlListItem.from_doc(doc)
        assert r.id == str(doc.id)
        assert r.alias == "test123"
        assert r.total_clicks == 42

    def test_password_set_derived(self):
        assert UrlListItem.from_doc(_make_doc(password="hash")).password_set is True
        assert UrlListItem.from_doc(_make_doc(password=None)).password_set is False

    def test_block_bots_none_preserved(self):
        r = UrlListItem.from_doc(_make_doc(block_bots=None))
        assert r.block_bots is None

    def test_block_bots_false_preserved(self):
        r = UrlListItem.from_doc(_make_doc(block_bots=False))
        assert r.block_bots is False

    def test_block_bots_true(self):
        r = UrlListItem.from_doc(_make_doc(block_bots=True))
        assert r.block_bots is True

    def test_created_at_is_utc_datetime(self):
        r = UrlListItem.from_doc(_make_doc())
        assert r.created_at is not None
        assert r.created_at.tzinfo is not None

    def test_created_at_naive_gets_utc(self):
        naive = datetime(2025, 1, 1)  # no tzinfo
        r = UrlListItem.from_doc(_make_doc(created_at=naive))
        assert r.created_at.tzinfo == timezone.utc

    def test_last_click_none(self):
        r = UrlListItem.from_doc(_make_doc(last_click=None))
        assert r.last_click is None

    def test_last_click_utc(self):
        r = UrlListItem.from_doc(_make_doc())
        assert r.last_click is not None
        assert r.last_click.tzinfo is not None

    def test_expire_after_as_unix_timestamp(self):
        dt = datetime(2025, 12, 31, tzinfo=timezone.utc)
        r = UrlListItem.from_doc(_make_doc(expire_after=dt))
        assert r.expire_after == int(dt.timestamp())

    def test_expire_after_none(self):
        r = UrlListItem.from_doc(_make_doc(expire_after=None))
        assert r.expire_after is None


class TestUrlListItemDomain:
    def test_domain_propagates(self):
        from app.schemas.dto.responses.url import UrlListItem

        r = UrlListItem.from_doc(_make_doc(domain="links.acme.com"))
        assert r.domain == "links.acme.com"

    def test_domain_default_system(self):
        from app.schemas.dto.responses.url import UrlListItem

        r = UrlListItem.from_doc(_make_doc())  # domain = spoo.me
        assert r.domain == "spoo.me"


class TestCreateUrlRequestDomainField:
    def test_lowercases_and_strips_dot(self):
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(url="https://example.com", domain="LINKS.ACME.COM.")
        assert req.domain == "links.acme.com"

    def test_empty_string_normalised_to_none(self):
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(url="https://example.com", domain="")
        assert req.domain is None

    def test_omitted_domain_is_none(self):
        from app.schemas.dto.requests.url import CreateUrlRequest

        req = CreateUrlRequest(url="https://example.com")
        assert req.domain is None


class TestListUrlsQueryDomainFilter:
    def test_domain_filter_normalised(self):
        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery(domain="Links.Acme.COM")
        assert q.domain == "links.acme.com"

    def test_no_domain_filter(self):
        from app.schemas.dto.requests.url import ListUrlsQuery

        q = ListUrlsQuery()
        assert q.domain is None


# ── geo_rules ──────────────────────────────────────────────────────────────────


class TestGeoRulesField:
    def test_keys_normalised_to_uppercase(self):
        req = CreateUrlRequest.model_validate(
            {
                "long_url": "https://example.com",
                "geo_rules": {" in ": "https://example.in/"},
            }
        )
        assert req.geo_rules == {"IN": "https://example.in/"}

    def test_case_colliding_keys_rejected(self):
        with pytest.raises(ValidationError, match="duplicate country code"):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "geo_rules": {
                        "in": "https://a.example/",
                        "IN": "https://b.example/",
                    },
                }
            )

    def test_empty_url_value_rejected(self):
        with pytest.raises(ValidationError, match="non-empty URL"):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "geo_rules": {"IN": "  "}}
            )

    def test_oversized_url_value_rejected(self):
        with pytest.raises(ValidationError, match="exceeds"):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "geo_rules": {"IN": "https://example.in/" + "a" * 8192},
                }
            )

    def test_update_null_registers_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({"geo_rules": None})
        assert "geo_rules" in req.model_fields_set
        assert req.geo_rules is None

    def test_update_omitted_not_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({})
        assert "geo_rules" not in req.model_fields_set

    def test_url_values_stripped(self):
        req = UpdateUrlRequest.model_validate(
            {"geo_rules": {"US": "  https://example.com/us  "}}
        )
        assert req.geo_rules == {"US": "https://example.com/us"}

    def test_non_dict_geo_rules_returns_clean_validation_error(self):
        """A list/string payload must surface as a normal Pydantic 422,
        not an AttributeError from the mode="before" normaliser."""
        for bad in (["IN"], "IN", 42):
            with pytest.raises(ValidationError):
                CreateUrlRequest.model_validate(
                    {"long_url": "https://example.com", "geo_rules": bad}
                )

    def test_hard_entry_ceiling_bounds_the_pre_auth_loop(self):
        """The normaliser runs before auth — a giant map must be rejected
        before any per-entry work, independent of the configurable cap."""
        from itertools import product
        from string import ascii_uppercase

        keys = ["".join(p) for p in product(ascii_uppercase, repeat=2)][:501]
        with pytest.raises(ValidationError, match="cannot exceed 500"):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "geo_rules": {k: "https://example.com/x" for k in keys},
                }
            )


class TestMetaTagsResponseWarnings:
    """Platform-cliff warnings computed from image_meta — warn, never reject."""

    @staticmethod
    def _meta(width=None, height=None, bytes_=None):
        from app.schemas.models.url import LinkMetaTags, MetaImageMeta

        image_meta = None
        if width or height or bytes_:
            image_meta = MetaImageMeta(
                width=width,
                height=height,
                bytes=bytes_,
                checked_at=datetime.now(timezone.utc),
            )
        return LinkMetaTags(
            title="T", image="https://cdn.example.com/og.png", image_meta=image_meta
        )

    @staticmethod
    def _warnings(meta):
        from app.schemas.dto.responses.url import MetaTagsResponse

        return MetaTagsResponse.from_model(meta).warnings

    def test_good_image_has_no_warnings(self):
        assert self._warnings(self._meta(1200, 630, 120_000)) is None

    def test_no_image_meta_has_no_warnings(self):
        assert self._warnings(self._meta()) is None

    def test_over_300kb_warns_whatsapp(self):
        w = self._warnings(self._meta(1200, 630, 350_000))
        assert any("300KB" in x for x in w)

    def test_tiny_image_warns_may_be_dropped(self):
        w = self._warnings(self._meta(150, 150, 5_000))
        assert any("200x200" in x for x in w)

    def test_narrow_image_warns_small_thumbnail(self):
        w = self._warnings(self._meta(500, 500, 50_000))
        assert any("600px" in x for x in w)

    def test_extreme_aspect_ratio_warns(self):
        w = self._warnings(self._meta(2000, 400, 50_000))
        assert any("4:1" in x for x in w)

    def test_dims_unknown_skips_dimension_warnings(self):
        # Truncated sniff / not-yet-validated external image: bytes may be
        # known while dims are not — only the byte warning applies.
        w = self._warnings(self._meta(bytes_=400_000))
        assert w == ["image exceeds 300KB; WhatsApp may silently drop it"]


# ── Derived status in from_doc (time/max-click expiry folded) ────────────────


class TestFromDocDerivedStatus:
    """All three DTOs serialize EFFECTIVE status — a stored-ACTIVE doc
    whose expiry has passed must read EXPIRED on the wire even before
    the persisted flip happens."""

    PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)

    def test_url_response_derives_expired(self):
        r = UrlResponse.from_doc(_make_doc(expire_after=self.PAST), "https://spoo.me")
        assert r.status == "EXPIRED"

    def test_update_response_derives_expired(self):
        r = UpdateUrlResponse.from_doc(_make_doc(expire_after=self.PAST))
        assert r.status == "EXPIRED"

    def test_list_item_derives_expired(self):
        r = UrlListItem.from_doc(_make_doc(expire_after=self.PAST))
        assert r.status == "EXPIRED"

    def test_list_item_derives_expired_from_max_clicks(self):
        r = UrlListItem.from_doc(_make_doc(max_clicks=10, total_clicks=42))
        assert r.status == "EXPIRED"

    def test_inactive_stored_is_not_folded(self):
        r = UrlListItem.from_doc(_make_doc(status="INACTIVE", expire_after=self.PAST))
        assert r.status == "INACTIVE"

    def test_future_expiry_stays_active(self):
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        r = UrlListItem.from_doc(_make_doc(expire_after=future))
        assert r.status == "ACTIVE"


# ── ab_variants ────────────────────────────────────────────────────────────────


class TestAbVariantsField:
    def test_urls_stripped_and_weights_coerced(self):
        req = CreateUrlRequest.model_validate(
            {
                "long_url": "https://example.com",
                "ab_variants": [{"url": "  https://example.com/b ", "weight": "40"}],
            }
        )
        assert req.ab_variants is not None
        assert req.ab_variants[0].url == "https://example.com/b"
        assert req.ab_variants[0].weight == 40

    def test_weights_over_100_rejected(self):
        with pytest.raises(ValidationError, match="sum to at most 100"):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "ab_variants": [
                        {"url": "https://example.com/b", "weight": 70},
                        {"url": "https://example.com/c", "weight": 40},
                    ],
                }
            )

    def test_weights_exactly_100_accepted(self):
        req = CreateUrlRequest.model_validate(
            {
                "long_url": "https://example.com",
                "ab_variants": [
                    {"url": "https://example.com/b", "weight": 60},
                    {"url": "https://example.com/c", "weight": 40},
                ],
            }
        )
        assert sum(v.weight for v in req.ab_variants) == 100

    @pytest.mark.parametrize("weight", [0, 101, -1, 60.5, "many"])
    def test_bad_weight_rejected(self, weight):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "ab_variants": [{"url": "https://example.com/b", "weight": weight}],
                }
            )

    def test_empty_url_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "ab_variants": [{"url": "   ", "weight": 10}],
                }
            )

    def test_oversized_url_rejected(self):
        with pytest.raises(ValidationError):
            CreateUrlRequest.model_validate(
                {
                    "long_url": "https://example.com",
                    "ab_variants": [
                        {"url": "https://example.com/" + "a" * 8192, "weight": 10}
                    ],
                }
            )

    def test_non_list_returns_clean_validation_error(self):
        for bad in ({"url": "https://example.com/b", "weight": 10}, "x", 42):
            with pytest.raises(ValidationError):
                CreateUrlRequest.model_validate(
                    {"long_url": "https://example.com", "ab_variants": bad}
                )

    def test_hard_entry_ceiling_bounds_the_pre_auth_loop(self):
        variants = [{"url": "https://example.com/x", "weight": 1} for _ in range(51)]
        with pytest.raises(ValidationError, match="cannot exceed 50"):
            CreateUrlRequest.model_validate(
                {"long_url": "https://example.com", "ab_variants": variants}
            )

    def test_update_null_registers_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({"ab_variants": None})
        assert "ab_variants" in req.model_fields_set
        assert req.ab_variants is None

    def test_update_empty_list_registers_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({"ab_variants": []})
        assert "ab_variants" in req.model_fields_set
        assert req.ab_variants == []

    def test_update_omitted_not_in_fields_set(self):
        req = UpdateUrlRequest.model_validate({})
        assert "ab_variants" not in req.model_fields_set

    def test_update_weights_over_100_rejected(self):
        with pytest.raises(ValidationError, match="sum to at most 100"):
            UpdateUrlRequest.model_validate(
                {
                    "ab_variants": [
                        {"url": "https://example.com/b", "weight": 60},
                        {"url": "https://example.com/c", "weight": 50},
                    ]
                }
            )


class TestAbVariantsResponse:
    def _doc(self, **overrides) -> UrlV2Doc:
        base = {
            "_id": ObjectId(),
            "alias": "abc1234",
            "owner_id": ObjectId(),
            "domain": "spoo.me",
            "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "long_url": "https://example.com",
        }
        base.update(overrides)
        return UrlV2Doc.model_validate(base)

    VARIANTS: typing.ClassVar[list[dict]] = [
        {"url": "https://example.com/b", "weight": 60},
        {"url": "https://example.com/c", "weight": 40},
    ]

    def test_echoed_on_all_three_shapes(self):
        doc = self._doc(ab_variants=self.VARIANTS)
        assert (
            UrlResponse.from_doc(doc, "https://spoo.me").model_dump()["ab_variants"]
            == self.VARIANTS
        )
        assert UpdateUrlResponse.from_doc(doc).model_dump()["ab_variants"] == (
            self.VARIANTS
        )
        assert UrlListItem.from_doc(doc).model_dump()["ab_variants"] == self.VARIANTS

    def test_null_when_unset(self):
        doc = self._doc()
        assert UrlResponse.from_doc(doc, "https://spoo.me").ab_variants is None
        assert UrlListItem.from_doc(doc).ab_variants is None
