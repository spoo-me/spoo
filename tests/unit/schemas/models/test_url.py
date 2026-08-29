"""Unit tests for UrlV2Doc, LegacyUrlDoc, EmojiUrlDoc, and LinkMetaTags."""

from datetime import timedelta

import pytest
from bson import ObjectId
from pydantic import ValidationError as PydanticValidationError

from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.url import (
    EmojiUrlDoc,
    LegacyUrlDoc,
    LinkMetaTags,
    UrlDestination,
    UrlStatus,
    UrlV2Doc,
)

from .conftest import now, oid

# ── UrlV2Doc ──────────────────────────────────────────────────────────────────


class TestUrlV2Doc:
    def _make(self, **overrides):
        base = {
            "_id": oid(),
            "alias": "abc1234",
            "owner_id": oid(),
            "domain": "spoo.me",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        base.update(overrides)
        return UrlV2Doc.model_validate(base)

    def test_instantiation(self):
        doc = self._make()
        assert doc.alias == "abc1234"
        assert doc.status == "ACTIVE"
        assert doc.total_clicks == 0
        assert doc.private_stats is True

    def test_optional_fields_default_none(self):
        doc = self._make()
        for field in ("password", "max_clicks", "last_click", "expire_after"):
            assert getattr(doc, field) is None, f"{field} should default to None"

    def test_to_mongo_round_trip(self):
        o, owner, t = oid(), oid(), now()
        doc = self._make(
            **{"_id": o, "owner_id": owner, "created_at": t, "max_clicks": 10}
        )
        restored = UrlV2Doc.from_mongo(doc.to_mongo())
        assert restored.alias == doc.alias
        assert restored.max_clicks == 10
        assert str(restored.id) == str(o)

    def test_null_owner_id_coerced_to_anonymous(self):
        doc = self._make(owner_id=None)
        assert doc.owner_id == ANONYMOUS_OWNER_ID
        assert isinstance(doc.owner_id, ObjectId)

    def test_missing_owner_id_defaults_to_anonymous(self):
        base = {
            "_id": oid(),
            "alias": "abc1234",
            "domain": "spoo.me",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        doc = UrlV2Doc.from_mongo(base)
        assert doc.owner_id == ANONYMOUS_OWNER_ID
        assert isinstance(doc.owner_id, ObjectId)

    def test_missing_domain_raises(self):
        # PR1: domain is required. Catching the absence at construction time
        # prevents silent corruption (a doc with empty domain is invisible
        # to find_by_alias scoped lookups). Pydantic's "required field
        # missing" check fires before the field_validator, so the error
        # type is ValidationError, not ValueError.
        import pytest
        from pydantic import ValidationError

        base = {
            "_id": oid(),
            "alias": "abc1234",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        with pytest.raises(ValidationError, match="domain"):
            UrlV2Doc.model_validate(base)

    def test_empty_domain_raises(self):
        # Empty string passes the required check but fails the validator.
        import pytest
        from pydantic import ValidationError

        base = {
            "_id": oid(),
            "alias": "abc1234",
            "domain": "",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        with pytest.raises(ValidationError, match="domain is required"):
            UrlV2Doc.model_validate(base)

    def test_whitespace_only_domain_raises(self):
        # Strip-then-check catches whitespace-only input that the bare
        # empty-string check would miss.
        import pytest
        from pydantic import ValidationError

        base = {
            "_id": oid(),
            "alias": "abc1234",
            "domain": "   ",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        with pytest.raises(ValidationError, match="domain is required"):
            UrlV2Doc.model_validate(base)

    def test_domain_strips_surrounding_whitespace(self):
        doc = UrlV2Doc.model_validate(
            {
                "_id": oid(),
                "alias": "abc1234",
                "domain": "  Spoo.Me  ",
                "created_at": now(),
                "long_url": "https://example.com",
            }
        )
        assert doc.domain == "spoo.me"

    def test_from_mongo_none_returns_none(self):
        assert UrlV2Doc.from_mongo(None) is None


# ── LegacyUrlDoc ─────────────────────────────────────────────────────────────


class TestLegacyUrlDoc:
    def _make(self, **overrides):
        base = {"_id": "abcdef", "url": "https://example.com"}
        base.update(overrides)
        return LegacyUrlDoc.model_validate(base)

    def test_string_id(self):
        assert self._make().id == "abcdef"

    def test_hyphenated_aliases(self):
        doc = LegacyUrlDoc.model_validate(
            {
                "_id": "abcdef",
                "url": "https://example.com",
                "max-clicks": 100,
                "total-clicks": 5,
                "block-bots": True,
                "last-click": "2024-01-01 12:00:00",
                "last-click-browser": "Chrome",
                "last-click-os": "Windows",
                "last-click-country": "US",
            }
        )
        assert doc.max_clicks == 100
        assert doc.total_clicks == 5
        assert doc.block_bots is True
        assert doc.last_click_browser == "Chrome"

    def test_to_mongo_uses_hyphenated_keys(self):
        doc = self._make(**{"max-clicks": 50, "total-clicks": 3})
        mongo = doc.to_mongo()
        assert mongo.get("max-clicks") == 50
        assert "total-clicks" in mongo

    def test_missing_optional_fields_use_defaults(self):
        doc = self._make()
        assert doc.max_clicks is None
        assert doc.total_clicks == 0
        assert doc.block_bots is None
        assert doc.ips == []
        assert doc.counter == {}

    def test_from_mongo_none_returns_none(self):
        assert LegacyUrlDoc.from_mongo(None) is None


# ── EmojiUrlDoc ───────────────────────────────────────────────────────────────


class TestEmojiUrlDoc:
    def test_same_shape_as_legacy(self):
        doc = EmojiUrlDoc.model_validate(
            {
                "_id": "\U0001f680\U0001f389",
                "url": "https://example.com",
                "max-clicks": 5,
            }
        )
        assert doc.id == "\U0001f680\U0001f389"
        assert doc.max_clicks == 5


class TestUrlV2DocGeoRules:
    def _make(self, **overrides):
        base = {
            "_id": oid(),
            "alias": "abc1234",
            "owner_id": oid(),
            "domain": "spoo.me",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        base.update(overrides)
        return UrlV2Doc.model_validate(base)

    def test_absent_geo_rules_defaults_to_none(self):
        """Docs created before the field existed deserialize with None —
        no backfill required."""
        doc = self._make()
        assert doc.geo_rules is None

    def test_geo_rules_round_trip(self):
        rules = {"IN": "https://example.in/", "US": "https://example.com/us"}
        doc = self._make(geo_rules=rules)
        assert doc.geo_rules == rules
        assert doc.to_mongo()["geo_rules"] == rules


# ── LinkMetaTags ──────────────────────────────────────────────────────────────


class TestLinkMetaTags:
    def test_minimal(self):
        m = LinkMetaTags(title="Launch day 🎉")
        assert m.title == "Launch day 🎉"
        assert m.description is None
        assert m.image is None
        assert m.color is None

    def test_strips_control_chars(self):
        assert LinkMetaTags(title="a\r\nb\x00c").title == "abc"
        assert LinkMetaTags(title="t", description="d\x1fe").description == "de"

    def test_title_required(self):
        with pytest.raises(PydanticValidationError):
            LinkMetaTags()
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="")

    def test_title_length_cap(self):
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="x" * 121)

    def test_description_length_cap(self):
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="t", description="x" * 241)

    def test_image_requires_https(self):
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="t", image="http://x.com/a.png")

    def test_image_rejects_svg(self):
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="t", image="https://x.com/a.svg")
        # case-insensitive, query string doesn't hide it
        with pytest.raises(PydanticValidationError):
            LinkMetaTags(title="t", image="https://x.com/A.SVG?v=1")

    def test_image_valid(self):
        m = LinkMetaTags(title="t", image="https://x.com/og.png")
        assert m.image == "https://x.com/og.png"

    def test_color_hex(self):
        assert LinkMetaTags(title="t", color="#FF5733").color == "#FF5733"
        for bad in ("red", "#FFF", "#GG5733", "FF5733"):
            with pytest.raises(PydanticValidationError):
                LinkMetaTags(title="t", color=bad)

    def test_url_v2_doc_carries_meta_tags(self):
        doc = UrlV2Doc.model_validate(
            {
                "_id": oid(),
                "alias": "abc1234",
                "domain": "spoo.me",
                "created_at": now(),
                "long_url": "https://example.com",
                "meta_tags": {"title": "T", "description": "D"},
            }
        )
        assert doc.meta_tags is not None
        assert doc.meta_tags.title == "T"
        restored = UrlV2Doc.from_mongo(doc.to_mongo())
        assert restored.meta_tags.description == "D"

    def test_url_v2_doc_meta_tags_default_none(self):
        doc = UrlV2Doc.model_validate(
            {
                "_id": oid(),
                "alias": "abc1234",
                "domain": "spoo.me",
                "created_at": now(),
                "long_url": "https://example.com",
            }
        )
        assert doc.meta_tags is None


# ── UrlV2Doc.effective_status ─────────────────────────────────────────────────


class TestUrlV2DocEffectiveStatus:
    """The derived-status predicate shared by the redirect, the DTOs, and
    the public resolver. The Mongo filter clause must express the same
    predicate — pinned in tests/unit/services/test_url_service.py."""

    def _make(self, **overrides):
        base = {
            "_id": oid(),
            "alias": "abc1234",
            "owner_id": oid(),
            "domain": "spoo.me",
            "created_at": now(),
            "long_url": "https://example.com",
        }
        base.update(overrides)
        return UrlV2Doc.model_validate(base)

    def test_active_without_expiry_stays_active(self):
        assert self._make().effective_status == UrlStatus.ACTIVE

    def test_active_with_future_expiry_stays_active(self):
        doc = self._make(expire_after=now() + timedelta(hours=1))
        assert doc.effective_status == UrlStatus.ACTIVE

    def test_active_with_past_expiry_reads_expired(self):
        doc = self._make(expire_after=now() - timedelta(seconds=1))
        assert doc.effective_status == UrlStatus.EXPIRED

    def test_boundary_expiry_equal_to_now_reads_expired(self):
        # <= convention — an expiry stamped exactly now is already expired.
        doc = self._make(expire_after=now() - timedelta(microseconds=1))
        assert doc.effective_status == UrlStatus.EXPIRED

    def test_naive_past_expiry_treated_as_utc(self):
        # Mongo returns naive UTC datetimes — they must still enforce.
        naive_past = (now() - timedelta(hours=1)).replace(tzinfo=None)
        doc = self._make(expire_after=naive_past)
        assert doc.effective_status == UrlStatus.EXPIRED

    def test_max_clicks_exhausted_reads_expired(self):
        doc = self._make(max_clicks=5, total_clicks=5)
        assert doc.effective_status == UrlStatus.EXPIRED

    def test_max_clicks_not_exhausted_stays_active(self):
        doc = self._make(max_clicks=5, total_clicks=4)
        assert doc.effective_status == UrlStatus.ACTIVE

    @pytest.mark.parametrize("status", ["INACTIVE", "BLOCKED", "EXPIRED"])
    def test_non_active_stored_status_never_folded(self, status):
        # Derivation only applies to ACTIVE — stored non-ACTIVE is truth.
        doc = self._make(status=status, expire_after=now() - timedelta(hours=1))
        assert doc.effective_status == UrlStatus(status)

    def test_wire_casing_pin(self):
        """The public-page contract is frozen lowercase — .value.lower()
        in v2_effective_status is load-bearing over UPPERCASE enum values."""
        from services.public_link_resolver import v2_effective_status

        assert v2_effective_status(self._make()) == "active"
        expired = self._make(expire_after=now() - timedelta(hours=1))
        assert v2_effective_status(expired) == "expired"
        assert {s.value for s in UrlStatus} == {
            "ACTIVE",
            "INACTIVE",
            "EXPIRED",
            "BLOCKED",
        }


# ── UrlDestination ────────────────────────────────────────────────────────────


class TestUrlDestination:
    def test_from_url_parses(self):
        dest = UrlDestination.from_url("https://a.example.co.uk/x")
        assert dest.registrable_domain == "example.co.uk"
        assert dest.host == "a.example.co.uk"
        assert dest.subdomain == "a"
        assert dest.scheme == "https"

    def test_from_url_unparseable_is_none(self):
        assert UrlDestination.from_url("garbage") is None
        assert UrlDestination.from_url(None) is None

    def test_v2_doc_defaults_dest_to_none(self):
        # Additive optional field — old cache entries and pre-backfill docs
        # deserialize without it.
        doc = UrlV2Doc(
            alias="abc",
            domain="spoo.me",
            created_at=now(),
            long_url="https://example.com",
        )
        assert doc.dest is None

    def test_v2_doc_accepts_dest_subdoc(self):
        doc = UrlV2Doc(
            alias="abc",
            domain="spoo.me",
            created_at=now(),
            long_url="https://a.evil.com/kit",
            dest=UrlDestination.from_url("https://a.evil.com/kit"),
        )
        assert doc.dest.registrable_domain == "evil.com"
