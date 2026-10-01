"""Unit tests for ApiKeyDoc."""

from app.schemas.models.api_key import ApiKeyDoc

from .conftest import now, oid


class TestApiKeyDoc:
    def _make(self, **overrides):
        base = {
            "_id": oid(),
            "user_id": oid(),
            "token_prefix": "AbCdEfGh",
            "token_hash": "abc123" * 10,
            "name": "My Key",
            "scopes": ["shorten:create"],
            "created_at": now(),
        }
        base.update(overrides)
        return ApiKeyDoc.model_validate(base)

    def test_instantiation(self):
        doc = self._make()
        assert doc.name == "My Key"
        assert doc.revoked is False
        assert doc.description is None
        assert doc.expires_at is None

    def test_to_mongo_round_trip(self):
        doc = self._make(description="test key")
        restored = ApiKeyDoc.from_mongo(doc.to_mongo())
        assert restored.description == "test key"
        assert restored.scopes == ["shorten:create"]
