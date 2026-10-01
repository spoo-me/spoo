"""Unit tests for PyObjectId and MongoBaseModel."""

import pytest
from bson import ObjectId

from app.schemas.models.base import MongoBaseModel, PyObjectId

from .conftest import oid

# ── PyObjectId ────────────────────────────────────────────────────────────────


class TestPyObjectId:
    def test_accepts_objectid_instance(self):
        o = oid()
        assert PyObjectId._validate(o) == o

    def test_accepts_valid_string(self):
        s = str(oid())
        result = PyObjectId._validate(s)
        assert isinstance(result, ObjectId)
        assert str(result) == s

    @pytest.mark.parametrize(
        "invalid",
        ["not-an-objectid", "tooshort", ""],
        ids=["bad_string", "too_short", "empty"],
    )
    def test_rejects_invalid_string(self, invalid):
        with pytest.raises(ValueError):
            PyObjectId._validate(invalid)

    def test_rejects_none(self):
        with pytest.raises((ValueError, TypeError)):
            PyObjectId._validate(None)


# ── MongoBaseModel ─────────────────────────────────────────────────────────────


class TestMongoBaseModel:
    def test_from_mongo_returns_none_for_none(self):
        assert MongoBaseModel.from_mongo(None) is None

    def test_id_alias(self):
        o = oid()
        m = MongoBaseModel.model_validate({"_id": o})
        assert m.id == o

    def test_to_mongo_drops_none_id(self):
        assert "_id" not in MongoBaseModel().to_mongo()

    def test_to_mongo_keeps_set_id(self):
        o = oid()
        m = MongoBaseModel.model_validate({"_id": o})
        assert m.to_mongo()["_id"] == o
