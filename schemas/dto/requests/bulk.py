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
from pydantic import Field, field_validator, model_validator

from schemas.dto.base import RequestBase
from schemas.models.url import UrlStatus
from shared.datetime_utils import parse_datetime
from shared.tags import TAGS_MAX_PER_LINK
from shared.url_utils import normalise_fqdn
from shared.validators import normalise_object_ids

# Envelope cap = the pro plan's bulk_batch_max; the route enforces the
# caller's plan cap and the frontend chunks larger selections.
BULK_MAX_IDS = 1000

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


class BulkTagUrlsRequest(BulkIdsRequest):
    """Request body for bulk tag / untag, by tag id.

    At least one of ``add`` or ``remove`` must name a tag, and no tag may be
    in both.
    """

    add: list[str] = Field(
        default_factory=list,
        max_length=TAGS_MAX_PER_LINK,
        description=(
            f"Tag ids to add to every id (at most {TAGS_MAX_PER_LINK}); every one "
            "must be a tag you own. Tags a link already carries are kept once."
        ),
        examples=[["665f0c2f9e7a4b1d2c3d4e5f"]],
    )
    remove: list[str] = Field(
        default_factory=list,
        description="Tag ids to remove from every id. Tags a link does not carry are ignored.",
        examples=[["665f0c2f9e7a4b1d2c3d4e60"]],
    )

    @field_validator("add", "remove", mode="before")
    @classmethod
    def _tag_ids_are_object_ids(cls, v: list | None) -> object:
        return [] if v is None else normalise_object_ids(v)

    @model_validator(mode="after")
    def _add_or_remove_present(self) -> BulkTagUrlsRequest:
        if not self.add and not self.remove:
            raise ValueError("add or remove must name at least one tag")
        if len(self.add) > TAGS_MAX_PER_LINK:
            raise ValueError(f"at most {TAGS_MAX_PER_LINK} tags per link")
        both = set(self.add) & set(self.remove)
        if both:
            raise ValueError(
                f"tags cannot be both added and removed: {', '.join(sorted(both))}"
            )
        return self

    def add_ids(self) -> list[ObjectId]:
        return [ObjectId(i) for i in self.add]

    def remove_ids(self) -> list[ObjectId]:
        return [ObjectId(i) for i in self.remove]
