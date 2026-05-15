"""
Common response DTOs shared across multiple endpoints.

ErrorResponse    — standard error shape from AppError.to_dict()
HealthResponse   — GET /health
MessageResponse  — generic {success, message} shape used by many endpoints
"""

from __future__ import annotations

from typing import Any

from schemas.dto.base import ResponseBase


class ErrorResponse(ResponseBase):
    """Standard error JSON body produced by the AppError exception handler."""

    error: str
    code: str
    field: str | None = None
    details: Any | None = None


class HealthResponse(ResponseBase):
    """Response body for GET /health (liveness probe)."""

    status: str


class MessageResponse(ResponseBase):
    """Generic success/message response returned by several endpoints."""

    success: bool
    message: str | None = None


class PaginationMeta(ResponseBase):
    """Reusable pagination metadata included in list responses."""

    page: int
    page_size: int
    total: int
    has_next: bool
