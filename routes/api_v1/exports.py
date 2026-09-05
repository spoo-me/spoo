"""
GET /api/v1/export              — export URL stats as CSV, XLSX, JSON, or XML.
GET /api/v1/export/links/{id}   — export twin of the per-link stats endpoint.

Both require authentication — every export is scoped to the caller's own
URLs. API key users require ``stats:read``, ``urls:read``, or ``admin:all``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response

from dependencies import (
    STATS_SCOPES,
    CurrentUser,
    Entitled,
    ExportSvc,
    UrlSvc,
    require_scopes,
)
from middleware.openapi import EXPORT_RESPONSES
from middleware.rate_limiter import Limits, limiter, plan_scaled
from routes.api_v1._helpers import parse_url_id
from schemas.dto.requests.stats import ExportQuery, LinkExportQuery

router = APIRouter(tags=["Statistics"])

# Shared 200 documentation for both export routes — the download body is
# format-dependent, never JSON-schema'd.
_EXPORT_200_RESPONSES = {
    **EXPORT_RESPONSES,
    200: {
        "description": "Export file download",
        "content": {
            "application/json": {
                "schema": {"type": "string", "format": "binary"},
            },
            "application/xml": {
                "schema": {"type": "string", "format": "binary"},
            },
            "application/zip": {
                "schema": {"type": "string", "format": "binary"},
                "x-description": "CSV export — ZIP archive containing summary.csv plus one file per dimension",
            },
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
                "schema": {"type": "string", "format": "binary"},
                "x-description": "XLSX export — Excel workbook with multiple sheets",
            },
        },
    },
}


@router.get(
    "/export",
    responses=_EXPORT_200_RESPONSES,
    operation_id="exportStats",
    summary="Export Statistics",
)
@limiter.limit(plan_scaled(Limits.API_EXPORT_AUTHED))
async def export_v1(
    request: Request,
    query: Annotated[ExportQuery, Query()],
    export_service: ExportSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(STATS_SCOPES)),  # noqa: B008
) -> Response:
    """Export click statistics across all URLs you own as a downloadable file.

    Generate a file export of click analytics data in the specified format.
    The response is a binary download with appropriate `Content-Disposition` header.

    **Authentication**: Required.

    **API Key Scope**: `stats:read`, `urls:read`, or `admin:all`

    **Rate Limits**: 30/min, 1,000/day

    **Export Formats**:

    - `json` — single JSON file
    - `xml` — single XML file
    - `xlsx` — Excel spreadsheet with multiple sheets
    - `csv` — **ZIP archive** containing `summary.csv` plus one CSV file per metrics dimension

    **Filtering**: Same as `GET /stats` — including the `url_id` filter to
    slice the export to specific URLs you own. To export a single link,
    prefer `GET /export/links/{url_id}`.

    **Note**: Export generation is resource-intensive. Lower rate limits apply
    compared to other endpoints.
    """
    result = await export_service.export(query, str(user.user_id))
    return Response(
        content=result.content,
        media_type=result.mimetype,
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )


@router.get(
    "/export/links/{url_id}",
    responses=_EXPORT_200_RESPONSES,
    operation_id="exportLinkStats",
    summary="Export Link Statistics",
)
@limiter.limit(plan_scaled(Limits.API_EXPORT_AUTHED))
async def export_link_v1(
    request: Request,
    url_id: Annotated[
        str,
        Path(description="Unique identifier of the URL (MongoDB ObjectId)."),
    ],
    query: Annotated[LinkExportQuery, Query()],
    export_service: ExportSvc,
    url_service: UrlSvc,
    entitlements: Entitled,
    user: CurrentUser = Depends(require_scopes(STATS_SCOPES)),  # noqa: B008
) -> Response:
    """Export click statistics for a single URL you own.

    The export twin of `GET /stats/links/{url_id}` — the same formats as
    `GET /export`, pre-scoped to one link. The suggested filename carries
    the link's alias.

    **Authentication**: Required — you must own the URL.

    **API Key Scope**: `stats:read`, `urls:read`, or `admin:all`

    **Rate Limits**: 30/min, 1,000/day

    **Export Formats**:

    - `json` — single JSON file
    - `xml` — single XML file
    - `xlsx` — Excel spreadsheet with multiple sheets
    - `csv` — **ZIP archive** containing `summary.csv` plus one CSV file per metrics dimension

    **Errors**:

    - `400` — malformed id (not a valid ObjectId)
    - `404` — no URL with that id in your account. A URL owned by someone
      else answers identically; this endpoint never confirms foreign ids.

    **Note**: Export generation is resource-intensive. Lower rate limits apply
    compared to other endpoints.
    """
    oid = parse_url_id(url_id)
    url = await url_service.get_owned(oid, user.user_id)
    result = await export_service.export_link(query, url)
    return Response(
        content=result.content,
        media_type=result.mimetype,
        headers={"Content-Disposition": f'attachment; filename="{result.filename}"'},
    )
