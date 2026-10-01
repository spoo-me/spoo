"""Request DTOs for the custom-domain API."""

from __future__ import annotations

from typing import Any

from pydantic import Field, HttpUrl, field_validator

from app.schemas.dto.base import RequestBase
from app.shared.url_utils import normalise_fqdn


class CreateCustomDomainRequest(RequestBase):
    """Body for ``POST /api/v1/custom-domains``."""

    fqdn: str = Field(
        max_length=253,
        description="The fully-qualified domain to register (e.g. links.acme.com).",
        examples=["links.acme.com"],
    )

    @field_validator("fqdn", mode="before")
    @classmethod
    def _validate_fqdn(cls, v: Any) -> str:
        return normalise_fqdn(v)


class VerifyCustomDomainRequest(RequestBase):
    """Body for ``POST /api/v1/custom-domains/{id}/verify`` — empty by design.

    Triggers a fresh verifier dispatch for the named domain. Existing fields
    on the doc (verification_method, verification_token) drive the strategy.
    """


class ListCustomDomainsQuery(RequestBase):
    """Query string for ``GET /api/v1/custom-domains``."""

    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=100)


class UpdateCustomDomainRequest(RequestBase):
    """Body for ``PATCH /api/v1/custom-domains/{id}``.

    Partial update: callers send only the fields they want to change.
    Field omitted = leave doc value as-is. Field explicitly ``null`` = clear
    the stored value. The service distinguishes the two via ``model_fields_set``.
    """

    root_redirect: HttpUrl | None = Field(
        default=None,
        description=(
            "Destination URL for `GET /` on the custom domain. Returns 302. "
            "Pass `null` to clear. Field omitted leaves current value."
        ),
        examples=["https://acme.com/landing"],
    )
    not_found_redirect: HttpUrl | None = Field(
        default=None,
        description=(
            "Fallback URL for any path not matching an alias. Returns 302 "
            "instead of the default 404. Pass `null` to clear."
        ),
        examples=["https://acme.com/404"],
    )
    custom_robots_txt: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Override the default `User-agent: *\\nDisallow: /` body served at "
            "/robots.txt. Capped at 4096 chars. Pass `null` to clear. Note: "
            "responses to alias redirects always carry `X-Robots-Tag: "
            "noindex, nofollow, noarchive` regardless of this field — short "
            "URLs are pure redirects with no indexable content."
        ),
    )

    @field_validator("custom_robots_txt", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        # Empty-string from a form submission is "clear" semantically — treat
        # the same as explicit null so the doc field becomes None, not "".
        if isinstance(v, str) and not v.strip():
            return None
        return v
