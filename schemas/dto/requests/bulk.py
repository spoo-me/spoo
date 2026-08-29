"""Request DTOs for bulk URL operations (``POST /api/v1/urls/bulk/*``).

Every bulk request carries ``ids`` — MongoDB ObjectIds, matching the
single-item management routes' 24-hex path param. Aliases are not
accepted: they aren't stable (rename is a legal PATCH) and ids are what
the dashboard's selection set already holds.

Envelope validation lives here (count cap, id format); anything
per-item is the service's job and comes back in the result report, not
as a 4xx.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from bson import ObjectId
from pydantic import Field, field_validator

from schemas.dto.base import RequestBase
from schemas.models.url import UrlStatus
from shared.datetime_utils import parse_datetime
from shared.url_utils import normalise_fqdn

# Server cap per request. The frontend chunks larger selections and
# merges the per-chunk reports. Bounded by report ergonomics and the
# per-day item math in middleware/rate_limiter.py, not by execution
# cost (the set-based pipeline is ~4 calls regardless of batch size).
BULK_MAX_IDS = 100

_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")


class BulkIdsRequest(RequestBase):
    """Shared envelope for all bulk URL operations."""

    ids: list[str] = Field(
        min_length=1,
        max_length=BULK_MAX_IDS,
        description=(
            "URL ids (MongoDB ObjectIds, as returned by the list endpoint). "
            f"1 to {BULK_MAX_IDS} per request; duplicates are deduplicated "
            "server-side (first occurrence wins). One malformed id rejects "
            "the whole request — nothing is attempted."
        ),
        examples=[["665f0c2f9e7a4b1d2c3d4e5f", "665f0c2f9e7a4b1d2c3d4e60"]],
    )

    @field_validator("ids")
    @classmethod
    def _ids_are_object_ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not _OBJECT_ID_RE.fullmatch(item):
                raise ValueError(f"'{item}' is not a valid URL id")
        return v

    def object_ids(self) -> list[ObjectId]:
        """The ids as ObjectIds, request order preserved (incl. duplicates)."""
        return [ObjectId(item) for item in self.ids]


class BulkDeleteUrlsRequest(BulkIdsRequest):
    """Request body for bulk delete — ids only, no parameters."""


class BulkUpdateStatusRequest(BulkIdsRequest):
    """Request body for bulk activate/deactivate."""

    status: Literal[UrlStatus.ACTIVE, UrlStatus.INACTIVE] = Field(
        description=(
            "Status applied to every id. `ACTIVE` enables redirects, "
            "`INACTIVE` disables them. `BLOCKED`/`EXPIRED` are not "
            "caller-settable, same as the single-item status endpoint."
        ),
        examples=["INACTIVE"],
    )


class BulkUpdateExpiryRequest(BulkIdsRequest):
    """Request body for bulk set/clear expiry."""

    expire_after: datetime | None = Field(
        description=(
            "Expiration applied to every id — ISO 8601 or epoch seconds, "
            "must be in the future. Pass `null` to clear expiry. One value "
            "for the whole batch."
        ),
        examples=[1767225600],
    )

    @field_validator(
        "expire_after", mode="before", json_schema_input_type=datetime | int | None
    )
    @classmethod
    def _parse_expire_after(cls, v: str | int | None) -> datetime | None:
        # Same coercion as UpdateUrlRequest.expire_after — one parser for
        # every wire form of an expiry timestamp.
        if v is None:
            return None
        result = parse_datetime(v)
        if result is None:
            raise ValueError("Invalid expire_after format")
        return result


class BulkMoveDomainRequest(BulkIdsRequest):
    """Request body for bulk domain move."""

    domain: str | None = Field(
        max_length=253,
        description=(
            "Target domain for every id — a custom domain you own (must be "
            "ACTIVE), or `null` to move back to the system default. One "
            "target for the whole batch."
        ),
        examples=["links.acme.com"],
    )

    @field_validator("domain", mode="before")
    @classmethod
    def _norm_domain(cls, v: str | None) -> str | None:
        # Same normalisation as UpdateUrlRequest.domain: null/"" mean the
        # system default; anything else is canonicalised (lowercase, no
        # trailing dot) before the route's ownership check sees it.
        if v is None or v == "":
            return None
        return normalise_fqdn(v)
