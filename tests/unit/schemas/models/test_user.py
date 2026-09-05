"""Unit tests for UserDoc."""

from schemas.models.user import UserDoc

from .conftest import now, oid


class TestUserDoc:
    def _make(self, **overrides):
        base = {"_id": oid(), "email": "user@example.com"}
        base.update(overrides)
        return UserDoc.model_validate(base)

    def test_minimal_instantiation(self):
        doc = self._make()
        assert doc.email == "user@example.com"
        assert doc.email_verified is False
        assert doc.password_set is False
        assert doc.status == "ACTIVE"
        assert doc.auth_providers == []

    def test_password_user_shape(self):
        doc = self._make(password_hash="$argon2id$...", password_set=True)
        assert doc.password_hash == "$argon2id$..."
        assert doc.password_set is True

    def test_oauth_user_shape(self):
        t = now()
        doc = self._make(
            password_hash=None,
            password_set=False,
            email_verified=True,
            pfp={
                "url": "https://example.com/pic.jpg",
                "source": "google",
                "last_updated": t,
            },
            auth_providers=[
                {
                    "provider": "google",
                    "provider_user_id": "123",
                    "email": "user@example.com",
                    "email_verified": True,
                    "profile": {
                        "name": "Alice",
                        "picture": "https://example.com/pic.jpg",
                    },
                    "linked_at": t,
                }
            ],
        )
        assert doc.pfp.source == "google"
        assert len(doc.auth_providers) == 1
        assert doc.auth_providers[0].provider == "google"

    def test_pfp_none(self):
        assert self._make(pfp=None).pfp is None

    def test_from_mongo_round_trip(self):
        o, t = oid(), now()
        doc = self._make(**{"_id": o, "created_at": t, "updated_at": t})
        restored = UserDoc.from_mongo(doc.to_mongo())
        assert restored.email == doc.email
        assert str(restored.id) == str(o)
