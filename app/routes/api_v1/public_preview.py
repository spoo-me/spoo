"""GET /api/v1/public/preview/{short_code} — public link preview.

The wire behind the frontend's /{code}+ page. No auth dependency at all:
the response is identical for everyone, owners included. Resolution is
status-agnostic (expired / inactive / blocked links still answer 200 with
their status stated; only truly missing codes 404), but the destination
rides the wire only while the link is active and not password-protected.
Ships dark until the frontend + Caddy serve the page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.dependencies.services import PublicPreviewSvc
from app.middleware.openapi import ERROR_RESPONSES, PUBLIC_SECURITY
from app.middleware.rate_limiter import Limits, limiter
from app.schemas.dto.responses.public_preview import PublicPreviewResponse

router = APIRouter(tags=["Public"])


@router.get(
    "/public/preview/{short_code}",
    responses=ERROR_RESPONSES,
    openapi_extra=PUBLIC_SECURITY,
    operation_id="getPublicPreview",
    summary="Public Link Preview",
)
@limiter.limit(Limits.PUBLIC_PREVIEW)
async def public_preview(
    short_code: str,
    request: Request,
    preview_service: PublicPreviewSvc,
) -> PublicPreviewResponse:
    """Preview where a short link leads before following it.

    Returns the link's resolved facts — status, creation date, whether a
    password gates it, and (only while the link is active and unlocked)
    the destination plus every geo-targeted destination, grouped by URL.

    **Authentication**: None. The preview shows the same resolved facts to
    everyone; owner-set social meta never appears here.

    **Rate Limits**: 30/min, 2,000/day.
    """
    return await preview_service.get_preview(short_code)
