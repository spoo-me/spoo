"""Response DTO for GET /api/v1/domain-intel."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from schemas.dto.base import ResponseBase


class DomainWhois(ResponseBase):
    registrar: str | None = None
    created: str | None = None
    updated: str | None = None
    expires: str | None = None
    age_days: int | None = Field(
        default=None, description="Days since registration; young = suspect."
    )


class DomainSsl(ResponseBase):
    issuer: str | None = None
    subject: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    days_left: int | None = None
    sans: list[str] = Field(default_factory=list)


class DomainIntelResponse(ResponseBase):
    """Public records of a destination host: DNS, RDAP registration, TLS.

    ``whois``/``ssl`` are null when the registry or handshake doesn't
    answer — absence of data, never a verdict.
    """

    host: str
    registrable_domain: str
    dns: dict[str, list[str]]
    whois: DomainWhois | None = None
    ssl: DomainSsl | None = None
    fetched_at: datetime
