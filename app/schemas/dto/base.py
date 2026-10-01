"""
Shared base classes and field types for request and response DTOs.

All DTOs inherit from these to get consistent Pydantic configuration
without repeating ``model_config`` in every class.

``UtcDatetime`` is the standard type for response timestamp fields:
it serializes as ISO 8601 with an explicit UTC offset, stamping naive
datetimes (PyMongo read-backs) as UTC on the way out.
"""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _stamp_utc(dt: datetime) -> str:
    # PyMongo returns naive datetimes; without explicit tzinfo the JSON
    # form omits the offset and clients parse it as local time. Stamp
    # UTC so the wire format is unambiguous (`...+00:00`).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


UtcDatetime = Annotated[datetime, PlainSerializer(_stamp_utc, return_type=str)]
"""Response datetime that always serializes with an explicit UTC offset.

Use this for every new response timestamp field. Naive values (Mongo
read-backs, the client is not ``tz_aware``) are stamped as UTC; aware
values keep their instant. Wire form is ``2025-01-01T00:00:00+00:00``.
"""


class RequestBase(BaseModel):
    """Base class for all request DTOs."""

    model_config = ConfigDict(populate_by_name=True)


class ResponseBase(BaseModel):
    """Base class for all response DTOs."""

    model_config = ConfigDict(populate_by_name=True)
