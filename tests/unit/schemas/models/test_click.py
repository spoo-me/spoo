"""Unit tests for ClickDoc."""

from bson import ObjectId

from schemas.models.base import ANONYMOUS_OWNER_ID
from schemas.models.click import ClickDoc

from .conftest import now, oid


class TestClickDoc:
    def _make(self, **overrides):
        base = {
            "clicked_at": now(),
            "meta": {"url_id": oid(), "short_code": "abc1234", "owner_id": oid()},
            "ip_address": "1.2.3.4",
            "browser": "Chrome",
            "os": "Windows",
            "redirect_ms": 42,
        }
        base.update(overrides)
        return ClickDoc.model_validate(base)

    def test_instantiation(self):
        doc = self._make()
        assert doc.country == "Unknown"
        assert doc.city == "Unknown"
        assert doc.referrer is None
        assert doc.bot_name is None

    def test_meta_subdocument(self):
        doc = self._make()
        assert isinstance(doc.meta.url_id, ObjectId)
        assert doc.meta.short_code == "abc1234"

    def test_anonymous_owner_id(self):
        doc = self._make(
            **{
                "meta": {
                    "url_id": oid(),
                    "short_code": "abc1234",
                    "owner_id": ANONYMOUS_OWNER_ID,
                }
            }
        )
        assert doc.meta.owner_id == ANONYMOUS_OWNER_ID

    def test_to_mongo_round_trip(self):
        doc = self._make(referrer="google.com")
        restored = ClickDoc.from_mongo(doc.to_mongo())
        assert restored.referrer == "google.com"
        assert restored.redirect_ms == doc.redirect_ms

    def test_to_mongo_omits_none_fields(self):
        # Explicit nulls flip the per-field BSON type and close time-series
        # buckets; None fields must be absent from the insert document.
        data = self._make(referrer=None, bot_name=None).to_mongo()
        assert "referrer" not in data
        assert "bot_name" not in data
        assert "device" not in data
        assert "utm_source" not in data
        assert "domain" not in data["meta"]

    def test_to_mongo_keeps_set_fields(self):
        data = self._make(referrer="google.com", device="mobile").to_mongo()
        assert data["referrer"] == "google.com"
        assert data["device"] == "mobile"
        assert data["country"] == "Unknown"
