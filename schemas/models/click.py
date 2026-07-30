"""
Click document model.

Maps to the `clicks` MongoDB time-series collection.

Time-series schema:
  timeField  = "clicked_at"
  metaField  = "meta"
  granularity = "seconds"

The `meta` subdocument groups clicks by URL for efficient range queries.
owner_id always holds an ObjectId — anonymous clicks use ANONYMOUS_OWNER_ID
to avoid bucket churn from mixed None/ObjectId types.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from schemas.models.base import MongoBaseModel, PyObjectId


class ClickMeta(BaseModel):
    """The metaField subdocument for the time-series collection."""

    url_id: PyObjectId
    short_code: str
    owner_id: PyObjectId  # ANONYMOUS_OWNER_ID for unowned URLs
    # Nullable because time-series buckets created before this field existed
    # can't be backfilled — they keep their original shape forever.
    domain: str | None = None


class ClickDoc(MongoBaseModel):
    """Document model for the `clicks` time-series collection."""

    # Time-series timeField
    clicked_at: datetime

    # Time-series metaField
    meta: ClickMeta

    # Analytics fields
    ip_address: str
    country: str = "Unknown"
    city: str = "Unknown"
    browser: str
    os: str
    redirect_ms: int
    referrer: str | None = None  # sanitised referrer domain, nullable
    bot_name: str | None = None  # nullable
    # Nullable like meta.domain: clicks recorded before these fields existed
    # keep their original shape forever (time-series buckets can't be
    # backfilled). device is "mobile" | "tablet" | "desktop" | "unknown".
    device: str | None = None
    # UTM tags captured from the short link's own query string, sanitised
    # at event construction (services.click.events).
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None

    def to_mongo(self) -> dict:
        """Serialise for insertion, omitting None fields entirely.

        Time-series buckets track one BSON type per field. An explicit
        null following a string value (or vice versa) is a type conflict
        that closes the bucket early, while an absent field packs fine.
        Queries are unaffected: MongoDB treats missing and null the same
        in $eq/$group, and the stats sentinels already map both.
        """
        return self.model_dump(by_alias=True, exclude_none=True)
