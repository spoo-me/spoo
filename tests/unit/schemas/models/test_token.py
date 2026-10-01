"""Unit tests for VerificationTokenDoc."""

import pytest

from app.schemas.models.token import VerificationTokenDoc

from .conftest import now, oid


class TestVerificationTokenDoc:
    def _make(self, **overrides):
        base = {
            "_id": oid(),
            "user_id": oid(),
            "email": "user@example.com",
            "token_hash": "deadbeef" * 8,
            "token_type": "email_verify",
            "expires_at": now(),
            "created_at": now(),
        }
        base.update(overrides)
        return VerificationTokenDoc.model_validate(base)

    def test_instantiation(self):
        doc = self._make()
        assert doc.used_at is None
        assert doc.attempts == 0

    def test_negative_attempts_rejected(self):
        with pytest.raises(ValueError):
            self._make(attempts=-1)

    def test_to_mongo_round_trip(self):
        t = now()
        doc = self._make(used_at=t)
        restored = VerificationTokenDoc.from_mongo(doc.to_mongo())
        assert restored.token_type == "email_verify"
        assert restored.used_at == t
