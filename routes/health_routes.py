"""GET /health — liveness probe."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from middleware.openapi import PUBLIC_SECURITY

router = APIRouter(tags=["System"])


@router.get(
    "/health",
    openapi_extra=PUBLIC_SECURITY,
    operation_id="healthCheck",
    summary="Health Check",
)
async def health_check() -> JSONResponse:
    """Liveness probe. Public, unauthenticated, no dependency checks."""
    return JSONResponse(status_code=200, content={"status": "ok"})
