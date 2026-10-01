"""Request DTOs for /api/v1/billing."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from schemas.dto.base import RequestBase


class CheckoutRequest(RequestBase):
    cadence: Literal["monthly", "year"] = Field(
        description="monthly is a recurring subscription; year is a one-time prepaid purchase"
    )
    from_feature: str | None = Field(
        default=None,
        alias="from",
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Catalog key of the feature that sent the user here",
    )
    return_to: str = Field(
        default="/dashboard",
        alias="return",
        max_length=512,
        description="Dashboard path to land on after payment",
    )

    @field_validator("return_to")
    @classmethod
    def _relative_path_only(cls, v: str) -> str:
        if not v.startswith("/") or v.startswith("//") or "\\" in v:
            raise ValueError("return must be a path on this site")
        return v
