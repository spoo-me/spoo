"""
Base model for all MongoDB document models.

PyObjectId handles the mismatch between BSON ObjectId and Pydantic v2.
MongoBaseModel provides to_mongo() / from_mongo() for round-tripping between
Python objects and raw MongoDB dicts.
"""

from __future__ import annotations

from typing import Any, TypeVar

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler
from pydantic_core import core_schema


class PyObjectId(ObjectId):
    """BSON ObjectId that Pydantic v2 knows how to validate and serialize."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.to_string_ser_schema(),
        )

    @classmethod
    def _validate(cls, v: Any) -> ObjectId:
        if isinstance(v, ObjectId):
            return v
        if isinstance(v, str) and ObjectId.is_valid(v):
            return ObjectId(v)
        raise ValueError(f"Invalid ObjectId: {v!r}")


# Sentinel ObjectId used for anonymous/unowned URLs in the time-series collection.
# Using a consistent ObjectId type prevents MongoDB time-series bucket churn
# caused by mixing None and ObjectId types across documents.
ANONYMOUS_OWNER_ID = ObjectId("000000000000000000000000")

_T = TypeVar("_T", bound="MongoBaseModel")


class MongoBaseModel(BaseModel):
    """
    Base for all document models.

    Stores the MongoDB _id as `id` (PyObjectId). Subclasses add collection-
    specific fields on top.

    to_mongo()  — converts model → dict suitable for pymongo insert/update
    from_mongo() — converts raw pymongo dict → model instance (returns None
                    gracefully when passed None)
    """

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    id: PyObjectId | None = Field(default=None, alias="_id")

    def to_mongo(self) -> dict:
        """Return a dict ready for MongoDB insertion.

        - Renames `id` → `_id`
        - Excludes None `_id` so MongoDB can auto-generate it on insert
        - Uses field aliases where defined (important for v1 hyphenated keys)
        """
        data = self.model_dump(by_alias=True, exclude_none=False)
        # Normalise _id: drop if None (let MongoDB generate it), keep if set
        if data.get("_id") is None:
            data.pop("_id", None)
        return data

    @classmethod
    def from_mongo(cls: type[_T], data: dict | None) -> _T | None:
        """Build a model instance from a raw MongoDB document dict.

        Returns None when data is None (e.g. find_one returns None).
        Missing optional fields are filled with their defaults.
        """
        if data is None:
            return None
        return cls.model_validate(data)
